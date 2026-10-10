"""Tests for Interview Prep Dossier, LinkedIn/Recruiter Outreach, Screening Question Memory Bank,
Smart Transient Retry, Multi-Format Export, Career CRM Stage Manager, Multi-Provider LLM, and CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from typer.testing import CliRunner

from applypilot import config, database
from applypilot.apply.memory import (
    classify_apply_error,
    list_questions,
    reset_transient_failed_jobs,
    upsert_question,
)
from applypilot.cli import app
from applypilot.crm import update_job_crm_stage
from applypilot.export import export_jobs
from applypilot.llm import _detect_all_providers, get_telemetry, reset_telemetry
from applypilot.prep import generate_interview_prep, generate_outreach_pack
from applypilot.view import generate_dashboard

runner = CliRunner()


def test_interview_prep_and_outreach_pack_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """generate_interview_prep and generate_outreach_pack create fact-grounded dossiers and <300 char notes."""
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    res = runner.invoke(app, ["init", "-p", "neha", "-t", "neha"])
    assert res.exit_code == 0

    config.set_active_profile("neha")
    conn = database.init_db()
    database.store_jobs(
        conn,
        jobs=[
            {
                "url": "https://jobs.pwc.co.uk/audit-manager-99",
                "title": "Audit & Assurance Manager (ACA)",
                "company": "PwC UK",
                "location": "London, United Kingdom",
                "full_description": "Seeking an ACA Chartered Accountant with IFRS 15, IFRS 9, ISA statutory audit, SOX, and FP&A experience.",
            }
        ],
        site="PwC UK",
        strategy="workday_api",
    )
    conn.execute("UPDATE jobs SET fit_score = 9, ats_coverage_pct = 88 WHERE url = 'https://jobs.pwc.co.uk/audit-manager-99'")
    conn.commit()

    job = dict(conn.execute("SELECT * FROM jobs").fetchone())
    prep_md, prep_path = generate_interview_prep(job, use_llm=False)
    assert prep_path.exists()
    assert "Neha Tariq, ACA" in prep_md
    assert "KPMG" in prep_md
    assert "STAR" in prep_md

    out_md, out_path = generate_outreach_pack(job, use_llm=False)
    assert out_path.exists()
    assert "LinkedIn Connection Request Note" in out_md
    assert "/ 300 characters)" in out_md
    assert "PwC UK" in out_md

    row = conn.execute("SELECT prep_pack_path, outreach_path FROM jobs").fetchone()
    assert row["prep_pack_path"] == str(prep_path)
    assert row["outreach_path"] == str(out_path)
    database.close_connection()
    config.set_active_profile(None)


def test_screening_memory_bank_and_smart_transient_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Question bank seeds from profile, supports upserts, and smart retry preserves permanent failures."""
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    runner.invoke(app, ["init", "-p", "tahir", "-t", "tahir"])
    config.set_active_profile("tahir")
    conn = database.init_db()

    upsert_question("acca_papers", "How many ACCA exams have you passed?", "11 of 13 exams passed (ACCA Finalist)", "qualifications", conn=conn)
    questions = list_questions(conn=conn)
    keys = {q["question_key"]: q["canonical_answer"] for q in questions}
    assert keys["acca_papers"] == "11 of 13 exams passed (ACCA Finalist)"
    assert "notice_period" in keys

    # Classifier & transient retry test
    assert classify_apply_error("RESULT:FAILED:timeout waiting for selector") == "retryable"
    assert classify_apply_error("RESULT:FAILED:not_eligible_location") == "permanent_ineligible"
    assert classify_apply_error("RESULT:CAPTCHA") == "captcha_blocked"

    database.store_jobs(
        conn,
        jobs=[
            {"url": "https://example.com/job-timeout", "title": "Senior Accountant", "company": "Wise"},
            {"url": "https://example.com/job-ineligible", "title": "Accountant", "company": "Other"},
        ],
        site="Wise",
        strategy="workday",
    )
    conn.execute("UPDATE jobs SET apply_error = 'timeout on page load', apply_attempts = 2 WHERE url = 'https://example.com/job-timeout'")
    conn.execute("UPDATE jobs SET apply_error = 'not_eligible_location', apply_attempts = 1 WHERE url = 'https://example.com/job-ineligible'")
    conn.commit()

    retry_res = reset_transient_failed_jobs(conn=conn)
    assert retry_res["reset_count"] == 1

    r1 = conn.execute("SELECT apply_error, apply_attempts FROM jobs WHERE url = 'https://example.com/job-timeout'").fetchone()
    r2 = conn.execute("SELECT apply_error, apply_attempts FROM jobs WHERE url = 'https://example.com/job-ineligible'").fetchone()
    assert r1["apply_error"] is None
    assert r1["apply_attempts"] == 0
    assert r2["apply_error"] == "not_eligible_location"

    database.close_connection()
    config.set_active_profile(None)


