"""Resume and cover letter validation: banned words, fabrication detection, structural checks.

All validation is profile-driven and domain-adaptive (supporting both Software/AI Engineering
and Chartered Accountancy / ACA / Finance profiles).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, TypedDict

log = logging.getLogger(__name__)


class ValidationResult(TypedDict):
    passed: bool
    errors: list[str]
    warnings: list[str]


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
#
# This is a *denylist of high-risk, commonly hallucinated* technologies, not a
# complete profile allowlist: `_get_effective_watchlist` removes every entry the
# candidate's own `skills_boundary`, certifications, or base resume proves they
# have. It must therefore cover the tools a tailoring LLM reaches for first.
FABRICATION_WATCHLIST: set[str] = {
    "c#", "c++", "golang", "rust", "ruby",
    "kotlin", "swift", "scala", "matlab",
    "spring", "django", "rails", "angular", "vue", "svelte",
    "certif", "certified", "pmp", "scrum master", "aws certified",
    # Data warehouse / lakehouse / cloud platforms
    "snowflake", "databricks", "hadoop", "azure", "terraform",
    # Corporate finance / ERP / planning platforms
    "workday", "netsuite", "anaplan",
    # BI & analytics platforms
    "tableau",
    # CI/CD, configuration management & integration
    "jenkins", "ansible", "mulesoft",
    # CRM & mobile
    "salesforce", "flutter",
}

REQUIRED_SECTIONS: set[str] = {"SUMMARY", "TECHNICAL SKILLS", "EXPERIENCE", "PROJECTS", "EDUCATION"}

# Tokens that only qualify an organisation name ("PwC Holdings Ltd") and carry no
# identity of their own, so they must not be required to appear in the profile.
_ORG_SUFFIX_TOKENS: frozenset[str] = frozenset({
    "ltd", "limited", "plc", "inc", "llc", "llp", "corp", "corporation",
    "gmbh", "ag", "se", "bv", "nv", "sa", "sas", "pte", "pty",
    "group", "holding", "holdings", "company", "co",
    "and", "the", "of", "for", "international",
})

# Numeric metric capture: 100, 1000, 25, 3.75 -- comma grouping is stripped.
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


# ── Helpers ───────────────────────────────────────────────────────────────

def detect_profile_domain(profile: dict[str, Any]) -> str:
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


def _build_skills_set(profile: dict[str, Any]) -> set[str]:
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


def _get_effective_watchlist(profile: dict[str, Any], original_text: str = "") -> set[str]:
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
    # Symbol-bearing items ("c#", "c++") are meaningful at two characters, so the
    # length guard below must not reject them before the symbol-aware path runs.
    symbol_item = any(ch in item for ch in ("#", "+"))
    if len(item) <= 2 and not symbol_item:
        return False
    if symbol_item:
        pattern = r"(?<![a-z0-9])" + re.escape(item) + r"(?![a-z0-9#+])"
        return bool(re.search(pattern, text_lower))
    if item == "certif":
        return bool(re.search(r"\bcertif\w*", text_lower))
    return bool(re.search(r"\b" + re.escape(item) + r"\b", text_lower))


# ── Fabrication evidence helpers (metrics & employers) ──────────────────────

def _metric_tokens(text: str) -> set[str]:
    """Return the normalised numeric tokens in ``text``.

    Comma grouping is removed so ``1,200`` and ``1200`` compare equal. Four-digit
    values in the 1900-2099 range are calendar years, not achievements, and are
    ignored by :func:`_check_metric_fabrication`.
    """
    return {match.group(0).replace(",", "") for match in _NUMBER_RE.finditer(text)}


def _allowed_metric_tokens(profile: dict[str, Any], original_text: str = "") -> set[str]:
    """Numbers the candidate is actually allowed to claim.

    Ground truth is `resume_facts.real_metrics` plus the base resume. The rest of
    the profile is included too, because standards, qualifications and grade
    point averages ("IFRS 9", "FRS 102", "CGPA: 3.75/4.00") legitimately carry
    numbers that are not achievements at all.
    """
    allowed: set[str] = _metric_tokens(original_text or "")
    for metric in profile.get("resume_facts", {}).get("real_metrics", []) or []:
        allowed |= _metric_tokens(str(metric))
    try:
        allowed |= _metric_tokens(json.dumps(profile, ensure_ascii=False, default=str))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        log.debug("Could not derive metric vocabulary from profile", exc_info=True)
    return allowed


def _is_calendar_year(token: str) -> bool:
    """True when a numeric token looks like a year rather than an achievement."""
    if "." in token:
        return False
    try:
        return 1900 <= int(token) <= 2099
    except ValueError:  # pragma: no cover - regex guarantees digits
        return False


def _check_metric_fabrication(
    text: str, profile: dict[str, Any], original_text: str = "", limit: int = 3
) -> list[str]:
    """Flag numeric claims that appear in neither the base resume nor real_metrics."""
    allowed = _allowed_metric_tokens(profile, original_text)
    unknown: list[str] = []
    for token in sorted(_metric_tokens(text), key=lambda value: (-len(value), value)):
        if token in allowed or _is_calendar_year(token):
            continue
        unknown.append(token)
        if len(unknown) >= limit:
            break
    return [f"Unverified metric: '{value}' (absent from the resume and real_metrics)" for value in unknown]


def _org_tokens(phrase: str) -> list[str]:
    """Split an organisation name into the tokens that actually identify it."""
    return [
        token
        for token in re.findall(r"[A-Za-z][A-Za-z&'\-]{2,}", phrase)
        if token.lower() not in _ORG_SUFFIX_TOKENS
    ]


def _known_employer_tokens(profile: dict[str, Any], original_text: str = "") -> set[str]:
    """Organisation tokens the candidate has actually worked for."""
    sources: list[str] = []
    for company in profile.get("resume_facts", {}).get("preserved_companies", []) or []:
        sources.append(str(company))
    experience = profile.get("experience", {})
    for key in ("current_company", "previous_companies", "employers"):
        value = experience.get(key)
        if isinstance(value, str):
            sources.append(value)
        elif isinstance(value, list):
            sources.extend(str(item) for item in value)
    sources.append(original_text or "")
    known: set[str] = set()
    for source in sources:
        known |= {token.lower() for token in _org_tokens(source)}
    return known


def _employer_phrase(header: str) -> str:
    """Return the employer segment of an experience/project header line.

    The tailoring prompt documents the `"Title at Company"` shape, so only that
    explicit separator (or a comma/dash split of an already separated line) is
    trusted. A header without a separator yields ``""`` and is never checked, so
    role-only lines can never raise a false fabrication.
    """
    text = header.split("|", 1)[0].strip()
    lowered = text.lower()
    if " at " in lowered:
        return text[lowered.rfind(" at ") + 4 :].strip(" \t-–—|,(")
    if "," in text:
        tail = text.rsplit(",", 1)[-1].strip()
        if tail:
            return tail.strip(" \t-–—|,(")
    return ""


def _check_unknown_employers(
    headers: list[str], profile: dict[str, Any], original_text: str = ""
) -> list[str]:
    """Flag employer names in entry headers that the profile cannot account for."""
    known = _known_employer_tokens(profile, original_text)
    errors: list[str] = []
    for header in headers:
        employer = _employer_phrase(header)
        if not employer:
            continue
        tokens = _org_tokens(employer)
        if not tokens:
            continue
        unknown = [token for token in tokens if token.lower() not in known]
        if unknown:
            errors.append(
                f"Unverified employer '{employer}' in '{header.strip()}' "
                f"(not in preserved_companies or the base resume)"
            )
    return errors


def sanitize_text(text: str) -> str:
    """Auto-fix common LLM output issues instead of rejecting."""
    text = text.replace(" \u2014 ", ", ").replace("\u2014", ", ")   # em dash -> comma
    text = text.replace("\u2013", "-")    # en dash -> hyphen
    text = text.replace("\u201c", '"').replace("\u201d", '"')   # smart double quotes
    text = text.replace("\u2018", "'").replace("\u2019", "'")   # smart single quotes
    return text.strip()


# ── JSON Field Validation ─────────────────────────────────────────────────

def validate_json_fields(
    data: dict[str, Any],
    profile: dict[str, Any],
    mode: str = "normal",
    original_text: str = "",
) -> ValidationResult:
    """Validate individual JSON fields from an LLM-generated tailored resume.

    Args:
        data:    Parsed JSON from the LLM (title, summary, skills, experience, projects, education).
        profile: User profile dict from load_profile().
        mode:    Validation strictness — "strict", "normal", or "lenient".
        original_text: Base resume text, used so real skills/metrics/employers are
            never reported as fabrications.

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

    # The skills dict is part of the resume body, so it must join the checked text.
    skills_values: list[str] = []
    if isinstance(data["skills"], dict):
        skills_values = [str(value) for value in data["skills"].values()]
    all_text_parts: list[str] = [str(data["summary"]), *skills_values]
    watchlist = _get_effective_watchlist(profile, original_text=original_text)

    # Skills: check for fabrication against profile-aware watchlist
    skills_text = " ".join(skills_values).lower()
    for fake in watchlist:
        if _matches_watchlist_item(fake, skills_text):
            errors.append(f"Fabricated skill: '{fake}'")

    # Experience: preserved companies must be present
    resume_facts = profile.get("resume_facts", {})
    preserved_companies = resume_facts.get("preserved_companies", [])

    entry_headers: list[str] = []
    if isinstance(data["experience"], list):
        for company in preserved_companies:
            has_company = any(
                company.lower() in str(e.get("header", "")).lower()
                for e in data["experience"]
            )
            if not has_company:
                errors.append(f"Company '{company}' missing from experience")
        for entry in data["experience"]:
            entry_headers.append(str(entry.get("header", "")))
            all_text_parts.extend(str(b) for b in entry.get("bullets", []))

    # Projects: collect bullets if present
    if isinstance(data.get("projects"), list):
        for entry in data["projects"]:
            entry_headers.append(str(entry.get("header", "")))
            all_text_parts.extend(str(b) for b in entry.get("bullets", []))

    # Certifications / Qualifications: collect if present
    if data.get("certifications"):
        all_text_parts.append(str(data["certifications"]))

    education_text = str(data.get("education", ""))

    # Education: preserved school must be present
    preserved_school = resume_facts.get("preserved_school", "")
    if preserved_school and preserved_school.lower() not in education_text.lower():
        errors.append(f"Education '{preserved_school}' missing")

    # Employers: the tailoring engine may reword entries but never invent one.
    errors.extend(_check_unknown_employers(entry_headers, profile, original_text))

    # Bulk text checks
    body_text = " ".join([*all_text_parts, education_text])
    all_text = body_text.lower()

    # LLM self-talk is always an error regardless of mode
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in all_text]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # Fabricated metrics: the judge may allow "minor stretches", so every numeric
    # claim has to be traceable to the base resume or the recorded real_metrics.
    metric_errors = _check_metric_fabrication(body_text, profile, original_text)
    if mode == "lenient":
        warnings.extend(metric_errors)
    else:
        errors.extend(metric_errors)

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

def validate_tailored_resume(text: str, profile: dict[str, Any], original_text: str = "") -> ValidationResult:
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
    if skills_start != -1:
        skills_end = text_lower.find("experience", skills_start + 1)
        # A resume without an "experience" heading must still have its skills block
        # checked: fall through to the end of the document instead of checking nothing.
        if skills_end == -1 or skills_end <= skills_start:
            skills_end = len(text_lower)
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

def validate_cover_letter(text: str, mode: str = "normal") -> ValidationResult:
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
