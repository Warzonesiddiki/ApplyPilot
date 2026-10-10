"""Tailor batch orchestration with a scripted LLM tailoring boundary."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import applypilot.config as config
import applypilot.database as database
import applypilot.scoring.tailor as tailor
from applypilot.scoring.tailor import run_tailoring

_PROFILE: dict[str, Any] = {
    "personal": {"full_name": "Tahir Siddique", "email": "tahir@example.com", "city": "Manchester"},
    "experience": {"target_role": "Financial Accountant"},
    "skills_boundary": {"Reporting": ["IFRS", "UK GAAP"]},
    "resume_facts": {"certifications": ["ACCA"]},
}


@pytest.fixture()
def tailor_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile("tailor-tests")
    config.ensure_dirs()
    config.RESUME_PATH.write_text("Tahir Siddique, ACCA. IFRS reporting experience.", encoding="utf-8")
    config.PROFILE_PATH.write_text(json.dumps(_PROFILE), encoding="utf-8")

    conn = database.init_db()
    database.ensure_columns(conn)
    conn.executemany(
        "INSERT INTO jobs (url, title, company, location, fit_score, full_description, crm_stage, "
        "site, strategy, discovered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "https://jobs.example.com/approved-1",
                "Financial Accountant",
                "ING Group",
                "Amsterdam, NL",
                9,
                "Group reporting under IFRS and local GAAP.",
                "scored",
                "workday",
                "workday_api",
                "2026-10-10T10:00:00Z",
            ),
            (
                "https://jobs.example.com/failed-1",
                "Finance Manager",
                "Wise",
                "London, UK",
                8,
                "FP&A and month-end close.",
                "scored",
                "workday",
                "workday_api",
                "2026-10-10T11:00:00Z",
            ),
            (
                "https://jobs.example.com/crash-1",
                "Controller",
                "Adyen",
                "Amsterdam, NL",
                7,
                "Consolidations and technical accounting.",
                "scored",
                "workday",
                "workday_api",
                "2026-10-10T12:00:00Z",
            ),
        ],
    )
    conn.commit()
    database.close_connection()
    yield tmp_path
    database.close_connection()
    config.set_active_profile(None)


def _fake_tailor_resume(resume_text: str, job: dict[str, Any], profile: dict[str, Any],
                        max_retries: int = 3, validation_mode: str = "normal") -> tuple[str, dict[str, Any]]:
    url = str(job.get("url"))
    if url.endswith("approved-1"):
        return "TAILORED APPROVED RESUME", {"status": "approved", "attempts": 1, "validator": "passed"}
    if url.endswith("failed-1"):
        return "TAILORED FAILED RESUME", {"status": "failed_validation", "attempts": 2, "validator": "rejected"}
    raise RuntimeError("LLM exploded")


@pytest.fixture()
def scripted_tailor(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_select_best(
        job_description: str | None,
        default_resume_path: object | None = None,
        profile: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        calls.append({"kind": "select", "job_description": job_description})
        return "BASE RESUME TEXT", "default"

    def fake_coverage(
        job_description: str | None,
        tailored_resume: str | None,
        base_resume: str | None = None,
        profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {"coverage_pct": 77, "covered_keywords": ["IFRS"], "missing_keywords": []}

    def fake_docx(txt_path: Path, profile: dict[str, Any] | None = None) -> Path:
        out = Path(str(txt_path).replace(".txt", ".docx"))
        out.write_text("docx", encoding="utf-8")
        return out

    def fake_pdf(txt_path: Path) -> Path:
        out = Path(str(txt_path).replace(".txt", ".pdf"))
        out.write_text("pdf", encoding="utf-8")
        return out

    monkeypatch.setattr("applypilot.scoring.ats.select_best_base_resume", fake_select_best)
    monkeypatch.setattr("applypilot.scoring.ats.compute_ats_coverage", fake_coverage)
    monkeypatch.setattr(tailor, "tailor_resume", _fake_tailor_resume)
    monkeypatch.setattr("applypilot.scoring.docx_export.convert_to_docx", fake_docx)
    monkeypatch.setattr("applypilot.scoring.pdf.convert_to_pdf", fake_pdf)
    return calls


def test_run_tailoring_processes_approved_failed_and_crashed_jobs(
    tailor_env: Path,
    scripted_tailor: list[dict[str, Any]],
) -> None:
    summary = run_tailoring(min_score=7, limit=10)

    assert summary["approved"] == 1
    assert summary["failed"] == 1
    assert summary["errors"] == 1

    files = {path.name for path in config.TAILORED_DIR.iterdir()}
    # The approved and failed-validation jobs write artifacts; the crashed job writes none.
    assert len([name for name in files if name.endswith("_JOB.txt")]) == 2
    assert len([name for name in files if name.endswith("_REPORT.json")]) == 2
    assert any("ING" in name for name in files)

    conn = database.get_connection()
    rows = {
        row["url"]: row
        for row in conn.execute(
            "SELECT url, tailored_resume_path, tailor_attempts, crm_stage, ats_coverage_pct, "
            "pdf_generated_at FROM jobs"
        )
    }
    approved = rows["https://jobs.example.com/approved-1"]
    assert approved["tailored_resume_path"] is not None
    assert approved["crm_stage"] == "tailored"
    assert approved["tailor_attempts"] == 1
    assert approved["ats_coverage_pct"] == 77
    assert approved["pdf_generated_at"] is not None

    failed = rows["https://jobs.example.com/failed-1"]
    assert failed["tailored_resume_path"] is None
    assert failed["tailor_attempts"] == 1
    assert failed["crm_stage"] == "scored"

    crashed = rows["https://jobs.example.com/crash-1"]
    assert crashed["tailored_resume_path"] is None
    assert crashed["tailor_attempts"] == 1


def test_run_tailoring_reports_approved_report_contents(
    tailor_env: Path,
    scripted_tailor: list[dict[str, Any]],
) -> None:
    run_tailoring(min_score=7, limit=10)

    reports = list(config.TAILORED_DIR.glob("*_REPORT.json"))
    assert len(reports) == 2
    by_status: dict[str, dict[str, Any]] = {}
    for report_path in reports:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        by_status[str(report["status"])] = report
    assert by_status["approved"]["resume_variant"] == "default"
    assert by_status["approved"]["ats_coverage"]["coverage_pct"] == 77
    assert by_status["failed_validation"]["attempts"] == 2


def test_run_tailoring_no_pending_jobs_is_a_noop(
    tailor_env: Path,
    scripted_tailor: list[dict[str, Any]],
) -> None:
    summary = run_tailoring(min_score=10, limit=10)
    assert summary == {"approved": 0, "failed": 0, "errors": 0, "elapsed": 0.0}
    assert list(config.TAILORED_DIR.iterdir()) == []
