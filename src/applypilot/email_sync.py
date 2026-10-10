"""Post-Application Lifecycle & Email Sync CRM (`applypilot sync-email`).

Classifies incoming recruiting emails (confirmation, online assessment,
interview invitation, offer, or rejection), matches them to tracked jobs in
SQLite by company/title/domain, transitions `crm_stage` automatically, and
auto-generates an Interview Prep Dossier when an interview invite arrives.
"""

from __future__ import annotations

from datetime import UTC, datetime
import email
from email.header import decode_header
import imaplib
import json
import os
import re
import sqlite3
from typing import Any, TypedDict

from applypilot import config
from applypilot.database import close_connection, ensure_columns, init_db
from applypilot.prep import generate_interview_prep

class EmailClassification(TypedDict):
    event_type: str
    crm_stage: str | None
    confidence: float
    matched_phrase: str | None


class EmailSyncSummary(TypedDict):
    scanned: int
    matched: int
    updated: int
    prep_generated: int
    events: list[dict[str, Any]]


OFFER_PATTERNS: tuple[str, ...] = (
    "pleased to offer",
    "offer of employment",
    "formal offer",
    "extend an offer",
    "employment offer",
)

INTERVIEW_PATTERNS: tuple[str, ...] = (
    "invitation to interview",
    "invite you to interview",
    "schedule an interview",
    "interview availability",
    "schedule a call",
    "phone screen",
    "introductory call",
    "first round interview",
    "technical interview",
    "panel interview",
)

ASSESSMENT_PATTERNS: tuple[str, ...] = (
    "online assessment",
    "coding assessment",
    "technical assessment",
    "case study",
    "hackerrank",
    "codility",
    "hirevue",
    "take-home",
)

REJECTION_PATTERNS: tuple[str, ...] = (
    "unfortunately",
    "not moving forward",
    "will not be proceeding",
    "decided not to progress",
    "pursue other candidates",
    "other candidates whose",
    "regret to inform",
    "not to move forward",
    "unsuccessful",
)

CONFIRMATION_PATTERNS: tuple[str, ...] = (
    "thank you for applying",
    "thanks for applying",
    "application received",
    "we have received your application",
    "we received your application",
    "application confirmation",
)

STAGE_PRIORITY: dict[str, int] = {
    "discovered": 0,
    "enriched": 1,
    "scored": 2,
    "tailored": 3,
    "ready": 4,
    "ready_to_apply": 4,
    "applied": 5,
    "assessment": 6,
    "interviewing": 7,
    "offer": 9,
    "rejected": 8,
}


def classify_application_email(subject: str, body: str, sender: str = "") -> EmailClassification:
    """Classify an email into a post-application CRM lifecycle event.

    Returns:
        Dict with `event_type`, `crm_stage`, `confidence`, and `matched_phrase`.
    """
    combined = f"{subject}\n{body}".lower()

    for phrase in OFFER_PATTERNS:
        if phrase in combined:
            return {
                "event_type": "offer",
                "crm_stage": "offer",
                "confidence": 0.95,
                "matched_phrase": phrase,
            }

    for phrase in INTERVIEW_PATTERNS:
        if phrase in combined:
            return {
                "event_type": "interview_invite",
                "crm_stage": "interviewing",
                "confidence": 0.92,
                "matched_phrase": phrase,
            }

    for phrase in ASSESSMENT_PATTERNS:
        if phrase in combined:
            return {
                "event_type": "assessment",
                "crm_stage": "assessment",
                "confidence": 0.90,
                "matched_phrase": phrase,
            }

    for phrase in REJECTION_PATTERNS:
        if phrase in combined:
            return {
                "event_type": "rejected",
                "crm_stage": "rejected",
                "confidence": 0.90,
                "matched_phrase": phrase,
            }

    for phrase in CONFIRMATION_PATTERNS:
        if phrase in combined:
            return {
                "event_type": "application_received",
                "crm_stage": "applied",
                "confidence": 0.85,
                "matched_phrase": phrase,
            }

    return {
        "event_type": "irrelevant",
        "crm_stage": None,
        "confidence": 0.0,
        "matched_phrase": None,
    }


def _normalize_company_token(name: str) -> str:
    clean = re.sub(
        r"\b(ltd|limited|plc|inc|corp|corporation|gmbh|ag|se|bv|nv|uk|group|bank|holdings)\b",
        "",
        (name or "").lower(),
    )
    return re.sub(r"[^a-z0-9]+", " ", clean).strip()


def match_email_to_job(
    conn: sqlite3.Connection,
    subject: str,
    body: str,
    sender: str = "",
) -> dict[str, Any] | None:
    """Match an email against tracked jobs in SQLite by company name and role title."""
    ensure_columns(conn)
    rows = conn.execute(
        "SELECT url, company, title FROM jobs "
        "ORDER BY COALESCE(fit_score, 0) DESC, discovered_at DESC"
    )

    haystack = f"{subject} {sender} {body}".lower()
    best_url: str | None = None
    best_score = 0

    for row in rows:
        company = (row["company"] or "").strip()
        title = (row["title"] or "").strip()
        norm_comp = _normalize_company_token(company)
        if not norm_comp or len(norm_comp) < 2:
            continue

        score = 0
        if norm_comp in haystack or company.lower() in haystack:
            score += 5
        elif norm_comp.replace(" ", "") in haystack.replace(" ", ""):
            score += 4

        if score == 0:
            continue

        if title and title.lower() in haystack:
            score += 4
        else:
            title_words = [w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 3]
            matched_words = sum(1 for w in title_words if w in haystack)
            score += min(3, matched_words)

        if score > best_score:
            best_score = score
            best_url = str(row["url"])

    if best_url is None:
        return None
    best_row = conn.execute("SELECT * FROM jobs WHERE url = ?", (best_url,)).fetchone()
    return dict(best_row) if best_row is not None else None


