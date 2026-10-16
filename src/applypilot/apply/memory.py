"""Persistent Screening Question Memory Bank & Smart Apply Failure Classifier.

Stores canonical answers to recurring ATS screening questions (notice period,
UK/EU right-to-work, visa sponsorship, multi-currency salary expectations,
ACA/ACCA qualification status) in SQLite and classifies failed applications
into actionable buckets so transient failures can be retried cleanly.
"""

from __future__ import annotations

from datetime import UTC, datetime
import re
import sqlite3
from typing import Any

from applypilot import config
from applypilot.database import get_connection, init_db


def _ensure_table(conn: sqlite3.Connection) -> None:
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


def seed_question_bank_from_profile(
    conn: sqlite3.Connection | None = None,
    profile: dict[str, Any] | None = None,
) -> int:
    """Seed canonical screening questions from the active profile's `profile.json`."""
    if conn is None:
        conn = init_db()
    _ensure_table(conn)

    if profile is None:
        try:
            profile = config.load_profile()
        except Exception:
            profile = {}

    wa = profile.get("work_authorization", {})
    comp = profile.get("compensation", {})
    currencies = comp.get("currencies", {})
    exp = profile.get("experience", {})
    facts = profile.get("resume_facts", {})
    certs = facts.get("certifications", [])

    gbp_exp = currencies.get("GBP", {}).get("expectation") or comp.get("salary_expectation", "65000")
    eur_exp = currencies.get("EUR", {}).get("expectation") or comp.get("salary_expectation", "75000")

    defaults = [
        (
            "notice_period",
            "What is your notice period / earliest available start date?",
            str(wa.get("notice_period") or "1 Month (Immediately negotiable)"),
            "availability",
        ),
        (
            "willing_to_relocate",
            "Are you willing to relocate to the UK / EU for this role?",
            str(wa.get("willing_to_relocate") or "Yes"),
            "relocation",
        ),
        (
            "visa_sponsorship",
            "Will you now or in the future require visa sponsorship (Skilled Worker / EU Blue Card / Critical Skills Permit)?",
            str(wa.get("require_sponsorship") or "Yes"),
            "work_authorization",
        ),
        (
            "visa_sponsorship_details",
            "Please provide details regarding your work authorization or visa sponsorship status.",
            str(
                wa.get("visa_sponsorship_details")
                or "Eligible for UK Skilled Worker Visa, Ireland Critical Skills Permit, and EU Blue Card sponsorship."
            ),
            "work_authorization",
        ),
        (
            "salary_expectation_gbp",
            "What are your annual gross salary expectations in GBP (£)?",
            f"£{gbp_exp}",
            "compensation",
        ),
        (
            "salary_expectation_eur",
            "What are your annual gross salary expectations in EUR (€)?",
            f"€{eur_exp}",
            "compensation",
        ),
        (
            "years_of_experience",
            "How many years of relevant professional experience do you have?",
            str(exp.get("years") or "6"),
            "experience",
        ),
        (
            "professional_qualification",
            "What professional qualifications or memberships do you hold (e.g., ACA, ACCA, ICAEW, Degree)?",
            " | ".join(str(c) for c in certs) if certs else str(exp.get("education_level") or "Bachelor's Degree"),
            "qualifications",
        ),
    ]

    now = datetime.now(UTC).isoformat()
    inserted = 0
    for key, q_text, ans, cat in defaults:
        cur = conn.execute(
            "INSERT OR IGNORE INTO question_bank (question_key, question_text, canonical_answer, category, times_used, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (key, q_text, ans, cat, now),
        )
        if cur.rowcount > 0:
            inserted += 1

    conn.commit()
    return inserted


