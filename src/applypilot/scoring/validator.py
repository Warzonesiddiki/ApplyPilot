"""Resume and cover letter validation: banned words, fabrication detection, structural checks.

All validation is profile-driven and domain-adaptive (supporting both Software/AI Engineering
and Chartered Accountancy / ACA / Finance profiles).
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)


# ── Universal Constants (not personal data) ───────────────────────────────

BANNED_WORDS: list[str] = [
    "passionate", "dedicated", "committed to",
    "utilizing", "utilize", "harnessing",
    "spearheaded", "spearhead", "orchestrated", "championed", "pioneered",
    "robust", "scalable solutions", "cutting-edge", "state-of-the-art", "best-in-class",
    "proven track record", "track record of success", "demonstrated ability",
    "strong communicator", "team player", "fast learner", "self-starter", "go-getter",
    "synergy", "cross-functional collaboration", "holistic",
    "transformative", "innovative solutions", "paradigm", "ecosystem",
    "proactive", "detail-oriented", "highly motivated",
    "seamless", "full lifecycle",
    "deep understanding", "extensive experience", "comprehensive knowledge",
    "thrives in", "excels at", "adept at", "well-versed in",
    "i am confident", "i believe", "i am excited",
    "plays a critical role", "instrumental in", "integral part of",
    "strong track record", "eager to", "eager",
    # Cover-letter-specific additions
    "this demonstrates", "this reflects", "i have experience with",
    "furthermore", "additionally", "moreover",
]

LLM_LEAK_PHRASES: list[str] = [
    "i am sorry", "i apologize", "i will try", "let me try",
    "i am at a loss", "i am truly sorry", "apologies for",
    "i keep fabricating", "i will have to admit", "one final attempt",
    "one last time", "if it fails again", "persistent errors",
    "i am having difficulty", "i made an error", "my mistake",
    "here is the corrected", "here is the revised", "here is the updated",
    "here is my", "below is the", "as requested",
    "note:", "disclaimer:", "important:",
    "i have rewritten", "i have removed", "i have fixed",
    "i have replaced", "i have updated", "i have corrected",
    "per your feedback", "based on your feedback", "as per the instructions",
    "the following resume", "the resume below",
    "the following cover letter", "the letter below",
]

# Default watchlist of tools/certifications that are flagged ONLY if absent from
# the candidate's profile and base resume.
FABRICATION_WATCHLIST: set[str] = {
    "c#", "c++", "golang", "rust", "ruby",
    "kotlin", "swift", "scala", "matlab",
    "spring", "django", "rails", "angular", "vue", "svelte",
    "certif", "certified", "pmp", "scrum master", "aws certified",
}

REQUIRED_SECTIONS: set[str] = {"SUMMARY", "TECHNICAL SKILLS", "EXPERIENCE", "PROJECTS", "EDUCATION"}


# ── Helpers ───────────────────────────────────────────────────────────────

def detect_profile_domain(profile: dict) -> str:
    """Detect the candidate's professional domain from their profile.

    Returns one of:
      - "finance_accounting" (ACA, ACCA, CPA, Audit, IFRS, Financial Reporting, Tax)
      - "fpa_tech_hybrid" (Software/AI + FP&A / Financial Systems)
      - "software_engineering" (default)
    """
    explicit = str(profile.get("domain", "")).strip().lower()
    if explicit in ("finance_accounting", "fpa_tech_hybrid", "software_engineering"):
        return explicit

    exp = profile.get("experience", {})
    role_text = " ".join([
        str(exp.get("target_role", "")),
        str(exp.get("current_job_title", "")),
        str(exp.get("current_title", "")),
        str(exp.get("education_level", "")),
    ]).lower()

    skills_text = " ".join(_build_skills_set(profile)).lower()
    combined = f"{role_text} {skills_text}"

    finance_markers = ("aca", "acca", "icaew", "chartered accountant", "ifrs", "uk gaap", "external audit", "statutory audit")
    software_markers = ("python", "typescript", "javascript", "fastapi", "react", "docker", "kubernetes")

    has_finance = any(m in combined for m in finance_markers)
    has_software = any(m in combined for m in software_markers)

    if has_finance and not has_software:
        return "finance_accounting"
    if has_finance and has_software:
        return "fpa_tech_hybrid"
    return "software_engineering"


def _build_skills_set(profile: dict) -> set[str]:
    """Build the set of allowed skills and qualifications from the profile."""
    boundary = profile.get("skills_boundary", {})
    allowed: set[str] = set()
    for category in boundary.values():
        if isinstance(category, (list, set, tuple)):
            for s in category:
                s_clean = str(s).lower().strip()
                allowed.add(s_clean)
                # Also add individual tokens (e.g. "c++" or "django" inside "Python / Django")
                for token in re.split(r"[\s,/()]+", s_clean):
                    if token:
                        allowed.add(token)

    resume_facts = profile.get("resume_facts", {})
    for cert in resume_facts.get("certifications", []):
        c_clean = str(cert).lower().strip()
        allowed.add(c_clean)
        allowed.update({"certif", "certified", "certification", "qualification"})

    exp = profile.get("experience", {})
    edu_level = str(exp.get("education_level", "")).lower()
    if any(q in edu_level for q in ("aca", "acca", "cpa", "cfa", "cima", "icaew", "chartered")):
        allowed.update({"certif", "certified", "certification", "qualification", "aca", "acca", "chartered"})

    return allowed


def _get_effective_watchlist(profile: dict, original_text: str = "") -> set[str]:
    """Return FABRICATION_WATCHLIST filtered by the user's actual skills and domain."""
    allowed = _build_skills_set(profile)
    domain = detect_profile_domain(profile)
    orig_lower = original_text.lower() if original_text else ""

    effective: set[str] = set()
    for item in FABRICATION_WATCHLIST:
        item_low = item.lower()
        if item_low in allowed:
            continue
        if any(item_low in skill for skill in allowed):
            continue
        if orig_lower and item_low in orig_lower:
            continue
        if domain == "finance_accounting" and item_low in ("certif", "certified"):
            continue
        effective.add(item_low)
    return effective


