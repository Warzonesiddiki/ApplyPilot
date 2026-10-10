"""One-Command Resume-to-Profile & Search Importer (`applypilot init --from-resume`).

Parses an existing plain-text (`.txt`) or Word (`.docx`) resume to automatically
bootstrap `profile.json`, `resume.txt`, and `searches.yaml` for the active profile.
Works both offline (deterministic parser) and with an LLM (when configured).
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

import yaml

from applypilot import config
from applypilot.scoring.ats import extract_jd_keywords
from applypilot.scoring.pdf import parse_entries, parse_resume, parse_skills


def _read_resume_file(path: Path) -> str:
    """Read plain text from `.txt` or `.docx` resume files."""
    if path.suffix.lower() == ".docx":
        with zipfile.ZipFile(path, "r") as zf:
            xml_bytes = zf.read("word/document.xml")
        root = ET.fromstring(xml_bytes)
        lines: list[str] = []
        for elem in root.iter():
            if elem.tag.endswith("}p"):
                texts = [t.text for t in elem.iter() if t.tag.endswith("}t") and t.text]
                if texts:
                    lines.append("".join(texts))
        return "\n".join(lines)
    return path.read_text(encoding="utf-8")


def parse_resume_to_profile_and_searches(resume_text: str) -> tuple[dict, dict]:
    """Deterministically parse a resume text into `(profile_dict, searches_dict)`."""
    parsed = parse_resume(resume_text)
    full_name = parsed.get("name") or "Candidate"
    target_role = parsed.get("title") or "Senior Professional"
    location_raw = parsed.get("location") or "London, United Kingdom"

    email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", resume_text)
    email = email_match.group(0) if email_match else ""

    phone_match = re.search(r"\+?\d[\d\s()-]{8,16}\d", resume_text)
    phone = phone_match.group(0).strip() if phone_match else ""

    linkedin_match = re.search(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w-]+", resume_text, re.IGNORECASE)
    linkedin = linkedin_match.group(0) if linkedin_match else ""

    loc_parts = [p.strip() for p in location_raw.split(",") if p.strip()]
    city = loc_parts[0] if loc_parts else "London"
    country = loc_parts[-1] if len(loc_parts) > 1 else "United Kingdom"

    years_match = re.search(r"(\d+)\+?\s*years?", resume_text, re.IGNORECASE)
    years = years_match.group(1) if years_match else "5"

    sections = parsed.get("sections", {})
    exp_entries = parse_entries(sections.get("EXPERIENCE", "")) if sections.get("EXPERIENCE") else []
    proj_entries = parse_entries(sections.get("PROJECTS", "")) if sections.get("PROJECTS") else []

    # Extract companies from experience headers ("Role at Company" or "Role | Company")
    companies: list[str] = []
    for exp in exp_entries:
        hdr = exp.get("title") or exp.get("header", "")
        if " at " in hdr:
            comp = hdr.split(" at ", 1)[1].strip()
        elif " | " in hdr:
            comp = hdr.split(" | ", 1)[1].strip()
        elif " - " in hdr:
            comp = hdr.split(" - ", 1)[-1].strip()
        else:
            comp = ""
        if comp and comp not in companies:
            companies.append(comp)

    # Extract quantified metrics from experience bullets
    metrics: list[str] = []
    for exp in exp_entries:
        for b in exp.get("bullets", []):
            if re.search(r"\d+%|\b(?:GBP|EUR|USD|AED|CHF|£|€|\$)\s*\d+|\b\d+\+\s*(?:entities|clients|audits|projects)", b, re.IGNORECASE):
                if len(metrics) < 8:
                    metrics.append(b[:120])

    # Extract skills boundary from parsed skills or ATS keyword extractor
    skills_boundary: dict[str, list[str]] = {}
    skills_sec = sections.get("TECHNICAL SKILLS") or sections.get("CORE COMPETENCIES & TECHNICAL SKILLS") or ""
    for cat, items_s in parse_skills(skills_sec):
        items = [i.strip() for i in items_s.split(",") if i.strip()]
        if items:
            skills_boundary[cat.strip()] = items
    if not skills_boundary:
        extracted = [lbl for lbl, _ in extract_jd_keywords(resume_text)]
        skills_boundary["Core Competencies"] = extracted[:15] if extracted else ["Python", "SQL", "Excel"]

    cert_sec = sections.get("CERTIFICATIONS") or sections.get("QUALIFICATIONS & CERTIFICATIONS") or ""
    certs = [c.strip() for c in cert_sec.split("|") if c.strip()] if cert_sec else []
    edu_sec = sections.get("EDUCATION") or ""
    edu_lines = [line.strip() for line in edu_sec.splitlines() if line.strip()]
    school = edu_lines[0].split("|")[0].strip() if edu_lines else ""

    text_low = resume_text.lower()
    if any(k in text_low for k in ("ifrs", "chartered accountant", "aca", "acca", "statutory audit", "fp&a", "general ledger")):
        domain = "finance_accounting"
    else:
        domain = "software_engineering"

    clean_first = full_name.split(",")[0].strip().split()[0] if full_name else "Candidate"
    profile_data = {
        "domain": domain,
        "personal": {
            "full_name": full_name,
            "preferred_name": clean_first,
            "email": email,
            "phone": re.sub(r"[^\d+]", "", phone),
            "phone_formatted": phone,
            "city": city,
            "state": "",
            "country": country,
            "linkedin": linkedin,
        },
        "work_authorization": {
            "authorized": "Yes",
            "require_sponsorship": "Yes",
            "willing_to_relocate": "Yes",
            "notice_period": "1 Month (Immediately negotiable)",
            "target_regions": ["United Kingdom", "Ireland", "Netherlands", "Germany", "Luxembourg", "Switzerland"],
        },
        "compensation": {
            "salary_expectation": "65000",
            "salary_currency": "GBP",
            "salary_range_min": "55000",
            "salary_range_max": "85000",
            "currencies": {
                "GBP": {"expectation": "65000", "min": "55000", "max": "85000", "symbol": "£"},
                "EUR": {"expectation": "75000", "min": "65000", "max": "95000", "symbol": "€"},
                "CHF": {"expectation": "110000", "min": "95000", "max": "135000", "symbol": "CHF"},
            },
        },
        "experience": {
            "years": str(years),
            "target_role": target_role,
            "education_level": "Master's / Professional Qualification" if certs else "Bachelor's Degree",
        },
        "skills_boundary": skills_boundary,
        "resume_facts": {
            "preserved_companies": companies,
            "preserved_projects": [p.get("title") or p.get("header", "") for p in proj_entries if p.get("title") or p.get("header")],
            "preserved_school": school,
            "certifications": certs,
            "real_metrics": metrics,
        },
    }

    if domain == "finance_accounting":
        queries = [
            {"query": target_role.split("|")[0].strip(), "tier": 1},
            {"query": "Senior Accountant", "tier": 1},
            {"query": "Financial Accountant IFRS", "tier": 1},
            {"query": "FP&A Analyst", "tier": 2},
            {"query": "Audit Senior", "tier": 2},
        ]
    else:
        queries = [
            {"query": target_role.split("|")[0].strip(), "tier": 1},
            {"query": "Senior Software Engineer", "tier": 1},
            {"query": "AI Engineer", "tier": 2},
        ]

    searches_data = {
        "defaults": {
            "results_wanted": 30,
            "hours_old": 168,
            "country_indeed": "uk",
        },
        "boards": ["linkedin", "indeed", "glassdoor", "google"],
        "locations": [
            {"location": "London, United Kingdom", "country_indeed": "uk", "remote": False},
            {"location": "Dublin, Ireland", "country_indeed": "ireland", "remote": False},
            {"location": "Amsterdam, Netherlands", "country_indeed": "netherlands", "remote": False},
        ],
        "queries": queries,
        "exclude_titles": ["Intern", "Internship", "Trainee"],
    }

    return profile_data, searches_data


def import_from_resume_file(resume_file: Path | str) -> dict:
    """Import a resume file (`.txt` or `.docx`) and write `profile.json`, `resume.txt`, and `searches.yaml`."""
    src = Path(resume_file)
    if not src.exists():
        raise FileNotFoundError(f"Resume file not found: {src}")

    config.ensure_dirs()
    resume_text = _read_resume_file(src)
    profile_data, searches_data = parse_resume_to_profile_and_searches(resume_text)

    config.RESUME_PATH.write_text(resume_text, encoding="utf-8")
    config.secure_write_text(config.PROFILE_PATH, json.dumps(profile_data, indent=2) + "\n")
    config.SEARCH_CONFIG_PATH.write_text(
        yaml.safe_dump(searches_data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    return {
        "profile_path": str(config.PROFILE_PATH),
        "resume_path": str(config.RESUME_PATH),
        "searches_path": str(config.SEARCH_CONFIG_PATH),
        "domain": profile_data["domain"],
        "full_name": profile_data["personal"]["full_name"],
        "target_role": profile_data["experience"]["target_role"],
    }
