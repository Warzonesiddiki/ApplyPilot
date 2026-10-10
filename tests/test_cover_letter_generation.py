"""Cover letter generation with a scripted LLM client and an isolated database."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import applypilot.config as config
import applypilot.database as database
import applypilot.scoring.cover_letter as cover_letter
from applypilot.scoring.cover_letter import generate_cover_letter, run_cover_letters

VALID_LETTER = """Dear Hiring Manager,

I led the migration of a group ledger close at ING to a 4-day cycle across 12 entities, using IFRS 9 and SQL reconciliation packs. That work maps directly onto the audit and reporting scope advertised.

At PwC UK I reviewed 40+ statutory audits under UK GAAP and IFRS, cutting review notes by a third with structured working papers.

Your Amsterdam team's IFRS 17 readiness programme is the exact work I want to discuss.

Regards,
Tahir Siddique, ACA"""

REJECTED_LETTER = """Here is the cover letter you asked for!

I am excited to apply — my proven track record of robust, scalable solutions speaks for itself.

Regards,
Tahir"""


class _ScriptedClient:
    """Minimal stand-in for the pooled LLMClient recording chat() calls."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages: list[dict[str, str]], max_tokens: int = 1024, temperature: float = 0.7) -> str:
        self.calls.append({"messages": messages, "max_tokens": max_tokens, "temperature": temperature})
        return self._replies.pop(0)


_PROFILE: dict[str, Any] = {
    "personal": {"full_name": "Tahir Siddique", "preferred_name": "Tahir"},
    "experience": {"target_role": "Audit Manager"},
    "skills_boundary": {"Audit": ["IFRS", "UK GAAP", "SQL"]},
    "resume_facts": {"certifications": ["ACA"], "real_metrics": ["4-day close"]},
}


@pytest.fixture()
def cover_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile("cover-tests")
    config.ensure_dirs()
    config.RESUME_PATH.write_text("Tahir Siddique, ACA. Auditor with IFRS experience.", encoding="utf-8")
    config.PROFILE_PATH.write_text(json.dumps(_PROFILE), encoding="utf-8")

    conn = database.init_db()
    database.ensure_columns(conn)
    conn.execute(
        "INSERT INTO jobs (url, title, company, location, fit_score, full_description, "
        "tailored_resume_path, site, strategy, discovered_at) VALUES "
        "('https://jobs.example.com/audit-1', 'Audit Manager', 'PwC UK', 'Leeds, UK', 8, "
        "'Statutory audit under UK GAAP and IFRS.', '/tmp/tailored.txt', 'ICAEW Jobs', 'ats', '2026-10-10')"
    )
    conn.commit()
    database.close_connection()
    yield None
    database.close_connection()
    config.set_active_profile(None)


def _install_client(monkeypatch: pytest.MonkeyPatch, replies: list[str]) -> _ScriptedClient:
    client = _ScriptedClient(replies)
    monkeypatch.setattr(cover_letter, "get_client", lambda: client)
    return client


def test_generate_cover_letter_accepts_valid_reply_and_strips_preamble(
    cover_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _install_client(monkeypatch, ["Sure thing!\n\n" + VALID_LETTER])

    letter = generate_cover_letter("resume text", {"company": "PwC UK", "title": "Audit Manager"}, _PROFILE)

    assert letter.startswith("Dear Hiring Manager,")
    assert "Tahir Siddique, ACA" in letter
    assert len(client.calls) == 1
    # The system prompt is profile-driven and forbids fabrication.
    assert "ACA" in client.calls[0]["messages"][0]["content"]


def test_generate_cover_letter_retries_then_accepts_a_fixed_reply(
    cover_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _install_client(monkeypatch, [REJECTED_LETTER, VALID_LETTER])

    letter = generate_cover_letter(
        "resume text", {"company": "PwC UK", "title": "Audit Manager"}, _PROFILE, max_retries=2
    )

    assert letter.startswith("Dear Hiring Manager,")
    assert len(client.calls) == 2
    # The retry prompt carries the validator feedback to avoid.
    assert "AVOID THESE ISSUES" in client.calls[1]["messages"][0]["content"]


def test_generate_cover_letter_gives_up_after_max_retries(
    cover_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _install_client(monkeypatch, [REJECTED_LETTER, REJECTED_LETTER])

    letter = generate_cover_letter(
        "resume text", {"company": "PwC UK", "title": "Audit Manager"}, _PROFILE, max_retries=1
    )

    assert len(client.calls) == 2
    assert "proven track record" in letter  # last attempt returned even though validation failed


def test_run_cover_letters_generates_files_and_updates_the_database(
    cover_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_client(monkeypatch, [VALID_LETTER])

    summary = run_cover_letters(min_score=7, limit=5)

    assert summary["generated"] == 1
    assert summary["errors"] == 0

    letters = list(config.COVER_LETTER_DIR.glob("*_CL.txt"))
    assert len(letters) == 1
    written = letters[0].read_text(encoding="utf-8")
    assert written.startswith("Dear Hiring Manager,")

    conn = database.get_connection()
    row = conn.execute(
        "SELECT cover_letter_path, cover_attempts, crm_stage FROM jobs WHERE url = 'https://jobs.example.com/audit-1'"
    ).fetchone()
    assert row["cover_letter_path"] == str(letters[0])
    assert row["cover_attempts"] == 1
    assert row["crm_stage"] == "ready"


def test_run_cover_letters_skips_exhausted_jobs(
    cover_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = database.get_connection()
    conn.execute("UPDATE jobs SET cover_attempts = ?", (cover_letter.MAX_ATTEMPTS,))
    conn.commit()

    _install_client(monkeypatch, [VALID_LETTER])
    summary = run_cover_letters(min_score=7, limit=5)

    assert summary["generated"] == 0
    assert list(config.COVER_LETTER_DIR.glob("*_CL.txt")) == []
