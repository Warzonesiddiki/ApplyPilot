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
    profile: dict | None = None,
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


def list_questions(conn: sqlite3.Connection | None = None) -> list[dict]:
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
    profile: dict | None = None,
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

def classify_apply_error(error_str: str | None) -> str:
    """Classify an `apply_error` string into an actionable bucket.

    Buckets:
      - 'retryable': timeout, network, rate limit, browser crash, element detached, 502/503
      - 'captcha_blocked': captcha, turnstile, recaptcha, hcaptcha, cloudflare
      - 'auth_or_sso': sso, verification code, login required, account locked
      - 'permanent_ineligible': not_eligible, expired, closed, no_sponsorship, manual_ats
      - 'other': uncategorized failure
    """
    if not error_str:
        return "other"
    err = error_str.lower()

    if any(
        k in err
        for k in (
            "not_eligible", "expired", "closed", "no_sponsorship", "manual_ats",
            "security_clearance", "filled", "404", "no longer available",
        )
    ):
        return "permanent_ineligible"

    if any(k in err for k in ("captcha", "turnstile", "recaptcha", "hcaptcha", "cloudflare", "bot_detected")):
        return "captcha_blocked"

    if any(k in err for k in ("sso", "verification_code", "login_required", "account_locked", "otp", "two_factor")):
        return "auth_or_sso"

    if any(
        k in err
        for k in (
            "timeout", "timed out", "network", "connection", "rate_limit", "429",
            "502", "503", "504", "crash", "detached", "navigation", "in_progress",
            "target closed", "protocol error",
        )
    ):
        return "retryable"

    return "other"


def reset_transient_failed_jobs(
    include_captcha: bool = False,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Reset failed jobs whose `apply_error` is classified as transient/retryable.

    Preserves `permanent_ineligible` failures so closed or ineligible roles are
    never re-attempted.
    """
    if conn is None:
        conn = get_connection()

    rows = conn.execute(
        "SELECT url, apply_error FROM jobs WHERE applied_at IS NULL AND apply_error IS NOT NULL"
    ).fetchall()

    allowed_buckets = {"retryable"}
    if include_captcha:
        allowed_buckets.add("captcha_blocked")

    reset_urls: list[str] = []
    by_bucket: dict[str, int] = {}

    for row in rows:
        url, err = row[0], row[1]
        bucket = classify_apply_error(err)
        by_bucket[bucket] = by_bucket.get(bucket, 0) + 1
        if bucket in allowed_buckets:
            reset_urls.append(url)

    for url in reset_urls:
        conn.execute(
            "UPDATE jobs SET apply_error = NULL, apply_status = NULL, apply_attempts = 0 WHERE url = ?",
            (url,),
        )
    conn.commit()

    return {
        "checked": len(rows),
        "reset_count": len(reset_urls),
        "by_bucket": by_bucket,
    }
