"""ApplyPilot database layer: schema, migrations, stats, and connection helpers.

Single source of truth for the jobs table schema. All columns from every
pipeline stage are created up front so any stage can run independently
without migration ordering issues.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from applypilot import config

log = logging.getLogger(__name__)

# Thread-local connection storage — each thread gets its own connection.
_local = threading.local()
_SQLITE_BUSY_TIMEOUT_MS = 30_000
# Bumped whenever a migration changes something ALTER TABLE cannot express
# (column type changes, renames, index-only changes, backfills). Databases
# stamped with a lower version are walked through _apply_migrations().
SCHEMA_VERSION = 2
# Safety cap applied when get_jobs_by_stage is asked for an "unlimited" batch
# (limit <= 0). Rows carry full descriptions, so an unbounded SELECT * could
# materialize the whole table; 1000 rows is 10x the largest stage default.
_MAX_UNLIMITED_STAGE_ROWS = 1000
# Jobs inserted before a commit is issued, so a large discovery batch does not
# hold SQLite's single write lock (and therefore every other writer) hostage.
_STORE_COMMIT_BATCH = 200
JobRecord = dict[str, Any]
DatabaseStats = dict[str, Any]


# PRAGMAs that mutate the database file/header and therefore need the write lock.
_WRITE_PRAGMAS = frozenset({"user_version", "schema_version", "application_id"})


class _WriteSerializedConnection(sqlite3.Connection):
    """SQLite connection that opens every write transaction with ``BEGIN IMMEDIATE``.

    SQLite remains responsible for its own database-level locking. Taking the
    RESERVED write lock up front means concurrent ApplyPilot workers — threads
    *and* separate processes — queue on SQLite's own ``busy_timeout`` instead of
    deadlocking on an out-of-band advisory lock file. (A POSIX ``flock`` treats
    two descriptors inside one process as conflicting, so the previous
    file-based lock made ``--workers 4`` serialise, and it leaked whenever a
    caller raised between a write and its commit.)
    """

    @staticmethod
    def _is_read_only(statement: str) -> bool:
        """Conservatively classify SQL that cannot mutate database state."""
        sql = statement.lstrip()
        while sql.startswith("--") or sql.startswith("/*"):
            if sql.startswith("--"):
                _, separator, sql = sql.partition("\n")
                if not separator:
                    return True
            else:
                _, separator, sql = sql.partition("*/")
                if not separator:
                    return False
            sql = sql.lstrip()
        verb = sql.split(None, 1)[0].upper() if sql else ""
        if verb == "PRAGMA":
            # `PRAGMA <name>` reads; only header-writing pragmas mutate state.
            parts = sql.replace("=", " ").split()
            name = parts[1].lower() if len(parts) > 1 else ""
            return name not in _WRITE_PRAGMAS
        return verb in {"SELECT", "EXPLAIN", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"}

    def _begin_write_transaction(self) -> None:
        """Acquire SQLite's write lock before the first mutating statement."""
        if self.in_transaction:
            return
        super().execute("BEGIN IMMEDIATE")

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        if not self._is_read_only(sql):
            self._begin_write_transaction()
        return super().execute(sql, parameters)

    def executemany(self, sql: str, parameters: Any, /) -> sqlite3.Cursor:
        if not self._is_read_only(sql):
            self._begin_write_transaction()
        return super().executemany(sql, parameters)

    def writer_lock(self) -> "_WriterLock":
        """Return an RAII guard that commits/rolls back and releases the write lock."""
        return _WriterLock(self)


