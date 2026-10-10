"""Tests for domain-adaptive validation and tailoring (Tahir Siddique ACCA & Neha Tariq ACA + Tech)."""

from __future__ import annotations

import json
from pathlib import Path

from applypilot.scoring.tailor import (
    _build_file_prefix,
    _build_tailor_prompt,
    assemble_resume_text,
)
from applypilot.scoring.validator import (
    detect_profile_domain,
    validate_json_fields,
    validate_tailored_resume,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_template(filename: str) -> dict:
    path = REPO_ROOT / "docs" / "profiles" / filename
    return json.loads(path.read_text(encoding="utf-8"))


def _load_resume_txt(filename: str) -> str:
    path = REPO_ROOT / "docs" / "profiles" / filename
    return path.read_text(encoding="utf-8")


def test_detect_profile_domain_for_tahir_neha_and_tech() -> None:
    tahir = _load_template("tahir_siddique.template.json")
    neha = _load_template("neha_tariq_aca.template.json")

    assert detect_profile_domain(tahir) == "finance_accounting"
    assert detect_profile_domain(neha) == "finance_accounting"

    hybrid_prof = {
        "experience": {"target_role": "AI Engineer & FP&A Systems Architect"},
        "skills_boundary": {"AI": ["FastAPI", "LangGraph"], "Finance": ["FP&A", "IFRS"]},
    }
    assert detect_profile_domain(hybrid_prof) == "fpa_tech_hybrid"

    swe_prof = {
        "experience": {"target_role": "Senior Backend Engineer"},
        "skills_boundary": {"Backend": ["Python", "Go", "Kubernetes"]},
    }
    assert detect_profile_domain(swe_prof) == "software_engineering"


def test_bundled_resumes_pass_validation_against_real_profiles() -> None:
    """Both Tahir Siddique's and Neha Tariq, ACA's bundled resume.txt files must pass validation."""
    tahir = _load_template("tahir_siddique.template.json")
    tahir_txt = _load_resume_txt("tahir_siddique_resume.txt")
    tahir_res = validate_tailored_resume(tahir_txt, tahir)
    assert tahir_res["passed"] is True, f"Tahir resume validation errors: {tahir_res['errors']}"

    neha = _load_template("neha_tariq_aca.template.json")
    neha_txt = _load_resume_txt("neha_tariq_aca_resume.txt")
    neha_res = validate_tailored_resume(neha_txt, neha)
    assert neha_res["passed"] is True, f"Neha resume validation errors: {neha_res['errors']}"


def test_neha_aca_validation_allows_certifications_and_no_projects() -> None:
    """Neha Tariq, ACA profile must preserve her 4 employers & ICAP and allow empty projects."""
    neha = _load_template("neha_tariq_aca.template.json")

    llm_data = {
        "title": "FP&A & Audit Manager, ACA",
        "summary": (
            "Chartered Accountant (ACA) with 7+ years of progressive experience in FP&A, "
            "Audit & Assurance, and Financial Management across KPMG, Grant Thornton, Finnet Global, and Al-Ghurair."
        ),
        "skills": {
            "FP&A & Corporate Finance": "Budgeting (>AED 100M), Rolling Forecasts, DCF Valuation, Financial Modeling",
            "Audit, Assurance & Standards": "IFRS (IFRS 3, 9, 10, 15), ISA, SOX Compliance, COSO Framework",
            "Taxation & Regulatory Compliance": "UAE Corporate Tax, VAT Compliance, FTA, ESR, UBO, AML/CFT",
            "Financial Systems & Analytics": "SAP, Oracle Financials, QuickBooks, Zoho Books, Dynamics 365, Power BI",
        },
        "experience": [
            {
                "header": "FP&A Analyst at Al-Ghurair",
                "subtitle": "Dubai, UAE | December 2025 - Present",
                "bullets": [
                    "Lead development of annual budgets exceeding AED 100M, quarterly forecasts, and 5-year long-range financial plans.",
                    "Design executive dashboards tracking 25+ financial and operational KPIs across 4 divisions.",
                ],
            },
            {
                "header": "Assistant Manager - Assurance And Finance at Finnet Global",
                "subtitle": "Dubai, UAE | January 2025 - November 2025",
                "bullets": [
                    "Implemented rolling forecast models improving forecast accuracy by 18% and identifying AED 850K in annual cost savings.",
                ],
            },
            {
                "header": "Senior Associate - Audit And Assurance at Grant Thornton",
                "subtitle": "Dubai, UAE | November 2023 - May 2024",
                "bullets": [
                    "Managed audit portfolio of 15+ clients with combined revenues exceeding AED 500M, reducing audit cycle time by 20%.",
                ],
            },
            {
                "header": "Assistant Manager - Audit at KPMG Taseer Hadi and Co.",
                "subtitle": "Islamabad | November 2018 - November 2023",
                "bullets": [
                    "Delivered 30+ statutory audits with zero restatements under ISA and IFRS 9, 3, 10, and 15.",
                ],
            },
        ],
        "projects": [],
        "certifications": (
            "Chartered Accountant (CA / ACA) - Institute of Chartered Accountants of Pakistan (ICAP) | "
            "Associate Public Finance Accountant (APFA) - PIPFA"
        ),
        "education": (
            "Institute of Chartered Accountants of Pakistan (ICAP) | Chartered Accountant (ACA) | "
            "University of Lahore | Bachelor of Commerce (B.Com), CGPA: 3.75/4.00"
        ),
    }

    res = validate_json_fields(llm_data, neha, mode="strict")
    assert res["passed"] is True, f"Unexpected validation errors: {res['errors']}"

    assembled = assemble_resume_text(llm_data, neha)
    assert "Neha Tariq, ACA" in assembled
    assert "CERTIFICATIONS" in assembled
    assert "PROJECTS" not in assembled

    text_res = validate_tailored_resume(assembled, neha)
    assert text_res["passed"] is True, f"Unexpected text validation errors: {text_res['errors']}"


def test_tahir_validation_and_file_prefix_collision_prevention() -> None:
    """Tahir's ACCA Finalist / Senior Accountant profile validates facts and prevents filename collisions."""
    tahir = _load_template("tahir_siddique.template.json")

    prompt = _build_tailor_prompt(tahir)
    assert "Finnet Global" in prompt
    assert "ACCA Finalist (11 of 13 exams passed)" in prompt
    assert "Zoho Books" in prompt

    job1 = {"title": "Senior Accountant", "company": "Revolut", "url": "https://jobs.revolut.com/101"}
    job2 = {"title": "Senior Accountant", "company": "Revolut", "url": "https://jobs.revolut.com/202"}

    prefix1 = _build_file_prefix(job1)
    prefix2 = _build_file_prefix(job2)
    assert prefix1 != prefix2
    assert prefix1.startswith("Revolut_Senior_Accountant_")
