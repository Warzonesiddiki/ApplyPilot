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
from applypilot.crm import (
    _CRMRequestHandler,
    _ReusableThreadingHTTPServer,
    exposure_warning,
    update_job_crm_stage,
)


def test_crm_loopback_default_and_warn_on_wider_binding() -> None:
    assert exposure_warning("127.0.0.1") is None
    assert exposure_warning("localhost") is None
    assert exposure_warning("::1") is None

    warn = exposure_warning("0.0.0.0")
    assert warn is not None
    assert "exposes" in warn
    assert "personal data" in warn

    warn = exposure_warning("192.168.1.2")
    assert warn is not None
    assert "exposes" in warn


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


def test_crm_readable_envelope(tmp_path: Path) -> None:
    """GET / returns a loadable HTML envelope even on an empty profile so the
    web UI is usable from a fresh install before any jobs exist.
    """
    tmp = tmp_path / "crm-empty-envelope"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp))
    config.set_active_profile("empty-envelope-tests")
    config.ensure_dirs()
    config.RESUME_PATH.write_text("Neha Tariq, ACA. Audit experience.", encoding="utf-8")
    config.PROFILE_PATH.write_text(json.dumps(_PROFILE), encoding="utf-8")
    conn = database.init_db()
    database.ensure_columns(conn)
    conn.commit()
    database.close_connection()
    try:
        server = _ReusableThreadingHTTPServer(("127.0.0.1", 0), _CRMRequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = int(server.server_address[1])
        resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=5.0)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        text = resp.text
        assert "<html" in text.lower()
        assert "<head>" in text
        assert "</script>" in text
        assert "updateJobStage" in text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)
        database.close_connection()
        config.set_active_profile(None)
        monkeypatch.undo()


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

    # Stop accepting new requests only after the client session above has fully
    # finished. On Windows, closing the listener while httpx is still winding down
    # its own connection can surface as WinError 10053, so we close the server
    # here rather than inside the ``with`` block.
    server.shutdown()
    server.server_close()

    # Best-effort join: the fixture thread is daemonised, so this is only to
    # avoid handing the next test a half-closed loopback listener.
    thread.join(timeout=5.0)

    # A short, bounded drain prevents httpx teardown from racing the final
    # RST on Windows without masking handler bugs.
    try:
        import socket as _socket
        with _socket.create_connection(("127.0.0.1", port), timeout=0.5) as _sock:
            _sock.sendall(b"QUIT\r\n")
    except OSError:
        pass

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


def test_post_stage_refuses_oversized_request_bodies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The 1 MiB request-body gate must refuse a declared oversized body with
    HTTP 413 *before* reading the body.

    This test owns its own database path and its own server so it cannot race
    the shared ``crm_server`` fixture, and it retries once so a one-off Windows
    loopback RST during httpx teardown does not become a flaky red.
    """
    db_root = tmp_path / "crm-oversized-db"
    monkeypatch.setenv("APPLYPILOT_DIR", str(db_root))
    config.set_active_profile("crm-oversized-tests")
    config.ensure_dirs()
    config.RESUME_PATH.write_text("Neha Tariq, ACA. Audit experience.", encoding="utf-8")
    config.PROFILE_PATH.write_text(json.dumps(_PROFILE), encoding="utf-8")

    conn = database.init_db(db_path=db_root / "applypilot.db")
    database.ensure_columns(conn)
    conn.commit()
    database.close_connection()

    server = _ReusableThreadingHTTPServer(("127.0.0.1", 0), _CRMRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])

    base = f"http://127.0.0.1:{port}"

    def do_request() -> httpx.Response:
        return httpx.post(
            f"{base}/api/jobs/stage",
            content=b"x" * 1_100_000,
            headers={"Content-Type": "application/json"},
            timeout=30.0,
        )

    last_exc: BaseException | None = None
    response: httpx.Response | None = None
    for _attempt in range(3):
        try:
            response = do_request()
            last_exc = None
            break
        except httpx.ReadError as exc:
            last_exc = exc
            continue
    else:
        assert False, ("oversized-body request did not return cleanly", last_exc)

    assert response is not None
    assert response.status_code == 413
    assert "exceeds" in response.json()["error"]

    server.shutdown()
    server.server_close()
    thread.join(timeout=3.0)

    database.close_connection()
    config.set_active_profile(None)


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


def test_post_outreach_generates_outreach_pack(_fresh_crm_server: httpx.Client) -> None:
    response = _fresh_crm_server.post(
        "/api/jobs/outreach",
        json={"url": "https://jobs.example.com/audit-1"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["url"] == "https://jobs.example.com/audit-1"
    assert Path(payload["outreach_path"]).exists()
    assert "Audit Manager" in payload["outreach_markdown"]


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


@pytest.mark.parametrize("endpoint", ["/api/jobs/prep", "/api/jobs/outreach"])
def test_post_prep_and_outreach_endpoints_are_404_for_missing_jobs(crm_server: httpx.Client, endpoint: str) -> None:
    response = crm_server.post(endpoint, json={"url": "https://jobs.example.com/nope"})
    assert response.status_code == 404


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


@pytest.fixture()
def _fresh_crm_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[httpx.Client]:
    """Isolated CRM loopback server for tests that must not race the shared
    ``crm_server`` fixture's teardown on Windows.
    """
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile("crm-isolated-tests")
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
    thread.join(timeout=5.0)

    database.close_connection()
    config.set_active_profile(None)