def _decode_mime_payload(payload: object) -> str:
    """Decode a MIME payload when it is bytes or text; ignore nested message objects."""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        return payload
    return ""


def fetch_imap_messages(limit: int = 30) -> list[dict[str, str]]:
    """Fetch recent recruiting emails via IMAP if IMAP_USER and IMAP_PASSWORD are set."""
    config.load_env()
    host = os.environ.get("IMAP_HOST", "imap.gmail.com")
    user = os.environ.get("IMAP_USER") or os.environ.get("GMAIL_ADDRESS", "")
    password = os.environ.get("IMAP_PASSWORD") or os.environ.get("GMAIL_APP_PASSWORD", "")
    if not user or not password:
        return []

    messages: list[dict[str, str]] = []
    try:
        with imaplib.IMAP4_SSL(host) as mail:
            mail.login(user, password)
            mail.select("INBOX", readonly=True)
            status, data = mail.search(None, "ALL")
            if status != "OK" or not data or not data[0]:
                return []
            msg_ids = data[0].split()[-limit:]
            for mid in reversed(msg_ids):
                res, msg_data = mail.fetch(mid, "(RFC822)")
                if res != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                raw_subj = msg.get("Subject", "")
                decoded_parts = decode_header(raw_subj)
                subj = "".join(
                    (part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else str(part))
                    for part, enc in decoded_parts
                )
                sender = str(msg.get("From", ""))
                body_text = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body_text += _decode_mime_payload(part.get_payload(decode=True))
                else:
                    body_text = _decode_mime_payload(msg.get_payload(decode=True))
                messages.append({"subject": subj, "sender": sender, "body": body_text[:4000]})
    except Exception:
        return []
    return messages


def sync_application_emails(
    messages: list[dict[str, Any]] | None = None,
    inbox_file: str | None = None,
    auto_prep_on_interview: bool = True,
    conn: sqlite3.Connection | None = None,
) -> EmailSyncSummary:
    """Process recruiting emails and update job CRM stages and interview prep packs.

    Args:
        messages: Optional list of `{"subject", "body", "sender"}` dicts.
        inbox_file: Optional path to a JSON file containing email objects.
        auto_prep_on_interview: Automatically generate Interview Prep Dossier on interview/assessment invite.
        conn: Optional SQLite connection.

    Returns:
        Summary dictionary with counts and updated job records.
    """
    if conn is None:
        conn = init_db()
        own_conn = True
    else:
        ensure_columns(conn)
        own_conn = False

    try:
        if messages is None:
            if inbox_file and os.path.exists(inbox_file):
                with open(inbox_file, encoding="utf-8") as f:
                    loaded = json.load(f)
                    messages = loaded if isinstance(loaded, list) else loaded.get("messages", [])
            elif (config.APP_DIR / "inbox_sync.json").exists():
                loaded = json.loads((config.APP_DIR / "inbox_sync.json").read_text(encoding="utf-8"))
                messages = loaded if isinstance(loaded, list) else loaded.get("messages", [])
            else:
                messages = fetch_imap_messages()

        summary: EmailSyncSummary = {
            "scanned": len(messages or []),
            "matched": 0,
            "updated": 0,
            "prep_generated": 0,
            "events": [],
        }

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        for msg in messages or []:
            if not isinstance(msg, dict):
                continue
            subject = str(msg.get("subject") or "")
            body = str(msg.get("body") or "")
            sender = str(msg.get("sender") or "")

            classification = classify_application_email(subject, body, sender)
            target_stage = classification.get("crm_stage")
            if not target_stage:
                continue

            job = match_email_to_job(conn, subject, body, sender)
            if not job:
                continue

            summary["matched"] += 1
            current_stage = (job.get("crm_stage") or "discovered").lower()
            cur_prio = STAGE_PRIORITY.get(current_stage, 0)
            new_prio = STAGE_PRIORITY.get(target_stage, 0)

            # Avoid downgrading e.g. 'interviewing' back to 'applied' from an old confirmation email
            final_stage = target_stage if new_prio >= cur_prio else current_stage
            existing_notes = (job.get("user_notes") or "").strip()
            note_line = f"[Email Sync {today}] {classification['event_type']}: {subject[:90]}"
            updated_notes = f"{existing_notes}\n{note_line}".strip() if note_line not in existing_notes else existing_notes

            if final_stage == "applied" and (job.get("apply_status") or "") not in ("applied", "done"):
                conn.execute(
                    "UPDATE jobs SET crm_stage = ?, user_notes = ?, apply_status = 'applied', applied_at = COALESCE(applied_at, CURRENT_TIMESTAMP) WHERE url = ?",
                    (final_stage, updated_notes, job["url"]),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET crm_stage = ?, user_notes = ? WHERE url = ?",
                    (final_stage, updated_notes, job["url"]),
                )
            conn.commit()
            summary["updated"] += 1

            prep_path_str = job.get("prep_pack_path")
            if auto_prep_on_interview and target_stage in ("interviewing", "assessment") and not prep_path_str:
                try:
                    _, p_path = generate_interview_prep(job, use_llm=False)
                    prep_path_str = str(p_path)
                    summary["prep_generated"] += 1
                except Exception:
                    pass

            summary["events"].append(
                {
                    "url": job["url"],
                    "company": job.get("company"),
                    "title": job.get("title"),
                    "event_type": classification["event_type"],
                    "crm_stage": final_stage,
                    "subject": subject,
                    "prep_pack_path": prep_path_str,
                }
            )

        return summary
    finally:
        if own_conn:
            close_connection()