def _matches_watchlist_item(item: str, text_lower: str) -> bool:
    """Match a watchlist item against text using token/word boundaries."""
    if len(item) <= 2:
        return False
    # Special handling for symbols like c++ or c#
    if any(ch in item for ch in ("#", "+")):
        pattern = r"(?<![a-z0-9])" + re.escape(item) + r"(?![a-z0-9#+])"
        return bool(re.search(pattern, text_lower))
    if item == "certif":
        return bool(re.search(r"\bcertif\w*", text_lower))
    return bool(re.search(r"\b" + re.escape(item) + r"\b", text_lower))


def sanitize_text(text: str) -> str:
    """Auto-fix common LLM output issues instead of rejecting."""
    text = text.replace(" \u2014 ", ", ").replace("\u2014", ", ")   # em dash -> comma
    text = text.replace("\u2013", "-")    # en dash -> hyphen
    text = text.replace("\u201c", '"').replace("\u201d", '"')   # smart double quotes
    text = text.replace("\u2018", "'").replace("\u2019", "'")   # smart single quotes
    return text.strip()


# ── JSON Field Validation ─────────────────────────────────────────────────

def validate_json_fields(data: dict, profile: dict, mode: str = "normal") -> dict:
    """Validate individual JSON fields from an LLM-generated tailored resume.

    Args:
        data:    Parsed JSON from the LLM (title, summary, skills, experience, projects, education).
        profile: User profile dict from load_profile().
        mode:    Validation strictness — "strict", "normal", or "lenient".

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []
    domain = detect_profile_domain(profile)

    # Required keys — projects is optional for finance/accounting profiles (e.g. ACA CVs)
    required_keys: tuple[str, ...] = (
        ("title", "summary", "skills", "experience", "education")
        if domain == "finance_accounting"
        else ("title", "summary", "skills", "experience", "projects", "education")
    )

    for key in required_keys:
        if key not in data or not data[key]:
            errors.append(f"Missing required field: {key}")
    if errors:
        return {"passed": False, "errors": errors, "warnings": warnings}

    all_text_parts: list[str] = [str(data["summary"])]
    watchlist = _get_effective_watchlist(profile)

    # Skills: check for fabrication against profile-aware watchlist
    if isinstance(data["skills"], dict):
        skills_text = " ".join(str(v) for v in data["skills"].values()).lower()
        for fake in watchlist:
            if _matches_watchlist_item(fake, skills_text):
                errors.append(f"Fabricated skill: '{fake}'")

    # Experience: preserved companies must be present
    resume_facts = profile.get("resume_facts", {})
    preserved_companies = resume_facts.get("preserved_companies", [])

    if isinstance(data["experience"], list):
        for company in preserved_companies:
            has_company = any(
                company.lower() in str(e.get("header", "")).lower()
                for e in data["experience"]
            )
            if not has_company:
                errors.append(f"Company '{company}' missing from experience")
        for entry in data["experience"]:
            all_text_parts.extend(str(b) for b in entry.get("bullets", []))

    # Projects: collect bullets if present
    if isinstance(data.get("projects"), list):
        for entry in data["projects"]:
            all_text_parts.extend(str(b) for b in entry.get("bullets", []))

    # Certifications / Qualifications: collect if present
    if data.get("certifications"):
        all_text_parts.append(str(data["certifications"]))

    # Education: preserved school must be present
    preserved_school = resume_facts.get("preserved_school", "")
    if preserved_school:
        edu = str(data.get("education", ""))
        if preserved_school.lower() not in edu.lower():
            errors.append(f"Education '{preserved_school}' missing")

    # Bulk text checks
    all_text = " ".join(all_text_parts).lower()

    # LLM self-talk is always an error regardless of mode
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in all_text]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # Banned filler words — severity depends on mode
    if mode != "lenient":
        found_banned = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", all_text)]
        if found_banned:
            msg = f"Banned words: {', '.join(found_banned[:5])}"
            if mode == "strict":
                errors.append(msg)
            else:
                warnings.append(msg)

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}


# ── Full Resume Text Validation ───────────────────────────────────────────

def validate_tailored_resume(text: str, profile: dict, original_text: str = "") -> dict:
    """Programmatic validation of a tailored resume against the user's profile."""
    errors: list[str] = []
    warnings: list[str] = []
    text_lower = text.lower()
    domain = detect_profile_domain(profile)

    personal = profile.get("personal", {})
    resume_facts = profile.get("resume_facts", {})

    # 1. Check required sections exist (flexible matching, domain-aware)
    section_variants: dict[str, list[str]] = {
        "SUMMARY": ["summary", "professional summary", "profile", "executive summary"],
        "TECHNICAL SKILLS": [
            "technical skills", "skills", "tech stack", "core skills", "technologies",
            "core competencies", "areas of expertise",
        ],
        "EXPERIENCE": ["experience", "work experience", "professional experience", "career history"],
        "EDUCATION": ["education", "academic background", "qualifications", "certifications"],
    }
    if domain != "finance_accounting":
        section_variants["PROJECTS"] = [
            "projects", "personal projects", "key projects", "selected projects",
            "key engagements", "achievements",
        ]

    for section, variants in section_variants.items():
        if not any(v in text_lower for v in variants):
            errors.append(f"Missing required section: {section} (or variant)")

    # 2. Check name preserved
    full_name = personal.get("full_name", "")
    if full_name and full_name.lower() not in text_lower:
        warnings.append(f"Name '{full_name}' missing -- will be injected")

    # 3. Check companies preserved
    for company in resume_facts.get("preserved_companies", []):
        if company.lower() not in text_lower:
            errors.append(f"Company '{company}' missing -- cannot remove real experience")

    # 4. Check projects preserved
    for project in resume_facts.get("preserved_projects", []):
        if project.lower() not in text_lower:
            warnings.append(f"Project '{project}' not found -- may have been renamed")

    # 5. Check school preserved
    preserved_school = resume_facts.get("preserved_school", "")
    if preserved_school and preserved_school.lower() not in text_lower:
        errors.append(f"Education '{preserved_school}' missing")

    # 6. Check contact info preserved
    email = personal.get("email", "")
    phone = personal.get("phone", "")
    if email and email.lower() not in text_lower:
        warnings.append("Email missing -- will be injected")
    if phone and phone not in text:
        warnings.append("Phone missing -- will be injected")

    watchlist = _get_effective_watchlist(profile, original_text=original_text)

    # 7. Scan TECHNICAL SKILLS / CORE COMPETENCIES section for fabricated tools
    skills_start = text_lower.find("technical skills")
    if skills_start == -1:
        skills_start = text_lower.find("core competencies")
    skills_end = text_lower.find("experience", skills_start) if skills_start != -1 else -1
    if skills_start != -1 and skills_end != -1:
        skills_block = text_lower[skills_start:skills_end]
        for fake in watchlist:
            if _matches_watchlist_item(fake, skills_block):
                errors.append(f"FABRICATED SKILL in Technical Skills: '{fake}'")

    # 8. Scan full document for fabrication watchlist items not in original
    if original_text:
        original_lower = original_text.lower()
        for fake in watchlist:
            if _matches_watchlist_item(fake, text_lower) and not _matches_watchlist_item(fake, original_lower):
                warnings.append(f"New tool/skill appeared: '{fake}' (not in original)")

    # 9. Em dashes
    if "\u2014" in text or "\u2013" in text:
        errors.append("Contains em dash or en dash.")

    # 10. Banned words
    found_banned = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", text_lower)]
    if found_banned:
        errors.append(f"Banned words: {', '.join(found_banned[:5])}")

    # 11. LLM self-talk leak detection
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in text_lower]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # 12. Duplicate section detection
    for section_name in ["summary", "experience", "education", "projects"]:
        count = text_lower.count(f"\n{section_name}\n") + text_lower.count(f"\n{section_name} \n")
        if text_lower.startswith(f"{section_name}\n"):
            count += 1
        if count > 1:
            errors.append(f"Section '{section_name}' appears {count} times.")

    return {
        "passed": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


# ── Cover Letter Validation ──────────────────────────────────────────────

def validate_cover_letter(text: str, mode: str = "normal") -> dict:
    """Programmatic validation of a cover letter."""
    errors: list[str] = []
    warnings: list[str] = []
    text_lower = text.lower()

    # 1. Em dashes
    if "\u2014" in text or "\u2013" in text:
        errors.append("Contains em dash or en dash.")

    # 2. Banned words
    if mode != "lenient":
        found = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", text_lower)]
        if found:
            msg = f"Banned words: {', '.join(found[:5])}"
            if mode == "strict":
                errors.append(msg)
            else:
                warnings.append(msg)

    # 3. Word count
    words = len(text.split())
    if mode == "strict" and words > 250:
        errors.append(f"Too long ({words} words). Max 250.")
    elif mode == "normal" and words > 275:
        warnings.append(f"Long ({words} words). Target 250.")

    # 4. LLM self-talk
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in text_lower]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # 5. Must start with "Dear"
    stripped = text.strip()
    if not stripped.lower().startswith("dear"):
        errors.append("Must start with 'Dear Hiring Manager,'")

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}
