"""Native Python Auto-Apply & Pre-Submission Proof Packet Engine.

Provides a zero-Claude-Code-CLI native Python fallback for Tier 3 application
preparation and form filling. Inspects ATS forms (Greenhouse, Lever, Ashby,
Workable, generic HTML forms), maps candidate profile fields + learned
Screening Question Memory Bank (`question_bank`) answers, writes an auditable
JSON Proof Packet (`~/.applypilot/proofs/<job>_PROOF.json`), and optionally
drives Playwright to pre-fill forms and upload tailored PDF/DOCX resumes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html.parser import HTMLParser
import json
from pathlib import Path
import sqlite3
from typing import Any

import httpx

from applypilot import config
from applypilot.apply.memory import (
    list_questions,
    seed_question_bank_from_profile,
)
from applypilot.database import close_connection, ensure_columns, init_db
from applypilot.scoring.tailor import _build_file_prefix


class _FormFieldParser(HTMLParser):
    """Lightweight HTML parser that extracts form input, select, and textarea fields."""

    def __init__(self) -> None:
        super().__init__()
        self.fields: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ("input", "textarea", "select"):
            return
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        field_type = attr_map.get("type", tag).lower()
        if field_type in ("hidden", "submit", "button", "reset"):
            return
        name = attr_map.get("name") or attr_map.get("id") or attr_map.get("aria-label") or attr_map.get("placeholder") or ""
        label = attr_map.get("aria-label") or attr_map.get("placeholder") or name
        self.fields.append(
            {
                "tag": tag,
                "type": field_type,
                "name": name,
                "label": label,
                "required": "true" if "required" in attr_map else "false",
            }
        )


def extract_form_fields_from_html(html: str) -> list[dict[str, str]]:
    """Extract interactive form fields from an HTML string."""
    parser = _FormFieldParser()
    try:
        parser.feed(html or "")
    except Exception:
        pass
    return parser.fields


def build_application_payload(
    job: dict[str, Any],
    profile: dict[str, Any] | None = None,
    detected_fields: list[dict[str, str]] | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Build a complete, auditable application field payload and save a Proof Packet.

    Args:
        job: Job row dictionary from SQLite.
        profile: Candidate `profile.json` dictionary (loaded automatically if None).
        detected_fields: Optional list of form fields parsed from the target ATS page.
        conn: Optional SQLite connection for `question_bank` lookups.

    Returns:
        Proof packet dictionary (also saved to `<PROOFS_DIR>/<prefix>_PROOF.json`).
    """
    if conn is None:
        conn = init_db()
        own_conn = True
    else:
        ensure_columns(conn)
        own_conn = False

    try:
        if profile is None:
            profile = config.load_profile()
        config.ensure_dirs()

        personal = profile.get("personal", {})
        exp = profile.get("experience", {})

        full_name = str(personal.get("full_name") or "").strip()
        parts = full_name.split()
        first_name = personal.get("first_name") or (parts[0] if parts else "")
        last_name = personal.get("last_name") or (" ".join(parts[1:]) if len(parts) > 1 else "")

        standard_fields = {
            "full_name": full_name,
            "first_name": first_name,
            "last_name": last_name,
            "email": personal.get("email", ""),
            "phone": personal.get("phone", ""),
            "city": personal.get("city", ""),
            "country": personal.get("country", ""),
            "linkedin_url": personal.get("linkedin_url", ""),
            "github_url": personal.get("github_url", ""),
            "portfolio_url": personal.get("portfolio_url", ""),
            "years_experience": str(exp.get("years", "")),
            "education_level": str(exp.get("education_level", "")),
        }

        # Resolve tailored documents (prefer PDF, fallback to DOCX or TXT)
        tailored_txt = str(job.get("tailored_resume_path") or "")
        resume_pdf = tailored_txt[:-4] + ".pdf" if tailored_txt.endswith(".txt") else tailored_txt
        resume_docx = str(job.get("docx_resume_path") or (tailored_txt[:-4] + ".docx" if tailored_txt.endswith(".txt") else ""))
        cover_letter = str(job.get("cover_letter_path") or "")

        attachments = {
            "resume_pdf": resume_pdf if resume_pdf and Path(resume_pdf).exists() else tailored_txt,
            "resume_docx": resume_docx if resume_docx and Path(resume_docx).exists() else "",
            "cover_letter": cover_letter if cover_letter and Path(cover_letter).exists() else "",
        }

        # Seed default screening answers into memory bank if not present
        seed_question_bank_from_profile(conn=conn, profile=profile)
        saved_memory = list_questions(conn=conn)

        mapped_custom_fields: list[dict[str, str]] = []
        for field in detected_fields or []:
            label = (field.get("label") or field.get("name") or "").strip()
            if not label:
                continue
            low = label.lower()
            resolved_val = ""
            for std_k, std_v in standard_fields.items():
                if std_k.replace("_", " ") in low or std_k in low:
                    resolved_val = str(std_v)
                    break
            if not resolved_val:
                for mem_row in saved_memory:
                    q_key = (mem_row.get("question_key") or "").replace("_", " ").lower()
                    q_txt = (mem_row.get("question_text") or "").lower()
                    if (q_key and q_key in low) or any(w in low for w in ("notice", "sponsorship", "salary", "relocat", "authoriz", "qualific") if w in q_txt):
                        resolved_val = str(mem_row.get("canonical_answer") or "")
                        break
            mapped_custom_fields.append(
                {
                    "field_name": field.get("name", ""),
                    "label": label,
                    "type": field.get("type", "text"),
                    "resolved_value": resolved_val,
                }
            )

        proof = {
            "generated_at": datetime.now(UTC).isoformat(),
            "engine": "applypilot_native_v0.5",
            "job": {
                "url": job.get("url"),
                "application_url": job.get("application_url") or job.get("url"),
                "title": job.get("title"),
                "company": job.get("company") or job.get("site"),
                "location": job.get("location"),
                "fit_score": job.get("fit_score"),
                "sponsor_status": job.get("sponsor_status"),
            },
            "candidate_fields": standard_fields,
            "attachments": attachments,
            "screening_answers": {row["question_text"]: row["canonical_answer"] for row in saved_memory[:15]},
            "mapped_form_fields": mapped_custom_fields,
        }

        prefix = _build_file_prefix(job)
        proof_path = config.PROOFS_DIR / f"{prefix}_PROOF.json"
        proof_path.write_text(json.dumps(proof, indent=2), encoding="utf-8")
        proof["proof_path"] = str(proof_path)
        return proof
    finally:
        if own_conn:
            close_connection()


