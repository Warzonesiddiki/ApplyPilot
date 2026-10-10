"""Tests for Deterministic ATS Keyword Coverage, 4D Sub-Score Parsing,
Native Word (.docx) Export, and One-Command Resume Importer.
"""

from __future__ import annotations

from pathlib import Path
import zipfile

import pytest

from applypilot import config, database
from applypilot.scoring.ats import compute_ats_coverage, extract_jd_keywords
from applypilot.scoring.docx_export import batch_convert_docx, convert_to_docx
from applypilot.scoring.scorer import _parse_score_response
from applypilot.wizard.importer import import_from_resume_file

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_ats_keyword_coverage_computes_delta_improvement() -> None:
    """compute_ats_coverage calculates baseline vs. tailored keyword overlap and delta."""
    jd = (
        "We are hiring an ACA or ACCA qualified Financial Accountant with strong "
        "IFRS 15, IFRS 16, UK GAAP, Statutory Audit, Consolidation, SAP, and Power BI experience."
    )
    base_cv = "Chartered Accountant (ACA) with experience in IFRS, Statutory Audit, and Excel."
    tailored_cv = (
        "Chartered Accountant (ACA) with deep expertise in IFRS 15, IFRS 16, UK GAAP, "
        "Statutory Audit, multi-entity Consolidation, SAP, and Power BI dashboards."
    )

    kws = [lbl for lbl, _ in extract_jd_keywords(jd)]
    assert "ACA" in kws
    assert "IFRS 15" in kws
    assert "SAP" in kws

    cov = compute_ats_coverage(jd, tailored_cv, base_resume=base_cv)
    assert cov["coverage_pct"] > cov["baseline_pct"]
    assert cov["delta_pct"] > 0
    assert "IFRS 15" in cov["matched_keywords"]
    assert "SAP" in cov["matched_keywords"]


def test_parse_score_response_with_4d_subscores() -> None:
    """_parse_score_response parses overall score, 4 sub-scores, matched and missing keywords."""
    raw = """{
        "score": 9,
        "skills_score": 9,
        "qualifications_score": 10,
        "domain_score": 9,
        "location_score": 8,
        "keywords": ["ACA", "IFRS 15", "Statutory Audit", "SAP", "FP&A"],
        "missing_keywords": ["Anaplan"],
        "reasoning": "Candidate is an ACA Chartered Accountant with direct KPMG and Grant Thornton audit and FP&A experience."
    }"""

    parsed = _parse_score_response(raw)
    assert parsed["score"] == 9
    assert parsed["skills_score"] == 9
    assert parsed["qualifications_score"] == 10
    assert parsed["domain_score"] == 9
    assert parsed["location_score"] == 8
    assert "ACA" in parsed["keywords"]
    assert parsed["missing_keywords"] == "Anaplan"


def test_docx_export_for_resume_and_cover_letter_and_importer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """convert_to_docx produces valid OpenXML .docx files that can be re-imported by importer.py."""
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile("tahir")
    config.ensure_dirs()
    conn = database.init_db()

    tahir_cv_src = REPO_ROOT / "docs" / "profiles" / "tahir_siddique_resume.txt"
    tailored_txt = config.TAILORED_DIR / "Revolut_Senior_Accountant_abc123.txt"
    tailored_txt.write_text(tahir_cv_src.read_text(encoding="utf-8"), encoding="utf-8")

    cl_txt = config.COVER_LETTER_DIR / "Revolut_Senior_Accountant_abc123_CL.txt"
    cl_txt.write_text(
        "Dear Hiring Manager,\n\n"
        "As an ACCA Finalist (11 of 13 exams passed) and Senior Accountant at Finnet Global, "
        "I have prepared IFRS financial statements across 20+ multi-entity clients.\n\n"
        "Tahir Siddique",
        encoding="utf-8",
    )

    database.store_jobs(
        conn,
        jobs=[{"url": "https://jobs.revolut.com/1", "title": "Senior Accountant", "company": "Revolut"}],
        site="Revolut",
        strategy="greenhouse_api",
    )
    conn.execute(
        "UPDATE jobs SET tailored_resume_path = ?, cover_letter_path = ? WHERE url = 'https://jobs.revolut.com/1'",
        (str(tailored_txt), str(cl_txt)),
    )
    conn.commit()

    res_docx = convert_to_docx(tailored_txt)
    cl_docx = convert_to_docx(cl_txt)
    assert res_docx.exists()
    assert cl_docx.exists()

    with zipfile.ZipFile(res_docx, "r") as zf:
        names = zf.namelist()
        assert "word/document.xml" in names
        assert "[Content_Types].xml" in names
        xml_str = zf.read("word/document.xml").decode("utf-8")
        assert "Tahir Siddique" in xml_str
        assert "Finnet Global" in xml_str

    batch_convert_docx()
    row = conn.execute("SELECT docx_resume_path, docx_cover_path FROM jobs WHERE url = 'https://jobs.revolut.com/1'").fetchone()
    assert row["docx_resume_path"] == str(res_docx)
    assert row["docx_cover_path"] == str(cl_docx)

    # Now test importing a brand-new profile directly from the generated .docx file!
    config.set_active_profile("imported_tahir")
    info = import_from_resume_file(res_docx)
    assert info["domain"] == "finance_accounting"
    assert "Tahir Siddique" in info["full_name"]
    assert Path(info["profile_path"]).exists()
    assert Path(info["searches_path"]).exists()

    database.close_connection()
    config.set_active_profile(None)


def test_multi_variant_resume_selection_and_expired_prefilter(tmp_path: Path) -> None:
    """select_best_base_resume picks the highest-overlap variant and score_job pre-filters expired listings."""
    from applypilot.scoring.ats import select_best_base_resume
    from applypilot.scoring.scorer import score_job

    base_cv = tmp_path / "resume.txt"
    base_cv.write_text("General Accountant with Excel and Bookkeeping.", encoding="utf-8")

    variants_dir = tmp_path / "variants"
    variants_dir.mkdir(parents=True, exist_ok=True)
    audit_cv = variants_dir / "resume_statutory_audit.txt"
    audit_cv.write_text(
        "ACA Chartered Accountant specializing in IFRS 15, IFRS 16, Statutory Audit, ISA, and Multi-Entity Consolidation.",
        encoding="utf-8",
    )

    jd = "Hiring an ACA Chartered Accountant to lead IFRS 15, IFRS 16, Statutory Audit, and Multi-Entity Consolidation."
    chosen_text, chosen_name = select_best_base_resume(jd, default_resume_path=base_cv)
    assert chosen_name == "resume_statutory_audit"
    assert "IFRS 16" in chosen_text

    expired_job = {
        "title": "Audit Manager",
        "company": "PwC",
        "location": "London",
        "full_description": "This position has been filled and is no longer accepting applications. Require ACA and IFRS.",
    }
    res = score_job(chosen_text, expired_job)
    assert res["score"] == 1
    assert "expired" in res["reasoning"].lower()

