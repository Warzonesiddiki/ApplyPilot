"""Streaming dashboard rendering writes complete, deterministic HTML to disk."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

import applypilot.config as config
import applypilot.database as database
from applypilot.view import generate_dashboard


@pytest.fixture()
def dashboard_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile("dashboard-tests")
    config.ensure_dirs()
    conn = database.init_db()
    database.ensure_columns(conn)
    now = "2026-10-10T10:00:00Z"
    rows = [
        (
            "https://jobs.example.com/1",
            "Senior AI Engineer",
            "DeepTech London",
            "London, UK",
            9,
            "python, llm\nStrong alignment with the role",
            '{"skills_score": 9, "qualifications_score": 8, "domain_score": 9, "location_score": 7}',
            82,
            "sponsorship_offered",
            "remote",
            "tailored",
            "Follow up Monday",
            "Build LLM pipelines end to end.",
        ),
        (
            "https://jobs.example.com/2",
            "Audit Manager",
            "PwC UK",
            "Leeds, UK",
            7,
            None,
            None,
            None,
            "licensed_sponsor",
            "hybrid",
            "discovered",
            None,
            "ACA qualified auditor.",
        ),
        (
            "https://jobs.example.com/3",
            "Backend Engineer",
            "Acme",
            "Berlin, DE",
            None,
            None,
            None,
            None,
            "no_sponsorship_stated",
            "onsite",
            "discovered",
            None,
            None,
        ),
    ]
    for url, title, company, location, score, reasoning, breakdown, ats, sponsor, arr, stage, notes, desc in rows:
        conn.execute(
            "INSERT INTO jobs (url, title, company, location, fit_score, score_reasoning, score_breakdown, "
            "ats_coverage_pct, sponsor_status, work_arrangement, crm_stage, user_notes, full_description, "
            "discovered_at, site, strategy) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (url, title, company, location, score, reasoning, breakdown, ats, sponsor, arr, stage, notes, desc,
             now, "indeed", "jobspy"),
        )
    conn.commit()
    database.close_connection()
    yield tmp_path
    config.set_active_profile(None)


def test_dashboard_streams_complete_and_deterministic_html(dashboard_db: Path) -> None:
    out_file = dashboard_db / "dash.html"
    returned = generate_dashboard(output_path=str(out_file))

    assert Path(returned) == out_file.resolve() or Path(returned).resolve() == out_file.resolve()
    html = out_file.read_text(encoding="utf-8")

    # Structural completeness: the streamed head, every job section, and the tail landed.
    assert html.startswith("<!DOCTYPE html>")
    assert html.rstrip().endswith("</html>")
    assert html.count('<div class="job-card"') == 3
    assert '<span class="score-badge" style="background:#10b981">9</span>' in html
    assert "Excellent Fit (1 jobs)" in html
    assert "Follow up Monday" in html
    assert "Build LLM pipelines end to end." in html
    assert 'id="kanban-container"' in html
    assert "applyFilters();" in html  # JS tail written
    assert "Showing ${shown} of ${total} jobs" in html  # braces intact after template split

    # No temp file is left behind by the atomic-replace write.
    assert not (dashboard_db / "dash.html.tmp").exists()
    assert list(dashboard_db.glob("*.tmp")) == []

    # A second render is byte-identical (deterministic streaming).
    generate_dashboard(output_path=str(out_file))
    assert out_file.read_text(encoding="utf-8") == html


def test_dashboard_interactive_mode_includes_profile_options(dashboard_db: Path) -> None:
    out_file = dashboard_db / "dash_interactive.html"
    generate_dashboard(output_path=str(out_file), interactive_api=True)
    html = out_file.read_text(encoding="utf-8")

    assert "filter-label\">Profile:" in html
    assert "updateJobStage(" in html
    assert "/api/export?format=csv" in html
    assert not (dashboard_db / "dash_interactive.html.tmp").exists()