def prepare_native_applications(
    min_score: int = 7,
    limit: int = 10,
    fetch_live_forms: bool = False,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Generate Pre-Submission Proof Packets for high-scoring jobs without requiring Claude CLI."""
    if conn is None:
        conn = init_db()
        own_conn = True
    else:
        ensure_columns(conn)
        own_conn = False

    try:
        profile = config.load_profile()
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE COALESCE(fit_score, 0) >= ?
              AND COALESCE(apply_status, 'pending') NOT IN ('applied', 'done')
            ORDER BY fit_score DESC, discovered_at DESC
            LIMIT ?
            """,
            (min_score, limit),
        ).fetchall()

        proofs: list[dict[str, Any]] = []
        for r in rows:
            job = dict(zip(r.keys(), r))
            detected: list[dict[str, str]] = []
            target_url = job.get("application_url") or job.get("url")
            if fetch_live_forms and target_url:
                try:
                    resp = httpx.get(target_url, timeout=10.0, follow_redirects=True)
                    if resp.status_code == 200:
                        detected = extract_form_fields_from_html(resp.text)
                except Exception:
                    detected = []

            proof = build_application_payload(job, profile=profile, detected_fields=detected, conn=conn)
            conn.execute(
                "UPDATE jobs SET crm_stage = CASE WHEN COALESCE(crm_stage, 'discovered') IN ('discovered', 'scored', 'tailored') THEN 'ready_to_apply' ELSE crm_stage END WHERE url = ?",
                (job["url"],),
            )
            proofs.append(proof)

        conn.commit()
        return {
            "prepared": len(proofs),
            "proof_paths": [p["proof_path"] for p in proofs],
            "proofs": proofs,
        }
    finally:
        if own_conn:
            close_connection()