def test_crm_stage_export_dashboard_and_cli_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CRM stage updates, multi-format export, dashboard generation, and CLI commands succeed end-to-end."""
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    runner.invoke(app, ["init", "-p", "neha", "-t", "neha"])
    config.set_active_profile("neha")
    conn = database.init_db()
    database.store_jobs(
        conn,
        jobs=[
            {
                "url": "https://jobs.barclays.co.uk/fpna-1",
                "title": "FP&A Manager (ACA)",
                "company": "Barclays",
                "location": "London, United Kingdom (Hybrid)",
                "full_description": "ACA qualified FP&A Manager role at Barclays in London.",
            }
        ],
        site="Barclays",
        strategy="workday_api",
    )
    conn.execute(
        "UPDATE jobs SET fit_score = 9, ats_coverage_pct = 92, "
        "score_breakdown = ? WHERE url = 'https://jobs.barclays.co.uk/fpna-1'",
        (json.dumps({"skills_score": 9, "qualifications_score": 10, "domain_score": 9, "location_score": 9}),),
    )
    conn.commit()

    assert update_job_crm_stage("https://jobs.barclays.co.uk/fpna-1", "interviewing", user_notes="Partner interview scheduled", conn=conn) is True

    csv_path = export_jobs(fmt="csv")
    json_path = export_jobs(fmt="json")
    md_path = export_jobs(fmt="markdown")
    assert csv_path.exists() and "Barclays" in csv_path.read_text(encoding="utf-8")
    assert json_path.exists() and "interviewing" in json_path.read_text(encoding="utf-8")
    exported_json = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(exported_json) == 1
    assert exported_json[0]["company"] == "Barclays"
    assert md_path.exists() and "92%" in md_path.read_text(encoding="utf-8")

    dash_path = Path(generate_dashboard())
    dash_html = dash_path.read_text(encoding="utf-8")
    assert "Barclays" in dash_html
    assert "ATS: 92%" in dash_html
    assert "Licensed UK/EU Sponsor" in dash_html
    assert "Skills: 9" in dash_html

    database.close_connection()

    # Test CLI subcommands via CliRunner
    r_run = runner.invoke(app, ["run", "-p", "neha", "--dry-run"])
    assert r_run.exit_code == 0
    assert "DRY RUN" in r_run.stdout

    r_prep = runner.invoke(app, ["prep", "Barclays", "--no-llm", "-p", "neha"])
    assert r_prep.exit_code == 0

    r_out = runner.invoke(app, ["outreach", "Barclays", "-p", "neha"])
    assert r_out.exit_code == 0

    r_stat = runner.invoke(app, ["status", "--all-profiles"])
    assert r_stat.exit_code == 0

    config.set_active_profile(None)


def test_multi_provider_llm_detection_and_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """_detect_all_providers detects Gemini, Anthropic, OpenAI, OpenRouter, Groq, and DeepSeek in order."""
    monkeypatch.setenv("GEMINI_API_KEY", "gem-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-test")
    monkeypatch.setenv("GROQ_API_KEY", "groq-test")
    monkeypatch.delenv("LLM_URL", raising=False)

    providers = _detect_all_providers()
    assert len(providers) >= 3
    assert "googleapis.com" in providers[0][0]
    assert "anthropic.com" in providers[1][0]
    assert any("groq.com" in p[0] for p in providers)

    reset_telemetry()
    tel = get_telemetry()
    assert tel["calls"] == 0


def test_email_sync_lifecycle_and_native_proof_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """sync_application_emails updates CRM stages & generates prep packs; native proof engine writes _PROOF.json."""
    from applypilot.apply.native_agent import extract_form_fields_from_html, prepare_native_applications
    from applypilot.email_sync import classify_application_email, sync_application_emails

    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    runner.invoke(app, ["init", "-p", "neha", "-t", "neha"])
    config.set_active_profile("neha")
    conn = database.init_db()
    database.store_jobs(
        conn,
        jobs=[
            {
                "url": "https://boards.greenhouse.io/monzo/jobs/777",
                "title": "Financial Reporting Manager (ACA)",
                "company": "Monzo Bank",
                "location": "London, United Kingdom",
                "full_description": "Lead IFRS 9/15/16 statutory financial reporting at Monzo in London.",
                "application_url": "https://boards.greenhouse.io/monzo/jobs/777#app",
            },
            {
                "url": "https://jobs.ashbyhq.com/deepl/888",
                "title": "Senior Group Accountant",
                "company": "DeepL",
                "location": "Berlin, Germany",
                "full_description": "Lead IFRS group consolidation across European entities at DeepL.",
                "application_url": "https://jobs.ashbyhq.com/deepl/888/application",
            },
        ],
        site="Greenhouse",
        strategy="ats_direct",
    )
    conn.execute("UPDATE jobs SET fit_score = 9")
    conn.commit()

    # 1. Test Native Application Proof Engine & HTML form extractor
    fields = extract_form_fields_from_html(
        '<form><input name="first_name" placeholder="First Name" required>'
        '<input type="email" name="email"><textarea name="notice_period" aria-label="What is your notice period?"></textarea></form>'
    )
    assert len(fields) == 3

    native_res = prepare_native_applications(min_score=8, limit=5, conn=conn)
    assert native_res["prepared"] == 2
    proof_file = Path(native_res["proof_paths"][0])
    assert proof_file.exists()
    proof_data = json.loads(proof_file.read_text(encoding="utf-8"))
    assert proof_data["candidate_fields"]["full_name"] == "Neha Tariq, ACA"
    assert len(proof_data["screening_answers"]) >= 5

    # 2. Test Post-Application Email Sync CRM
    assert classify_application_email("Invitation to interview at Monzo", "We would like to schedule a first round interview")["crm_stage"] == "interviewing"
    assert classify_application_email("Update on your DeepL application", "Unfortunately we will not be proceeding")["crm_stage"] == "rejected"

    inbox_json = tmp_path / "sample_inbox.json"
    inbox_json.write_text(
        json.dumps(
            [
                {
                    "subject": "Invitation to interview: Financial Reporting Manager (ACA) at Monzo Bank",
                    "sender": "recruiting@monzo.com",
                    "body": "Hi Neha, thank you for applying to Monzo Bank. We would love to schedule an interview with you this week.",
                },
                {
                    "subject": "Your application for Senior Group Accountant at DeepL",
                    "sender": "careers@deepl.com",
                    "body": "Dear Neha, unfortunately we have decided not to progress your application further.",
                },
            ]
        ),
        encoding="utf-8",
    )

    sync_res = sync_application_emails(inbox_file=str(inbox_json), auto_prep_on_interview=True, conn=conn)
    assert sync_res["matched"] == 2
    assert sync_res["updated"] == 2
    assert sync_res["prep_generated"] == 1

    monzo_row = conn.execute("SELECT crm_stage, prep_pack_path, user_notes FROM jobs WHERE company = 'Monzo Bank'").fetchone()
    deepl_row = conn.execute("SELECT crm_stage, user_notes FROM jobs WHERE company = 'DeepL'").fetchone()
    assert monzo_row["crm_stage"] == "interviewing"
    assert monzo_row["prep_pack_path"] is not None and Path(monzo_row["prep_pack_path"]).exists()
    assert "interview_invite" in (monzo_row["user_notes"] or "")
    assert deepl_row["crm_stage"] == "rejected"

    database.close_connection()

    # Verify CLI sync-email and apply --engine native commands
    r_sync = runner.invoke(app, ["sync-email", "-i", str(inbox_json), "-p", "neha"])
    assert r_sync.exit_code == 0
    assert "Email sync complete" in r_sync.stdout

    r_native = runner.invoke(app, ["apply", "--engine", "native", "-p", "neha"])
    assert r_native.exit_code == 0
    assert "Native Application Proof Engine" in r_native.stdout

    # Verify Daily Digest builder and Workday store_results metadata enrichment
    from applypilot.discovery.workday import store_results
    from applypilot.notifications import build_daily_digest

    config.set_active_profile("neha")
    conn = database.init_db()
    store_results(
        conn,
        jobs=[
            {
                "apply_url": "https://barclays.wd3.myworkdayjobs.com/job/909",
                "title": "Group Reporting Manager (ACA)",
                "location": "London, United Kingdom (Hybrid)",
                "full_description": "Lead Barclays group IFRS statutory reporting and external audit coordination in London." * 4,
                "employer_name": "Barclays",
            }
        ],
        employers={},
    )
    wd_row = conn.execute("SELECT fingerprint, sponsor_status, work_arrangement, crm_stage FROM jobs WHERE url = 'https://barclays.wd3.myworkdayjobs.com/job/909'").fetchone()
    assert wd_row["fingerprint"] is not None
    assert wd_row["sponsor_status"] == "licensed_sponsor"
    assert wd_row["work_arrangement"] == "hybrid"
    assert wd_row["crm_stage"] == "discovered"

    digest_text = build_daily_digest(min_score=8)
    assert "Monzo Bank" in digest_text
    assert "Top Matches" in digest_text

    database.close_connection()
    config.set_active_profile(None)




def test_reset_transient_failed_jobs_processes_backlogs_in_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed-job scans must cross the 500-row window boundary without losing rows."""
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile("reset-window-tests")
    conn = database.init_db()

    total = 1_200  # deliberately more than two 500-row windows
    conn.executemany(
        "INSERT INTO jobs (url, title, company, site, strategy, discovered_at, apply_error, apply_attempts) "
        "VALUES (?, ?, 'Bulk Corp', 'indeed', 'jobspy', '2026-10-10', ?, 1)",
        [
            (
                f"https://example.com/backlog-{index}",
                f"Role {index}",
                "RESULT:FAILED:timeout waiting for selector" if index % 2 == 0 else "not_eligible_location",
            )
            for index in range(total)
        ],
    )
    conn.commit()

    result = reset_transient_failed_jobs(conn=conn)

    assert result["checked"] == total
    assert result["reset_count"] == total // 2
    assert result["by_bucket"]["retryable"] == total // 2
    assert result["by_bucket"]["permanent_ineligible"] == total // 2
    remaining = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_error IS NOT NULL"
    ).fetchone()[0]
    assert remaining == total // 2

    database.close_connection()
    config.set_active_profile(None)
