"""Tests for Direct ATS API connectors (Greenhouse, Lever, Ashby, SmartRecruiters),
Cross-Board Fuzzy Deduplication, and UK/EU Visa Sponsorship Intelligence.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from applypilot import config, database
from applypilot.discovery.ats_direct import (
    parse_ashby_jobs,
    parse_greenhouse_jobs,
    parse_lever_jobs,
    parse_personio_jobs,
    parse_recruitee_jobs,
    parse_smartrecruiters_jobs,
    parse_workable_jobs,
    run_ats_direct_discovery,
)
from applypilot.enrichment.sponsors import (
    detect_sponsor_status,
    detect_work_arrangement,
    evaluate_sponsorship_and_metadata,
    is_job_expired,
)


def test_parse_greenhouse_lever_ashby_and_smartrecruiters() -> None:
    """All 4 direct ATS JSON parsers extract title, URL, location, and pre-enriched descriptions."""
    gh_payload = {
        "jobs": [
            {
                "id": 101,
                "title": "Senior Financial Accountant (ACA / ACCA)",
                "absolute_url": "https://boards.greenhouse.io/monzo/jobs/101",
                "location": {"name": "London, United Kingdom (Hybrid)"},
                "content": "<p>We are seeking an ACA or ACCA qualified Senior Financial Accountant with strong IFRS 9 and IFRS 15 statutory reporting experience across multi-entity banking groups in London.</p>",
            }
        ]
    }
    gh_jobs = parse_greenhouse_jobs(gh_payload, "Monzo", accept_locs=["london"])
    assert len(gh_jobs) == 1
    assert gh_jobs[0]["company"] == "Monzo"
    assert gh_jobs[0]["full_description"] is not None
    assert "IFRS 15" in gh_jobs[0]["full_description"]

    lever_payload = [
        {
            "text": "FP&A Manager",
            "hostedUrl": "https://jobs.lever.co/mistral/202",
            "applyUrl": "https://jobs.lever.co/mistral/202/apply",
            "categories": {"location": "Paris, France"},
            "descriptionPlain": "Lead annual budgeting, rolling forecasts, and commercial variance analysis across European entities using ERP and BI tools.",
            "salaryRange": {"currency": "EUR", "min": 75000, "max": 95000},
        }
    ]
    lv_jobs = parse_lever_jobs(lever_payload, "Mistral AI", accept_locs=["paris"])
    assert len(lv_jobs) == 1
    assert lv_jobs[0]["salary"] == "EUR 75000-95000"
    assert lv_jobs[0]["application_url"] == "https://jobs.lever.co/mistral/202/apply"

    ashby_payload = {
        "jobs": [
            {
                "title": "Finance Systems & ERP Specialist",
                "location": "Berlin, Germany",
                "isRemote": True,
                "jobUrl": "https://jobs.ashbyhq.com/deepl/303",
                "applyUrl": "https://jobs.ashbyhq.com/deepl/303/application",
                "descriptionPlain": "Own our multi-entity ERP implementation, general ledger automation, and IFRS consolidation workflows across Europe.",
            }
        ]
    }
    ab_jobs = parse_ashby_jobs(ashby_payload, "DeepL", accept_locs=["berlin"])
    assert len(ab_jobs) == 1
    assert "Remote" in ab_jobs[0]["location"]
    assert ab_jobs[0]["full_description"] is not None

    sr_payload = {
        "content": [
            {
                "id": "sr-404",
                "name": "Internal Audit Manager",
                "location": {"city": "Dublin", "country": "Ireland", "remote": False},
            }
        ]
    }
    sr_jobs = parse_smartrecruiters_jobs(sr_payload, "Visa", "Visa", accept_locs=["dublin"])
    assert len(sr_jobs) == 1
    assert sr_jobs[0]["url"] == "https://jobs.smartrecruiters.com/Visa/sr-404"

    personio_xml = """<workzag-jobs><position><id>505</id><name>Senior Group Accountant (IFRS)</name><office>Munich, Germany</office><jobDescriptions><jobDescription><name>Role</name><value>&lt;p&gt;Own IFRS consolidated financial statements and multi-entity statutory audit coordination in Munich.&lt;/p&gt;</value></jobDescription></jobDescriptions></position></workzag-jobs>"""
    p_jobs = parse_personio_jobs(personio_xml, "Personio", "personio", accept_locs=["munich"])
    assert len(p_jobs) == 1
    assert p_jobs[0]["url"] == "https://personio.jobs.personio.de/job/505"
    assert "IFRS consolidated" in (p_jobs[0]["full_description"] or "")

    workable_payload = {
        "jobs": [
            {
                "title": "Senior Financial Controller (ACA)",
                "shortcode": "WK606",
                "city": "London",
                "country": "United Kingdom",
                "telecommuting": True,
                "description": "<p>Lead statutory IFRS reporting, regulatory capital returns, and Big 4 external audit coordination at Tide in London.</p>",
            }
        ]
    }
    w_jobs = parse_workable_jobs(workable_payload, "Tide", "tide", accept_locs=["london"])
    assert len(w_jobs) == 1
    assert w_jobs[0]["url"] == "https://apply.workable.com/tide/j/WK606/"
    assert "Remote" in w_jobs[0]["location"]

    recruitee_payload = {
        "offers": [
            {
                "title": "Group Consolidation & IFRS Specialist",
                "slug": "group-consolidation-ifrs-707",
                "location": "Amsterdam, Netherlands",
                "remote": False,
                "description": "<p>Manage multi-entity IFRS consolidated financial statements and Dutch GAAP statutory filings in Amsterdam.</p>",
            }
        ]
    }
    r_jobs = parse_recruitee_jobs(recruitee_payload, "Mollie", "mollie", accept_locs=["amsterdam"])
    assert len(r_jobs) == 1
    assert r_jobs[0]["url"] == "https://mollie.recruitee.com/o/group-consolidation-ifrs-707"


def test_run_ats_direct_discovery_with_mock_http(tmp_path: Path, monkeypatch) -> None:
    """run_ats_direct_discovery ingests pre-enriched jobs into SQLite without browser or LLM."""
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile("neha")
    config.ensure_dirs()

    def _fake_http_get(url: str) -> httpx.Response:
        if "monzo" in url:
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": 999,
                            "title": "Financial Reporting Manager (ACA)",
                            "absolute_url": "https://boards.greenhouse.io/monzo/jobs/999",
                            "location": {"name": "London, United Kingdom"},
                            "content": "<p>Lead statutory IFRS reporting and external audit coordination at Monzo in London. Visa sponsorship available for qualified ACA candidates.</p>",
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"jobs": [], "content": []})

    summary = run_ats_direct_discovery(workers=2, platforms=["greenhouse"], http_get=_fake_http_get)
    assert summary["new_jobs"] == 1

    conn = database.get_connection()
    row = conn.execute("SELECT * FROM jobs WHERE url = 'https://boards.greenhouse.io/monzo/jobs/999'").fetchone()
    assert row is not None
    assert row["company"] == "Monzo"
    assert row["detail_scraped_at"] is not None
    assert row["sponsor_status"] == "sponsorship_offered"
    database.close_connection()
    config.set_active_profile(None)


def test_cross_board_fuzzy_deduplication_prioritizes_direct_ats(tmp_path: Path, monkeypatch) -> None:
    """deduplicate_jobs merges aggregator duplicates into the direct ATS listing and preserves metadata."""
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile(None)
    conn = database.init_db()

    # Same job posted on LinkedIn, Indeed, and direct Greenhouse ATS
    database.store_jobs(
        conn,
        jobs=[
            {
                "url": "https://uk.linkedin.com/jobs/view/111",
                "title": "Senior Accountant (m/f/d) - Hybrid",
                "company": "Revolut Ltd",
                "location": "London, England, United Kingdom",
                "salary": "£70,000 - £85,000",
                "description": "Short aggregator snippet.",
            },
            {
                "url": "https://uk.indeed.com/viewjob?jk=222",
                "title": "Senior Accountant",
                "company": "Revolut",
                "location": "London, UK",
                "description": "Another short snippet.",
            },
            {
                "url": "https://boards.greenhouse.io/revolut/jobs/333",
                "title": "Senior Accountant (Hybrid)",
                "company": "Revolut Group",
                "location": "London, United Kingdom",
                "full_description": "Full direct ATS description with complete IFRS 15, IFRS 16, and multi-entity consolidation scope across 20+ entities.",
                "application_url": "https://boards.greenhouse.io/revolut/jobs/333#app",
            },
        ],
        site="mixed",
        strategy="greenhouse_api",
    )

    before_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert before_count == 3

    res = database.deduplicate_jobs(conn)
    assert res["groups_merged"] == 1
    assert res["duplicates_removed"] == 2

    rows = conn.execute("SELECT * FROM jobs").fetchall()
    assert len(rows) == 1
    winner = dict(zip(rows[0].keys(), rows[0]))
    # Direct Greenhouse ATS URL won
    assert winner["url"] == "https://boards.greenhouse.io/revolut/jobs/333"
    # Salary from LinkedIn row was merged into the winner!
    assert winner["salary"] == "£70,000 - £85,000"
    assert "IFRS 15" in winner["full_description"]
    database.close_connection()


def test_uk_eu_sponsor_and_expired_detection() -> None:
    """sponsors.py accurately classifies UK/EU licensed sponsors, explicit visa rules, and expired jobs."""
    assert detect_sponsor_status("PwC UK", "Audit Manager", "Join our London audit practice.") == "licensed_sponsor"
    assert (
        detect_sponsor_status(
            "Startup Ltd",
            "Accountant",
            "We offer Skilled Worker visa sponsorship and relocation support to London.",
        )
        == "sponsorship_offered"
    )
    assert (
        detect_sponsor_status(
            "Barclays",
            "Finance Analyst",
            "Candidates must have the existing right to work in the UK without sponsorship.",
        )
        == "no_sponsorship_stated"
    )
    assert detect_work_arrangement("Senior Accountant (Hybrid)", "London, UK", "") == "hybrid"
    assert detect_work_arrangement("AI Engineer", "Remote - EMEA", "") == "remote"
    assert is_job_expired("Sorry, this job is no longer accepting applications.") is True

    meta = evaluate_sponsorship_and_metadata(
        "KPMG",
        "Audit Senior (ACA)",
        "Requires ACA or ACCA qualification with IFRS and ISA statutory audit experience.",
        "London, UK (Hybrid)",
    )
    assert meta["sponsor_status"] == "licensed_sponsor"
    assert meta["work_arrangement"] == "hybrid"
    assert "ACA" in meta["qualifications"]
    assert "IFRS" in meta["qualifications"]