def upsert_question(
    question_key: str,
    question_text: str,
    canonical_answer: str,
    category: str = "custom",
    conn: sqlite3.Connection | None = None,
) -> None:
    """Insert or update a canonical screening question answer."""
    if conn is None:
        conn = init_db()
    _ensure_table(conn)
    clean_key = re.sub(r"[^a-z0-9_]+", "_", question_key.strip().lower()).strip("_") or "custom_question"
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO question_bank (question_key, question_text, canonical_answer, category, times_used, updated_at)
        VALUES (?, ?, ?, ?, 1, ?)
        ON CONFLICT(question_key) DO UPDATE SET
            question_text = excluded.question_text,
            canonical_answer = excluded.canonical_answer,
            category = excluded.category,
            times_used = question_bank.times_used + 1,
            updated_at = excluded.updated_at
        """,
        (clean_key, question_text.strip(), canonical_answer.strip(), category.strip(), now),
    )
    conn.commit()


def list_questions(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Return all canonical screening questions ordered by category and key."""
    if conn is None:
        conn = init_db()
    _ensure_table(conn)
    seed_question_bank_from_profile(conn=conn)
    rows = conn.execute(
        "SELECT question_key, question_text, canonical_answer, category, times_used, updated_at "
        "FROM question_bank ORDER BY category, question_key"
    ).fetchall()
    return [dict(zip(r.keys(), r)) for r in rows]


def build_memory_prompt_section(
    conn: sqlite3.Connection | None = None,
    profile: dict[str, Any] | None = None,
) -> str:
    """Format canonical screening answers for injection into the Auto-Apply prompt."""
    try:
        items = list_questions(conn=conn)
    except Exception:
        return ""
    if not items:
        return ""

    lines = [
        "## CANONICAL SCREENING QUESTION MEMORY BANK:",
        "When an application form asks any of these recurring questions, use the exact canonical answer below:",
    ]
    for item in items:
        lines.append(f"- **{item['question_text']}** -> `{item['canonical_answer']}`")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Smart Apply Error Classifier & Transient Retry
# ---------------------------------------------------------------------------

# Transient failures: the job is still open and worth retrying. These free-text
# markers are matched anywhere in `apply_error` because they are exception prose.
_RETRYABLE_MARKERS: tuple[str, ...] = (
    "timeout", "timed out", "network", "connection", "rate_limit", "rate limit",
    "429", "502", "503", "504", "crash", "crashed", "detached", "navigation",
    "in_progress", "protocol error", "closed", "expired", "filled", "token",
    "session", "browser closed", "target closed", "no result", "interrupted",
)

# Permanent failures are only recognised from the structured reason codes the apply
# agent emits (RESULT:FAILED:<code>) or from an exact standalone code. Bare
# substrings such as "closed"/"expired"/"filled"/"404" used to live here, which made
# "connection closed by peer", "token expired" and "target closed" permanent and
# stopped `--retry-transient` from ever retrying them.
_PERMANENT_REASON_PREFIX_RE = re.compile(
    r"^(?:"
    r"not_eligible"
    r"|no_sponsorship"
    r"|manual_ats"
    r"|site_blocked"
    r"|security_clearance"
    r"|not_a_job_application"
    r"|already_applied"
    r"|account_required"
    r"|unsafe_permissions|unsafe_verification"
    r"|sso_required"
    r"|no_longer_available"
    r")\w*"
)

# Standalone permanent codes. These are matched whole so free text that merely
# contains the word (e.g. "connection closed by peer") stays retryable.
_PERMANENT_EXACT_REASONS: frozenset[str] = frozenset({
    "expired", "closed", "filled", "404", "http_404", "http 404",
    "manual ats", "no longer available", "no longer accepting",
    "no longer accepting applications", "position filled",
})

_RESULT_ENVELOPE_PREFIXES: tuple[str, ...] = ("result:failed:", "result:failed", "result:", "failed:")


def _reason_code(error_str: str) -> str:
    """Strip the agent's `RESULT:` envelope to expose the bare reason code."""
    lowered = error_str.strip().lower()
    for prefix in _RESULT_ENVELOPE_PREFIXES:
        if lowered.startswith(prefix):
            return lowered[len(prefix) :].strip()
    return lowered


