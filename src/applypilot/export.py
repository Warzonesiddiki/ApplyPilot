"""Multi-Format Pipeline Exporter (CSV, JSON, Markdown).

Exports discovered, scored, tailored, and applied jobs from the active profile's
SQLite database for external analysis, spreadsheet tracking, or reporting.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
import sqlite3

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


def fetch_export_rows(
    conn: sqlite3.Connection | None = None,
    min_score: int = 0,
    crm_stage: str | None = None,
) -> list[dict]:
    """Fetch job rows matching export filters."""
    if conn is None:
        conn = get_connection()
    ensure_columns(conn)

    clauses = ["1=1"]
    params: list = []
    if min_score > 0:
        clauses.append("COALESCE(fit_score, 0) >= ?")
        params.append(min_score)
    if crm_stage:
        clauses.append("COALESCE(crm_stage, 'discovered') = ?")
        params.append(crm_stage.strip().lower())

    where_sql = " AND ".join(clauses)
    col_sql = ", ".join(EXPORT_COLUMNS)
    rows = conn.execute(
        f"SELECT {col_sql} FROM jobs WHERE {where_sql} "
        "ORDER BY fit_score DESC NULLS LAST, discovered_at DESC",
        params,
    ).fetchall()
    return [dict(zip(EXPORT_COLUMNS, r)) for r in rows]


def export_jobs_csv_string(
    conn: sqlite3.Connection | None = None,
    min_score: int = 0,
    crm_stage: str | None = None,
) -> str:
    """Render filtered jobs as a UTF-8 CSV string."""
    rows = fetch_export_rows(conn=conn, min_score=min_score, crm_stage=crm_stage)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(EXPORT_COLUMNS))
    writer.writeheader()
    for r in rows:
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
    rows = fetch_export_rows(min_score=min_score, crm_stage=crm_stage)

    if clean_fmt == "csv":
        out.write_text(
            export_jobs_csv_string(min_score=min_score, crm_stage=crm_stage),
            encoding="utf-8",
        )
    elif clean_fmt == "json":
        out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    else:
        prof_name = config.get_active_profile() or "default"
        lines = [
            f"# ApplyPilot Pipeline Export ({prof_name})",
            "",
            f"Total Exported Jobs: **{len(rows)}**",
            "",
            "| Score | ATS % | Company | Title | Location | Sponsor | Stage | URL |",
            "| :---: | :---: | :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
        for r in rows:
            score_s = str(r.get("fit_score") if r.get("fit_score") is not None else "-")
            ats_s = f"{r['ats_coverage_pct']}%" if r.get("ats_coverage_pct") is not None else "-"
            comp = str(r.get("company") or r.get("site") or "-").replace("|", "/")
            title = str(r.get("title") or "-").replace("|", "/")
            loc = str(r.get("location") or "-").replace("|", "/")
            sp = str(r.get("sponsor_status") or "-")
            st = str(r.get("crm_stage") or "discovered")
            url = str(r.get("url") or "")
            lines.append(f"| {score_s} | {ats_s} | {comp} | {title} | {loc} | {sp} | {st} | {url} |")
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return out
