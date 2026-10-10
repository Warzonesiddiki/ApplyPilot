"""Career CRM server exercised over real loopback HTTP against a temp database."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

import applypilot.config as config
import applypilot.database as database
from applypilot.crm import _CRMRequestHandler, _ReusableThreadingHTTPServer, update_job_crm_stage

_PROFILE: dict[str, Any] = {
    "personal": {
        "full_name": "Neha Tariq",
        "email": "neha@example.com",
        "phone": "+44 7700 900123",
        "city": "London",
        "country": "United Kingdom",
        "linkedin_url": "https://linkedin.com/in/neha-tariq",
    },
    "experience": {
        "years": "8",
        "education_level": "ACA Chartered Accountant",
        "target_role": "Audit Manager",
    },
    "skills_boundary": {"Audit": ["IFRS", "UK GAAP", "External Audit"]},
}


@pytest.fixture()
def crm_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[httpx.Client]:
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile("crm-tests")
    config.ensure_dirs()
    config.RESUME_PATH.write_text("Neha Tariq, ACA. Audit experience.", encoding="utf-8")
    config.PROFILE_PATH.write_text(json.dumps(_PROFILE), encoding="utf-8")

    conn = database.init_db()
    database.ensure_columns(conn)
    conn.executemany(
        "INSERT INTO jobs (url, title, company, location, fit_score, full_description, "
        "salary, site, strategy, discovered_at, crm_stage) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "https://jobs.example.com/audit-1",
                "Audit Manager",
                "PwC UK",
                "Leeds, UK",
                9,
                "Statutory audit under UK GAAP and IFRS with group consolidations.",
                "£65,000",
                "ICAEW Jobs",
                "ats",
                "2026-10-10T10:00:00Z",
                "tailored",
            ),
            (
                "https://jobs.example.com/engineer-1",
                "Senior AI Engineer",
                "DeepTech London",
                "London, UK",
                8,
                "Build LLM pipelines for finance teams.",
                None,
                "indeed",
                "jobspy",
                "2026-10-10T11:00:00Z",
                "scored",
            ),
        ],
    )
    conn.commit()
    database.close_connection()

    server = _ReusableThreadingHTTPServer(("127.0.0.1", 0), _CRMRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])

    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30.0) as client:
        yield client

    server.shutdown()
    server.server_close()
    thread.join(timeout=10.0)
    database.close_connection()
    config.set_active_profile(None)


def _db_row(url: str) -> sqlite3.Row:
    conn = database.init_db()
    try:
        row = conn.execute(
            "SELECT crm_stage, user_notes, prep_pack_path FROM jobs WHERE url = ?", (url,)
        ).fetchone()
    finally:
        database.close_connection()
    assert row is not None
    return cast(sqlite3.Row, row)


def test_get_dashboard_serves_streamed_html_with_interactive_controls(crm_server: httpx.Client) -> None:
    response = crm_server.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "updateJobStage(" in response.text
    assert "PwC UK" in response.text
    assert "Audit Manager" in response.text


def test_get_jobs_api_returns_profiles_stats_and_rows(crm_server: httpx.Client) -> None:
    response = crm_server.get("/api/jobs")
    assert response.status_code == 200
    payload = response.json()
    assert payload["active_profile"] == "crm-tests"
    assert payload["stats"]["total"] == 2
    assert payload["stats"]["scored"] == 2
    urls = {job["url"] for job in payload["jobs"]}
    assert urls == {"https://jobs.example.com/audit-1", "https://jobs.example.com/engineer-1"}


def test_get_export_streams_csv_with_all_rows(crm_server: httpx.Client) -> None:
    response = crm_server.get("/api/export.csv")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]
    lines = response.text.splitlines()
    assert lines[0].startswith("company,title,location")
    assert sum(1 for line in lines if "https://jobs.example.com/" in line) == 2


def test_get_unknown_path_returns_json_404(crm_server: httpx.Client) -> None:
    response = crm_server.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.json() == {"error": "Not found"}


def test_post_stage_updates_the_database(crm_server: httpx.Client) -> None:
    response = crm_server.post(
        "/api/jobs/stage",
        json={"url": "https://jobs.example.com/audit-1", "crm_stage": "interviewing"},
    )
    assert response.status_code == 200
    assert response.json()["updated"] is True
    assert _db_row("https://jobs.example.com/audit-1")["crm_stage"] == "interviewing"


def test_post_stage_rejects_invalid_stage_and_missing_fields(crm_server: httpx.Client) -> None:
    invalid = crm_server.post(
        "/api/jobs/stage",
        json={"url": "https://jobs.example.com/audit-1", "crm_stage": "hired-forever"},
    )
    assert invalid.status_code == 400
    assert "Invalid CRM stage" in invalid.json()["error"]

    missing = crm_server.post("/api/jobs/stage", json={"url": ""})
    assert missing.status_code == 400
    assert missing.json()["error"] == "url and crm_stage are required"


def test_post_stage_reports_unknown_urls_without_error(crm_server: httpx.Client) -> None:
    response = crm_server.post(
        "/api/jobs/stage",
        json={"url": "https://jobs.example.com/not-there", "crm_stage": "applied"},
    )
    assert response.status_code == 200
    assert response.json()["updated"] is False


def test_post_stage_rejects_non_string_notes_and_non_object_bodies(crm_server: httpx.Client) -> None:
    non_string = crm_server.post(
        "/api/jobs/stage",
        json={"url": "https://jobs.example.com/audit-1", "crm_stage": "applied", "user_notes": {"a": 1}},
    )
    assert non_string.status_code == 400
    assert "user_notes must be a string" in non_string.json()["error"]

    array_body = crm_server.post(
        "/api/jobs/stage",
        content=b"[1, 2, 3]",
        headers={"Content-Type": "application/json"},
    )
    assert array_body.status_code == 400
    assert array_body.json()["error"] == "url and crm_stage are required"

    broken_json = crm_server.post(
        "/api/jobs/stage",
        content=b"definitely not json",
        headers={"Content-Type": "application/json"},
    )
    assert broken_json.status_code == 400


def test_post_stage_refuses_oversized_request_bodies(crm_server: httpx.Client) -> None:
    response = crm_server.post(
        "/api/jobs/stage",
        content=b"x" * 1_100_000,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert "exceeds" in response.json()["error"]


def test_post_notes_updates_the_database(crm_server: httpx.Client) -> None:
    response = crm_server.post(
        "/api/jobs/notes",
        json={"url": "https://jobs.example.com/engineer-1", "user_notes": "Recruiter pinged on LinkedIn"},
    )
    assert response.status_code == 200
    assert response.json()["updated"] is True
    assert _db_row("https://jobs.example.com/engineer-1")["user_notes"] == "Recruiter pinged on LinkedIn"

    missing = crm_server.post("/api/jobs/notes", json={"user_notes": "no url"})
    assert missing.status_code == 400


def test_post_prep_generates_deterministic_documents(crm_server: httpx.Client) -> None:
    response = crm_server.post(
        "/api/jobs/prep",
        json={"url": "https://jobs.example.com/audit-1"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["url"] == "https://jobs.example.com/audit-1"
    assert Path(payload["prep_path"]).exists()
    assert Path(payload["outreach_path"]).exists()
    assert "Audit Manager" in payload["prep_markdown"]

    unknown = crm_server.post("/api/jobs/prep", json={"url": "https://jobs.example.com/nope"})
    assert unknown.status_code == 404


def test_post_proof_builds_application_payload(crm_server: httpx.Client) -> None:
    response = crm_server.post(
        "/api/jobs/proof",
        json={"url": "https://jobs.example.com/audit-1"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["candidate_fields"]["full_name"] == "Neha Tariq"
    assert payload["candidate_fields"]["email"] == "neha@example.com"
    assert payload["job"]["company"] == "PwC UK"
    assert Path(payload["proof_path"]).exists()

    unknown = crm_server.post("/api/jobs/proof", json={"url": "https://jobs.example.com/nope"})
    assert unknown.status_code == 404


def test_post_sync_email_matches_jobs_and_updates_stages(crm_server: httpx.Client) -> None:
    response = crm_server.post(
        "/api/sync-email",
        json={
            "messages": [
                {
                    "subject": "Invitation to interview - Audit Manager role",
                    "sender": "recruiting@pwc.com",
                    "body": "Dear Neha, we would like to schedule an interview for the Audit Manager position at PwC UK.",
                },
                {
                    "subject": "Newsletter",
                    "sender": "news@random.example",
                    "body": "Nothing relevant here.",
                },
            ]
        },
    )
    assert response.status_code == 200
    summary = response.json()
    assert summary["scanned"] == 2
    assert summary["matched"] == 1
    assert summary["updated"] == 1
    assert _db_row("https://jobs.example.com/audit-1")["crm_stage"] == "interviewing"
    # Interview invitations auto-generate a prep dossier.
    assert _db_row("https://jobs.example.com/audit-1")["prep_pack_path"]


def test_post_unknown_path_returns_json_404(crm_server: httpx.Client) -> None:
    response = crm_server.post("/api/unknown", json={"anything": True})
    assert response.status_code == 404
    assert response.json() == {"error": "Endpoint not found"}


def test_update_job_crm_stage_validates_and_persists_notes() -> None:
    with pytest.raises(ValueError, match="Invalid CRM stage"):
        update_job_crm_stage("https://x", "definitely-not-a-stage")