class _WriterLock:
    """Context manager that always ends an open write transaction.

    Guarantees the write lock is handed back even when the guarded block raises
    between a write and its commit, which is what previously wedged every
    subsequent writer until it timed out.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        return self._conn

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            if self._conn.in_transaction:
                if exc_type is None:
                    self._conn.commit()
                else:
                    self._conn.rollback()
        except sqlite3.Error as exc:
            log.warning("Could not close the write transaction cleanly: %s", exc)


def _close_quietly(conn: sqlite3.Connection) -> None:
    """Close a connection, ignoring errors from an already-broken handle."""
    try:
        conn.close()
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        log.debug("Ignoring error while closing connection: %s", exc)


def _open_connection(path: str) -> sqlite3.Connection:
    """Open a fresh connection with ApplyPilot's PRAGMA baseline."""
    conn = sqlite3.connect(
        path,
        timeout=_SQLITE_BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
        factory=_WriteSerializedConnection,
    )
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA cache_size=-64000")
    except BaseException:
        # Never leak the file descriptor (and the open write transaction) when
        # a PRAGMA fails on a fresh handle.
        _close_quietly(conn)
        raise
    conn.row_factory = sqlite3.Row
    return conn


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Get a thread-local SQLite connection configured for concurrent workers.

    Every new connection enables WAL, NORMAL synchronous mode, a 30-second
    busy timeout, and a 64 MiB page cache. Write transactions take SQLite's
    write lock up front (``BEGIN IMMEDIATE``) so writers queue instead of
    colliding.
    """
    path = str(Path(db_path or config.DB_PATH).expanduser().resolve())
    connections: dict[str, sqlite3.Connection] = getattr(_local, "connections", {})
    if not hasattr(_local, "connections"):
        _local.connections = connections

    conn = connections.get(path)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            connections.pop(path, None)
            _close_quietly(conn)

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = _open_connection(path)
    connections[path] = conn
    return conn


def close_connection(db_path: Path | str | None = None) -> None:
    """Close the cached connection for the current thread."""
    path = str(Path(db_path or config.DB_PATH).expanduser().resolve())
    connections: dict[str, sqlite3.Connection] = getattr(_local, "connections", {})
    conn = connections.pop(path, None)
    if conn is not None:
        _close_quietly(conn)


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create the full jobs table with all columns from every pipeline stage.

    This is idempotent -- safe to call on every startup. Uses CREATE TABLE IF NOT EXISTS
    so it won't destroy existing data.

    Schema columns by stage:
      - Discovery:  url, title, company, salary, description, location, site, strategy, discovered_at
      - Enrichment: full_description, application_url, detail_scraped_at, detail_error
      - Scoring:    fit_score, score_reasoning, scored_at
      - Tailoring:  tailored_resume_path, tailored_at, tailor_attempts, pdf_generated_at
      - Cover:      cover_letter_path, cover_letter_at, cover_attempts
      - Apply:      applied_at, apply_status, apply_error, apply_attempts,
                   agent_id, last_attempted_at, apply_duration_ms, apply_task_id,
                   verification_confidence

    Args:
        db_path: Override the default DB_PATH.

    Returns:
        sqlite3.Connection with the schema initialized.
    """
    path = db_path or config.DB_PATH

    # Ensure parent directory exists
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            -- Discovery stage (smart_extract / job_search)
            url                   TEXT PRIMARY KEY,
            title                 TEXT,
            company               TEXT,
            salary                TEXT,
            description           TEXT,
            location              TEXT,
            site                  TEXT,
            strategy              TEXT,
            discovered_at         TEXT,

            -- Enrichment stage (detail_scraper)
            full_description      TEXT,
            application_url       TEXT,
            detail_scraped_at     TEXT,
            detail_error          TEXT,

            -- Scoring stage (job_scorer)
            fit_score             INTEGER,
            score_reasoning       TEXT,
            scored_at             TEXT,

            -- Tailoring stage (resume tailor + PDF)
            tailored_resume_path  TEXT,
            tailored_at           TEXT,
            tailor_attempts       INTEGER DEFAULT 0,
            pdf_generated_at      TEXT,

            -- Cover letter stage
            cover_letter_path     TEXT,
            cover_letter_at       TEXT,
            cover_attempts        INTEGER DEFAULT 0,

            -- Application stage
            applied_at            TEXT,
            apply_status          TEXT,
            apply_error           TEXT,
            apply_attempts        INTEGER DEFAULT 0,
            agent_id              TEXT,
            last_attempted_at     TEXT,
            apply_duration_ms     INTEGER,
            apply_task_id         TEXT,
            verification_confidence TEXT,

            -- Next-Gen Intelligence, ATS, Export & CRM columns (v0.5.0)
            fingerprint           TEXT,
            sponsor_status        TEXT,
            work_arrangement      TEXT,
            ats_coverage_pct      INTEGER,
            score_breakdown       TEXT,
            docx_resume_path      TEXT,
            docx_cover_path       TEXT,
            crm_stage             TEXT DEFAULT 'discovered',
            user_notes            TEXT,
            prep_pack_path        TEXT,
            outreach_path         TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS question_bank (
            question_key      TEXT PRIMARY KEY,
            question_text     TEXT NOT NULL,
            canonical_answer  TEXT NOT NULL,
            category          TEXT,
            times_used        INTEGER DEFAULT 1,
            updated_at        TEXT
        )
    """)
    conn.commit()

    # Run migrations for any columns added after initial schema.
    ensure_columns(conn)

    # Column-name equivalents for this schema: URL is already indexed as the
    # primary key, apply_status is the application status, fit_score is the
    # score, and discovered_at is the creation timestamp.
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_apply_status ON jobs(apply_status)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_fit_score_discovered ON jobs(fit_score DESC, discovered_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_pending_detail ON jobs(detail_scraped_at, site)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_crm_score_date ON jobs(crm_stage, fit_score DESC, discovered_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_sponsor_status ON jobs(sponsor_status)",
    ):
        conn.execute(index_sql)
    conn.commit()

    return conn


# Complete column registry: column_name -> SQL type with optional default.
# This is the single source of truth. Adding a column here is all that's needed
# for it to appear in both new databases and migrated ones.
_ALL_COLUMNS: dict[str, str] = {
    # Discovery
    "url": "TEXT PRIMARY KEY",
    "title": "TEXT",
    "company": "TEXT",
    "salary": "TEXT",
    "description": "TEXT",
    "location": "TEXT",
    "site": "TEXT",
    "strategy": "TEXT",
    "discovered_at": "TEXT",
    # Enrichment
    "full_description": "TEXT",
    "application_url": "TEXT",
    "detail_scraped_at": "TEXT",
    "detail_error": "TEXT",
    # Scoring
    "fit_score": "INTEGER",
    "score_reasoning": "TEXT",
    "scored_at": "TEXT",
    # Tailoring
    "tailored_resume_path": "TEXT",
    "tailored_at": "TEXT",
    "tailor_attempts": "INTEGER DEFAULT 0",
    "pdf_generated_at": "TEXT",
    # Cover letter
    "cover_letter_path": "TEXT",
    "cover_letter_at": "TEXT",
    "cover_attempts": "INTEGER DEFAULT 0",
    # Application
    "applied_at": "TEXT",
    "apply_status": "TEXT",
    "apply_error": "TEXT",
    "apply_attempts": "INTEGER DEFAULT 0",
    "agent_id": "TEXT",
    "last_attempted_at": "TEXT",
    "apply_duration_ms": "INTEGER",
    "apply_task_id": "TEXT",
    "verification_confidence": "TEXT",
    # Next-Gen Intelligence, ATS, Export & CRM
    "fingerprint": "TEXT",
    "sponsor_status": "TEXT",
    "work_arrangement": "TEXT",
    "ats_coverage_pct": "INTEGER",
    "score_breakdown": "TEXT",
    "docx_resume_path": "TEXT",
    "docx_cover_path": "TEXT",
    "crm_stage": "TEXT DEFAULT 'discovered'",
    "user_notes": "TEXT",
    "prep_pack_path": "TEXT",
    "outreach_path": "TEXT",
}


def ensure_columns(conn: sqlite3.Connection | None = None) -> list[str]:
    """Add any missing columns to the jobs table (forward migration).

    Reads the current table schema via PRAGMA table_info and compares against
    the full column registry. Any missing columns are added with ALTER TABLE.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        List of column names that were added (empty if schema was already current).
    """
    if conn is None:
        conn = get_connection()

    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    added = []

    for col, dtype in _ALL_COLUMNS.items():
        if col not in existing:
            if "PRIMARY KEY" in dtype:
                continue
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {dtype}")
            added.append(col)

    if added:
        conn.commit()

    return added


def get_stats(conn: sqlite3.Connection | None = None) -> DatabaseStats:
    """Return job counts by pipeline stage.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        Dictionary with pipeline stage counts.
    """
    if conn is None:
        conn = get_connection()

    stats: DatabaseStats = {}

    # Total jobs
    stats["total"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    # By site breakdown
    rows = conn.execute(
        "SELECT site, COUNT(*) as cnt FROM jobs GROUP BY site ORDER BY cnt DESC"
    ).fetchall()
    stats["by_site"] = [(row[0], row[1]) for row in rows]

    # Enrichment stage
    stats["pending_detail"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL"
    ).fetchone()[0]

    stats["with_description"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL"
    ).fetchone()[0]

    stats["detail_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_error IS NOT NULL"
    ).fetchone()[0]

    # Scoring stage
    stats["scored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL"
    ).fetchone()[0]

    stats["unscored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL AND fit_score IS NULL"
    ).fetchone()[0]

    # Score distribution
    dist_rows = conn.execute(
        "SELECT fit_score, COUNT(*) as cnt FROM jobs "
        "WHERE fit_score IS NOT NULL "
        "GROUP BY fit_score ORDER BY fit_score DESC"
    ).fetchall()
    stats["score_distribution"] = [(row[0], row[1]) for row in dist_rows]

    # Tailoring stage
    stats["tailored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL"
    ).fetchone()[0]

    stats["untailored_eligible"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE fit_score >= 7 AND full_description IS NOT NULL "
        "AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    stats["tailor_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(tailor_attempts, 0) >= 5 "
        "AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    # Cover letter stage
    stats["with_cover_letter"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL"
    ).fetchone()[0]

    stats["cover_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(cover_attempts, 0) >= 5 "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '')"
    ).fetchone()[0]

    # Application stage
    stats["applied"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL"
    ).fetchone()[0]

    stats["apply_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_error IS NOT NULL"
    ).fetchone()[0]

    stats["ready_to_apply"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE tailored_resume_path IS NOT NULL "
        "AND applied_at IS NULL "
        "AND application_url IS NOT NULL"
    ).fetchone()[0]

    # Next-Gen CRM & ATS Metrics
    ensure_columns(conn)
    stats["verified_sponsors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE sponsor_status IN ('licensed_sponsor', 'sponsorship_offered')"
    ).fetchone()[0]

    avg_cov = conn.execute(
        "SELECT ROUND(AVG(ats_coverage_pct), 1) FROM jobs WHERE ats_coverage_pct IS NOT NULL"
    ).fetchone()[0]
    stats["avg_ats_coverage"] = float(avg_cov) if avg_cov is not None else 0.0

    stats["with_docx"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE docx_resume_path IS NOT NULL"
    ).fetchone()[0]

    stats["with_prep_pack"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE prep_pack_path IS NOT NULL"
    ).fetchone()[0]

    stats["with_outreach"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE outreach_path IS NOT NULL"
    ).fetchone()[0]

    crm_rows = conn.execute(
        "SELECT COALESCE(crm_stage, 'discovered'), COUNT(*) FROM jobs GROUP BY COALESCE(crm_stage, 'discovered')"
    ).fetchall()
    stats["by_crm_stage"] = {r[0]: r[1] for r in crm_rows}

    return stats


# ---------------------------------------------------------------------------
# Cross-Board Fuzzy Deduplication & Fingerprinting
# ---------------------------------------------------------------------------

_CORP_SUFFIX_RE = re.compile(
    r"\b(?:limited|ltd|plc|inc|incorporated|corp|corporation|llc|llp|gmbh|ag|b\.?v\.?|n\.?v\.?|s\.?a\.?|s\.?r\.?l\.?|group|holdings|uk|eu|europe|international|global)\b",
    re.IGNORECASE,
)
_TITLE_NOISE_RE = re.compile(
    r"\([^)]*\)|\[[^\]]*\]|\b(?:m/f/d|f/m/x|w/m/d|hybrid|remote|onsite|full[-\s]?time|contract|permanent|ref\s*[:#]?\s*\w+)\b",
    re.IGNORECASE,
)


def compute_job_fingerprint(
    company: str | None,
    title: str | None,
    location: str | None = None,
) -> str:
    """Compute a normalized cross-board fingerprint for `(company, title, primary_location)`.

    Used to identify and merge duplicate job postings across aggregators (LinkedIn,
    Indeed, Reed, eFinancialCareers) and direct employer ATS portals.
    """
    comp = (company or "").lower()
    comp = _CORP_SUFFIX_RE.sub(" ", comp)
    comp = re.sub(r"[^a-z0-9]+", "", comp).strip() or "unknown"

    ttl = (title or "").lower()
    ttl = _TITLE_NOISE_RE.sub(" ", ttl)
    ttl = re.sub(r"[^a-z0-9]+", " ", ttl)
    ttl = "_".join(ttl.split()) or "untitled"

    loc_raw = (location or "").lower()
    if "remote" in loc_raw and not any(
        city in loc_raw for city in ("london", "dublin", "amsterdam", "berlin", "munich", "frankfurt", "paris", "zurich", "madrid", "luxembourg")
    ):
        loc = "remote"
    else:
        first_part = loc_raw.split(",")[0].split("-")[0].split("/")[0]
        first_part = _TITLE_NOISE_RE.sub(" ", first_part)
        loc = re.sub(r"[^a-z0-9]+", "", first_part).strip() or "any"

    return f"{comp}::{ttl}::{loc}"


_DIRECT_ATS_STRATEGIES = {
    "workday",
    "workday_api",
    "ats_direct",
    "greenhouse_api",
    "lever_api",
    "ashby_api",
    "smartrecruiters_api",
    "personio_api",
    "workable_api",
    "recruitee_api",
}


def deduplicate_jobs(conn: sqlite3.Connection | None = None) -> DatabaseStats:
    """Merge duplicate job rows sharing the same normalized `(company, title, location)` fingerprint.

    Prioritizes:
      1. Rows already applied / tailored / scored
      2. Direct employer ATS URLs (Workday, Greenhouse, Lever, Ashby, SmartRecruiters) over aggregators
      3. Rows with the richest `full_description` / `description`

    Merges non-null metadata from duplicate rows into the winning canonical row before deleting duplicates.

    Returns:
        {"checked": int, "duplicates_removed": int, "groups_merged": int}
    """
    if conn is None:
        conn = get_connection()

    ensure_columns(conn)
    count_row = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
    checked = int(count_row[0]) if count_row is not None else 0
    if checked == 0:
        return {"checked": 0, "duplicates_removed": 0, "groups_merged": 0}

    max_row = conn.execute("SELECT MAX(rowid) FROM jobs").fetchone()
    max_rowid = int(max_row[0]) if max_row is not None and max_row[0] is not None else 0

    # Backfill old records in bounded rowid windows rather than materializing
    # every job just to compute missing fingerprints.
    while True:
        missing_rows = conn.execute(
            "SELECT rowid AS _dedup_rowid, company, title, location "
            "FROM jobs WHERE rowid <= ? AND (fingerprint IS NULL OR fingerprint = '') "
            "ORDER BY rowid LIMIT 500",
            (max_rowid,),
        ).fetchall()
        if not missing_rows:
            break

        fingerprint_updates: list[tuple[str, int]] = []
        for row in missing_rows:
            fingerprint = compute_job_fingerprint(row["company"], row["title"], row["location"])
            fingerprint_updates.append((fingerprint, int(row["_dedup_rowid"])))
        conn.executemany(
            "UPDATE jobs SET fingerprint = ? WHERE rowid = ?",
            fingerprint_updates,
        )

    direct_strategies = tuple(sorted(_DIRECT_ATS_STRATEGIES))
    direct_domains = (
        "myworkdayjobs.com",
        "greenhouse.io",
        "lever.co",
        "ashbyhq.com",
        "smartrecruiters.com",
        "personio.",
        "workable.com",
        "recruitee.com",
    )
    strategy_placeholders = ", ".join("?" for _ in direct_strategies)
    domain_conditions = " OR ".join(
        "LOWER(COALESCE(url, '')) LIKE ?" for _ in direct_domains
    )
    group_order = f"""
        ORDER BY
            CASE WHEN NULLIF(applied_at, '') IS NOT NULL THEN 1 ELSE 0 END DESC,
            CASE WHEN NULLIF(tailored_resume_path, '') IS NOT NULL THEN 1 ELSE 0 END DESC,
            CASE WHEN LOWER(COALESCE(strategy, '')) IN ({strategy_placeholders})
                 OR {domain_conditions} THEN 1 ELSE 0 END DESC,
            CASE WHEN fit_score IS NOT NULL THEN 1 ELSE 0 END DESC,
            LENGTH(COALESCE(NULLIF(full_description, ''), NULLIF(description, ''), '')) DESC,
            rowid ASC
    """
    group_params: tuple[str, ...] = direct_strategies + tuple(
        f"%{domain}%" for domain in direct_domains
    )

    merge_fields = (
        "salary", "description", "full_description", "application_url",
        "detail_scraped_at", "fit_score", "score_reasoning", "scored_at",
        "score_breakdown", "tailored_resume_path", "tailored_at",
        "pdf_generated_at", "cover_letter_path", "cover_letter_at",
        "docx_resume_path", "docx_cover_path", "sponsor_status",
        "work_arrangement", "ats_coverage_pct", "prep_pack_path", "outreach_path",
    )

    duplicates_removed = 0
    groups_merged = 0
    max_fingerprint_row = conn.execute(
        "SELECT MAX(fingerprint) FROM jobs WHERE fingerprint IS NOT NULL AND fingerprint != ''"
    ).fetchone()
    max_fingerprint = (
        str(max_fingerprint_row[0])
        if max_fingerprint_row is not None and max_fingerprint_row[0] is not None
        else ""
    )
    last_fingerprint = ""
    while True:
        fingerprint_rows = conn.execute(
            "SELECT fingerprint FROM jobs "
            "WHERE rowid <= ? AND fingerprint IS NOT NULL AND fingerprint != '' "
            "AND fingerprint > ? AND fingerprint <= ? "
            "GROUP BY fingerprint HAVING COUNT(*) > 1 "
            "ORDER BY fingerprint LIMIT 500",
            (max_rowid, last_fingerprint, max_fingerprint),
        ).fetchall()
        if not fingerprint_rows:
            break

        for fingerprint_row in fingerprint_rows:
            fingerprint = str(fingerprint_row[0])
            last_fingerprint = fingerprint
            if fingerprint.startswith("unknown::") or fingerprint.endswith("::untitled::any"):
                continue

            group_rows = conn.execute(
                f"SELECT * FROM jobs WHERE fingerprint = ? AND rowid <= ? {group_order}",
                (fingerprint, max_rowid, *group_params),
            )
            winner_row = group_rows.fetchone()
            if winner_row is None:
                continue
            winner = dict(winner_row)
            winner_url = winner["url"]
            losers: list[str] = []
            updates: dict[str, object] = {}

            # The SQL order matches the former in-memory rank. Retaining only
            # the winner and loser URLs bounds Python memory by the largest
            # duplicate group, not by the total jobs table.
            for loser_row in group_rows:
                loser = dict(loser_row)
                losers.append(str(loser["url"]))
                for field in merge_fields:
                    if winner.get(field) in (None, "") and loser.get(field) not in (None, ""):
                        updates[field] = loser[field]
                        winner[field] = loser[field]

            winner_desc = winner.get("full_description") or ""
            for description_row in conn.execute(
                "SELECT url, full_description FROM jobs "
                f"WHERE fingerprint = ? AND rowid <= ? {group_order}",
                (fingerprint, max_rowid, *group_params),
            ):
                if description_row["url"] == winner_url:
                    continue
                loser_desc = description_row["full_description"] or ""
                if len(loser_desc) > len(winner_desc) + 200:
                    updates["full_description"] = loser_desc
                    winner_desc = loser_desc

            if updates:
                set_clause = ", ".join(f"{field} = ?" for field in updates)
                conn.execute(
                    f"UPDATE jobs SET {set_clause} WHERE url = ?",
                    (*updates.values(), winner_url),
                )

            if losers:
                conn.executemany(
                    "DELETE FROM jobs WHERE url = ?",
                    ((loser_url,) for loser_url in losers),
                )
                duplicates_removed += len(losers)
                groups_merged += 1

    conn.commit()
    return {
        "checked": checked,
        "duplicates_removed": duplicates_removed,
        "groups_merged": groups_merged,
    }


def store_jobs(
    conn: sqlite3.Connection,
    jobs: list[JobRecord],
    site: str,
    strategy: str,
) -> tuple[int, int]:
    """Store discovered jobs, skipping duplicates by URL and enriching metadata.

    Args:
        conn: Database connection.
        jobs: List of job dicts with keys: url, title, company, salary, description, location,
              and optional full_description, application_url.
        site: Source site name (e.g. "RemoteOK", "Greenhouse").
        strategy: Extraction strategy used (e.g. "json_ld", "greenhouse_api").

    Returns:
        Tuple of (new_count, duplicate_count).
    """
    from applypilot.enrichment.sponsors import (
        detect_sponsor_status,
        detect_work_arrangement,
    )

    ensure_columns(conn)
    now = datetime.now(UTC).isoformat()
    new = 0
    existing = 0

    for job in jobs:
        url = job.get("url")
        if not url:
            continue
        title = job.get("title")
        company = job.get("company") or site
        location = job.get("location")
        desc = job.get("full_description") or job.get("description")
        fp = compute_job_fingerprint(company, title, location)
        sponsor = job.get("sponsor_status") or detect_sponsor_status(company, title, desc, location)
        work_arr = job.get("work_arrangement") or detect_work_arrangement(title, location, desc)
        full_desc = job.get("full_description")
        app_url = job.get("application_url")
        detail_at = now if full_desc else None

        try:
            conn.execute(
                "INSERT INTO jobs ("
                "url, title, company, salary, description, location, site, strategy, discovered_at, "
                "full_description, application_url, detail_scraped_at, fingerprint, sponsor_status, work_arrangement, crm_stage"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    url,
                    title,
                    company,
                    job.get("salary"),
                    job.get("description"),
                    location,
                    site,
                    strategy,
                    now,
                    full_desc,
                    app_url,
                    detail_at,
                    fp,
                    sponsor,
                    work_arr,
                    "discovered",
                ),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    conn.commit()
    return new, existing


def get_jobs_by_stage(
    conn: sqlite3.Connection | None = None,
    stage: str = "discovered",
    min_score: int | None = None,
    limit: int = 100,
) -> list[JobRecord]:
    """Fetch jobs filtered by pipeline stage.

    Args:
        conn: Database connection. Uses get_connection() if None.
        stage: One of "discovered", "enriched", "scored", "tailored", "applied", etc.
        min_score: Minimum fit_score filter (only relevant for scored+ stages).
        limit: Maximum number of rows to return. Values <= 0 mean "no explicit
            limit" and are clamped to ``_MAX_UNLIMITED_STAGE_ROWS`` so a single
            call can never materialize an unbounded ``SELECT *`` result set.

    Returns:
        List of job dicts.
    """
    if conn is None:
        conn = get_connection()

    conditions = {
        "discovered": "1=1",
        "pending_detail": "detail_scraped_at IS NULL",
        "enriched": "full_description IS NOT NULL",
        "pending_score": "full_description IS NOT NULL AND fit_score IS NULL",
        "scored": "fit_score IS NOT NULL",
        "pending_tailor": (
            "fit_score >= ? AND full_description IS NOT NULL "
            "AND tailored_resume_path IS NULL AND COALESCE(tailor_attempts, 0) < 5"
        ),
        "tailored": "tailored_resume_path IS NOT NULL",
        "pending_apply": (
            "tailored_resume_path IS NOT NULL AND applied_at IS NULL "
            "AND application_url IS NOT NULL"
        ),
        "applied": "applied_at IS NOT NULL",
    }

    where = conditions.get(stage, "1=1")
    params: list[Any] = []

    if "?" in where and min_score is not None:
        params.append(min_score)
    elif "?" in where:
        params.append(7)  # default min_score

    if min_score is not None and "fit_score" not in where and stage in ("scored", "tailored", "applied"):
        where += " AND fit_score >= ?"
        params.append(min_score)

    effective_limit = limit if limit > 0 else _MAX_UNLIMITED_STAGE_ROWS

    query = f"SELECT * FROM jobs WHERE {where} ORDER BY fit_score DESC NULLS LAST, discovered_at DESC"
    query += " LIMIT ?"
    params.append(effective_limit)

    rows = conn.execute(query, params).fetchall()
    if limit <= 0 and len(rows) >= effective_limit:
        log.warning(
            "get_jobs_by_stage(stage=%r) returned the %d-row safety cap for an unlimited "
            "query; additional matching rows were not fetched. Pass an explicit limit "
            "to raise this bound.",
            stage,
            effective_limit,
        )

    if rows:
        columns = rows[0].keys()
        return [dict(zip(columns, row)) for row in rows]
    return []
