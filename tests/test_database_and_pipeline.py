"""Tests for SQLite schema, multi-profile database switching, and pipeline SQL queries."""

from __future__ import annotations

from pathlib import Path

from applypilot import config, database, pipeline


def test_database_schema_and_profile_switching(tmp_path: Path, monkeypatch) -> None:
    """Database initializes with company and pdf_generated_at columns and switches per profile."""
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))

    config.set_active_profile("tahir")
    conn_tahir = database.init_db()
    database.store_jobs(
        conn_tahir,
        jobs=[
            {
                "url": "https://jobs.example.co.uk/ai-engineer-1",
                "title": "Senior AI Engineer",
                "company": "DeepTech London",
                "location": "London, United Kingdom",
            }
        ],
        site="linkedin",
        strategy="jobspy",
    )

    row_t = conn_tahir.execute("SELECT title, company, pdf_generated_at FROM jobs").fetchone()
    assert row_t["title"] == "Senior AI Engineer"
    assert row_t["company"] == "DeepTech London"
    assert row_t["pdf_generated_at"] is None
    database.close_connection()

    # Switch to Neha profile and verify completely isolated DB
    config.set_active_profile("neha")
    conn_neha = database.init_db()
    count_neha = conn_neha.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert count_neha == 0

    database.store_jobs(
        conn_neha,
        jobs=[
            {
                "url": "https://jobs.example.co.uk/aca-audit-manager-1",
                "title": "Audit Manager (ACA)",
                "company": "PwC UK",
                "location": "London, United Kingdom",
            }
        ],
        site="ICAEW Jobs",
        strategy="smartextract",
    )
    row_n = conn_neha.execute("SELECT title, company FROM jobs").fetchone()
    assert row_n["title"] == "Audit Manager (ACA)"
    assert row_n["company"] == "PwC UK"
    database.close_connection()
    config.set_active_profile(None)


def test_pipeline_pdf_pending_sql_terminates(tmp_path: Path, monkeypatch) -> None:
    """_PENDING_SQL['pdf'] returns 0 once pdf_generated_at is set, preventing infinite stream loops."""
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile(None)
    conn = database.init_db()
    database.store_jobs(
        conn,
        jobs=[
            {
                "url": "https://jobs.example.eu/role-1",
                "title": "Financial Accountant (ACA)",
                "company": "ING Group",
            }
        ],
        site="ING Group",
        strategy="workday",
    )
    conn.execute("UPDATE jobs SET tailored_resume_path = '/tmp/resume.txt'")
    conn.commit()

    pending_before = pipeline._count_pending("pdf", min_score=7)
    assert pending_before == 1

    conn.execute("UPDATE jobs SET pdf_generated_at = '2026-10-10T12:00:00Z'")
    conn.commit()

    pending_after = pipeline._count_pending("pdf", min_score=7)
    assert pending_after == 0
    database.close_connection()