def classify_apply_error(error_str: str | None) -> str:
    """Classify an `apply_error` string into an actionable bucket.

    Buckets:
      - 'retryable': timeout, network, rate limit, browser crash, element detached,
        closed/expired *transport* failures ("connection closed by peer",
        "token expired", "target closed")
      - 'captcha_blocked': captcha, turnstile, recaptcha, hcaptcha, cloudflare
      - 'auth_or_sso': sso, verification code, login required, account locked
      - 'permanent_ineligible': a structured permanent code only — not_eligible*,
        expired, closed, filled, 404, no_sponsorship, manual_ats, site_blocked
      - 'other': uncategorized failure

    Permanent detection is anchored to the reason code the agent emits, so it can
    never shadow the transient browser/network failures a retry exists for.
    """
    if not error_str:
        return "other"
    err = error_str.lower()
    reason = _reason_code(error_str)

    if any(k in reason for k in ("captcha", "turnstile", "recaptcha", "hcaptcha", "cloudflare", "bot_detected")):
        return "captcha_blocked"

    if any(k in reason for k in ("sso", "verification_code", "login_required", "account_locked", "otp", "two_factor")):
        return "auth_or_sso"

    if _PERMANENT_REASON_PREFIX_RE.match(reason) or reason in _PERMANENT_EXACT_REASONS:
        return "permanent_ineligible"

    if any(k in err for k in _RETRYABLE_MARKERS):
        return "retryable"

    return "other"


def reset_transient_failed_jobs(
    include_captcha: bool = False,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Reset failed jobs whose `apply_error` is classified as transient/retryable.

    Preserves `permanent_ineligible` failures so closed or ineligible roles are
    never re-attempted. Every column a previous attempt owned is cleared, not just
    `apply_error`/`apply_status`/`apply_attempts`, so a reset job carries no stale
    agent lock into the next run.
    """
    if conn is None:
        conn = get_connection()

    allowed_buckets = {"retryable"}
    if include_captcha:
        allowed_buckets.add("captcha_blocked")

    checked = 0
    reset_count = 0
    by_bucket: dict[str, int] = {}
    last_rowid = 0

    reset_sql = _build_reset_sql(conn)

    # Scan failed jobs in bounded rowid windows: the URL/error pairs are small,
    # but a large failed backlog should not be materialized in one result set.
    while True:
        rows = conn.execute(
            "SELECT rowid, url, apply_error FROM jobs "
            "WHERE applied_at IS NULL AND apply_error IS NOT NULL AND rowid > ? "
            "ORDER BY rowid LIMIT 500",
            (last_rowid,),
        ).fetchall()
        if not rows:
            break

        for row in rows:
            last_rowid = int(row[0])
            checked += 1
            bucket = classify_apply_error(str(row[2]))
            by_bucket[bucket] = by_bucket.get(bucket, 0) + 1
            if bucket in allowed_buckets:
                conn.execute(reset_sql, (row[1],))
                reset_count += 1
        conn.commit()

    return {
        "checked": checked,
        "reset_count": reset_count,
        "by_bucket": by_bucket,
    }


# Columns a completed attempt owns. They are cleared together on reset so a job
# cannot stay locked by a previous run.
_RESETTABLE_LOCK_COLUMNS: tuple[str, ...] = ("agent_id", "apply_task_id", "apply_duration_ms", "last_attempted_at")


def _build_reset_sql(conn: sqlite3.Connection) -> str:
    """Build the reset UPDATE, keeping only lock columns this schema actually has."""
    try:
        existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    except sqlite3.Error:  # pragma: no cover - defensive
        existing = set()
    columns = [name for name in _RESETTABLE_LOCK_COLUMNS if name in existing]
    assignments = ["apply_error = NULL", "apply_status = NULL", "apply_attempts = 0"]
    assignments.extend(f"{name} = NULL" for name in columns)
    return f"UPDATE jobs SET {', '.join(assignments)} WHERE url = ?"
