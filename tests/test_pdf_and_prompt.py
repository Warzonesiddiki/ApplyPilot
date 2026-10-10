"""Tests for HTML/PDF rendering (resume & cover letter) and UK/EU auto-apply prompt generation."""

from __future__ import annotations

from pathlib import Path

from applypilot import config
from applypilot.apply.prompt import build_prompt
from applypilot.scoring.pdf import (
    build_cover_letter_html,
    build_html,
    is_cover_letter,
    parse_resume,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_resume_html_escapes_special_characters() -> None:
    """Resume fields containing <, >, & (e.g. '<10ms', 'R&D') must be HTML-escaped."""
    sample_resume = """Tahir Siddique
Senior AI & Systems Engineer
London, United Kingdom
tahir@example.com | +447000000000

SUMMARY
Engineered low-latency pipelines (<10ms p99) for FP&A & R&D teams.

TECHNICAL SKILLS
Languages & Tools: Python, C++ template<T>, FastAPI & React

EXPERIENCE
Senior Engineer at FinTech Co
London | 2023 - Present
- Reduced query latency to <5ms & cut cloud costs by 35%

EDUCATION
B.Sc. Computer Science & Engineering"""

    parsed = parse_resume(sample_resume)
    html = build_html(parsed)

    assert "&lt;10ms" in html
    assert "template&lt;T&gt;" in html
    assert "&lt;5ms &amp; cut" in html
    assert "<10ms" not in html


def test_cover_letter_detection_and_dedicated_html_template(tmp_path: Path) -> None:
    """Cover letters are detected and rendered with the business-letter HTML template."""
    cl_text = (
        "Dear Hiring Manager,\n\n"
        "As an ACA-qualified Chartered Accountant with 5 years of IFRS 15/16 experience, "
        "I led statutory audits for £120M multi-entity groups.\n\n"
        "Happy to walk through any of this in more detail.\n\n"
        "Neha Tariq, ACA"
    )
    cl_file = tmp_path / "PwC_Audit_Manager_abc123_CL.txt"
    cl_file.write_text(cl_text, encoding="utf-8")

    assert is_cover_letter(cl_text, cl_file) is True
    html = build_cover_letter_html(cl_text, profile={"personal": {"full_name": "Neha Tariq, ACA", "email": "neha@example.com"}})
    assert "Dear Hiring Manager," in html
    assert "Neha Tariq, ACA" in html
    assert "class=\"letter-body\"" in html


def test_build_prompt_uk_eu_eligibility_and_worker_isolation(tmp_path: Path, monkeypatch) -> None:
    """build_prompt allows UK/EU locations, isolates worker uploads, and redacts secrets when requested."""
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))

    neha_dir = config.set_active_profile("neha")
    config.ensure_dirs()

    # Copy Neha template profile and searches
    neha_profile_src = REPO_ROOT / "docs" / "profiles" / "neha_tariq_aca.template.json"
    neha_search_src = REPO_ROOT / "docs" / "profiles" / "searches_neha_aca_eu_uk.yaml"
    (neha_dir / "profile.json").write_text(neha_profile_src.read_text(encoding="utf-8"), encoding="utf-8")
    (neha_dir / "searches.yaml").write_text(neha_search_src.read_text(encoding="utf-8"), encoding="utf-8")

    # Create dummy tailored resume PDF
    resume_txt = config.TAILORED_DIR / "PwC_Audit_Manager_123456.txt"
    resume_pdf = config.TAILORED_DIR / "PwC_Audit_Manager_123456.pdf"
    resume_txt.write_text("Neha Tariq, ACA\nAudit Manager", encoding="utf-8")
    resume_pdf.write_bytes(b"%PDF-1.4 dummy pdf")

    job = {
        "url": "https://jobs.pwc.co.uk/audit-manager",
        "application_url": "https://jobs.pwc.co.uk/audit-manager/apply",
        "title": "Audit Manager (ACA)",
        "company": "PwC UK",
        "site": "ICAEW Jobs",
        "fit_score": 9,
        "tailored_resume_path": str(resume_txt),
    }

    prompt = build_prompt(
        job=job,
        tailored_resume="Neha Tariq, ACA\nAudit Manager",
        worker_id=2,
        redact_secrets=True,
    )

    # 1. Verify hardcoded "Europe -> NOT ELIGIBLE" blocker is GONE
    assert "India, Philippines, Europe, etc." not in prompt
    assert "United Kingdom" in prompt
    assert "Netherlands" in prompt
    assert "Do NOT abort applications located in the United Kingdom" in prompt

    # 2. Verify multi-currency GBP/EUR/CHF support
    assert "GBP" in prompt
    assert "EUR" in prompt
    assert "MONTHLY GROSS" in prompt

    # 3. Verify worker-2 upload isolation
    expected_upload_dir = config.APPLY_WORKER_DIR / "worker-2" / "uploads"
    assert str(expected_upload_dir) in prompt
    assert (expected_upload_dir / "Neha_Tariq_Resume.pdf").exists()

    # 4. Verify secret redaction in debug mode
    assert "<REDACTED_PASSWORD>" in prompt

    config.set_active_profile(None)
