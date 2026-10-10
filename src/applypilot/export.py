"""Multi-Format Pipeline Exporter (CSV, JSON, Markdown).

Exports discovered, scored, tailored, and applied jobs from the active profile's
SQLite database for external analysis, spreadsheet tracking, or reporting.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from pathlib import Path
import sqlite3
from typing import Any

from applypilot import config
from applypilot.database import ensure_columns, get_connection

EXPORT_COLUMNS: tuple[str, ...] = (
    "company",
    "title",
    "location",
    "fit_score",
    "ats_coverage_pct",
    "sponsor_status",
    "work_arrangement",
    "crm_stage",
    "site",
    "strategy",
    "salary",
    "url",
    "application_url",
    "tailored_resume_path",
    "docx_resume_path",
    "cover_letter_path",
    "prep_pack_path",
    "outreach_path",
    "applied_at",
    "user_notes",
)


def _export_where_clause(min_score: int, crm_stage: str | None) -> tuple[str, list[int | str]]:
    """Build the parameterized predicate shared by export reads and counts."""
    clauses = ["1=1"]
    params: list[int | str] = []
    if min_score > 0:
        clauses.append("COALESCE(fit_score, 0) >= ?")
        params.append(min_score)
    if crm_stage:
        clauses.append("COALESCE(crm_stage, 'discovered') = ?")
        params.append(crm_stage.strip().lower())
    return " AND ".join(clauses), params


def _iter_export_rows(
    conn: sqlite3.Connection,
    min_score: int = 0,
    crm_stage: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield filtered export rows from a live SQLite cursor."""
    ensure_columns(conn)
    where_sql, params = _export_where_clause(min_score, crm_stage)
    col_sql = ", ".join(EXPORT_COLUMNS)
    cursor = conn.execute(
        f"SELECT {col_sql} FROM jobs WHERE {where_sql} "
        "ORDER BY fit_score DESC NULLS LAST, discovered_at DESC",
        params,
    )
    for row in cursor:
        yield dict(zip(EXPORT_COLUMNS, row))


def fetch_export_rows(
    conn: sqlite3.Connection | None = None,
    min_score: int = 0,
    crm_stage: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch job rows matching export filters."""
    if conn is None:
        conn = get_connection()
    return list(_iter_export_rows(conn, min_score=min_score, crm_stage=crm_stage))


def export_jobs_csv_string(
    conn: sqlite3.Connection | None = None,
    min_score: int = 0,
    crm_stage: str | None = None,
) -> str:
    """Render filtered jobs as a UTF-8 CSV string."""
    if conn is None:
        conn = get_connection()
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(EXPORT_COLUMNS))
    writer.writeheader()
    for r in _iter_export_rows(conn, min_score=min_score, crm_stage=crm_stage):
        writer.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in EXPORT_COLUMNS})
    return buf.getvalue()


def export_jobs(
    fmt: str = "csv",
    output_path: Path | str | None = None,
    min_score: int = 0,
    crm_stage: str | None = None,
) -> Path:
    """Export jobs to a CSV, JSON, or Markdown file.

    Args:
        fmt: 'csv', 'json', or 'markdown' ('md').
        output_path: Optional file path. Defaults to `<APP_DIR>/exports/jobs.<ext>`.
        min_score: Minimum fit_score filter (0 = all jobs).
        crm_stage: Optional CRM stage filter.

    Returns:
        Path to the written export file.
    """
    config.ensure_dirs()
    clean_fmt = fmt.strip().lower()
    if clean_fmt == "markdown":
        clean_fmt = "md"
    if clean_fmt not in ("csv", "json", "md"):
        raise ValueError(f"Unsupported export format '{fmt}'. Choose csv, json, or md.")

    if output_path:
        out = Path(output_path)
    else:
        prof = config.get_active_profile() or "default"
        out = config.APP_DIR / f"applypilot_export_{prof}.{clean_fmt}"

    out.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection()

    if clean_fmt == "csv":
        with out.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(EXPORT_COLUMNS))
            writer.writeheader()
            for row in _iter_export_rows(conn, min_score=min_score, crm_stage=crm_stage):
                writer.writerow({key: ("" if row.get(key) is None else row.get(key)) for key in EXPORT_COLUMNS})
    elif clean_fmt == "json":
        with out.open("w", encoding="utf-8") as handle:
            handle.write("[")
            has_rows = False
            for row in _iter_export_rows(conn, min_score=min_score, crm_stage=crm_stage):
                handle.write(",\n" if has_rows else "\n")
                serialized = json.dumps(row, indent=2)
                handle.write("  " + serialized.replace("\n", "\n  "))
                has_rows = True
            if has_rows:
                handle.write("\n")
            handle.write("]\n")
    else:
        prof_name = config.get_active_profile() or "default"
        ensure_columns(conn)
        where_sql, params = _export_where_clause(min_score, crm_stage)
        count_row = conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE {where_sql}",
            params,
        ).fetchone()
        exported_count = int(count_row[0]) if count_row is not None else 0
        header_lines = [
            f"# ApplyPilot Pipeline Export ({prof_name})",
            "",
            f"Total Exported Jobs: **{exported_count}**",
            "",
            "| Score | ATS % | Company | Title | Location | Sponsor | Stage | URL |",
            "| :---: | :---: | :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
        with out.open("w", encoding="utf-8") as handle:
            handle.write("\n".join(header_lines) + "\n")
            for row in _iter_export_rows(conn, min_score=min_score, crm_stage=crm_stage):
                score_s = str(row.get("fit_score") if row.get("fit_score") is not None else "-")
                ats_s = f"{row['ats_coverage_pct']}%" if row.get("ats_coverage_pct") is not None else "-"
                comp = str(row.get("company") or row.get("site") or "-").replace("|", "/")
                title = str(row.get("title") or "-").replace("|", "/")
                loc = str(row.get("location") or "-").replace("|", "/")
                sponsor = str(row.get("sponsor_status") or "-")
                stage = str(row.get("crm_stage") or "discovered")
                url = str(row.get("url") or "")
                handle.write(
                    f"| {score_s} | {ats_s} | {comp} | {title} | {loc} | {sponsor} | {stage} | {url} |\n"
                )

    return out
