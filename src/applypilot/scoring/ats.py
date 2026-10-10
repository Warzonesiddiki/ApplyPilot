"""Deterministic ATS Keyword Extraction & Coverage Engine.

Measures exact keyword and phrase overlap between a job description and a
candidate's resume (both baseline and tailored) with zero LLM calls.
Supports Finance/Accounting (ACA, ACCA, IFRS, UK GAAP, Audit, FP&A, ERP)
as well as Software/AI/Data Engineering.
"""

from __future__ import annotations

import re
from typing import Any, NotRequired, TypedDict

# Canonical ATS keyword taxonomy: (display_label, regex_pattern)
_CANONICAL_KEYWORDS: tuple[tuple[str, str], ...] = (
    # Accounting, Audit, Tax & Finance Qualifications / Standards
    ("ACA", r"\b(?:ACA|ICAEW)\b"),
    ("ACCA", r"\bACCA\b"),
    ("CIMA", r"\bCIMA\b"),
    ("CPA", r"\bCPA\b"),
    ("CFA", r"\bCFA\b"),
    ("Chartered Accountant", r"\bChartered\s+Accountant(?:s)?\b"),
    ("IFRS", r"\bIFRS\b"),
    ("IFRS 15", r"\bIFRS\s*15\b"),
    ("IFRS 16", r"\bIFRS\s*16\b"),
    ("IFRS 9", r"\bIFRS\s*9\b"),
    ("IFRS 3", r"\bIFRS\s*3\b"),
    ("IFRS 10", r"\bIFRS\s*10\b"),
    ("IAS", r"\bIAS(?:\s*\d+)?\b"),
    ("UK GAAP", r"\bUK\s+GAAP\b"),
    ("US GAAP", r"\bUS\s+GAAP\b"),
    ("FRS 102", r"\bFRS\s*102\b"),
    ("FRS 101", r"\bFRS\s*101\b"),
    ("ISA", r"\bISA(?:s)?\b"),
    ("Statutory Audit", r"\bstatutory\s+audit(?:s|ing)?\b"),
    ("External Audit", r"\bexternal\s+audit(?:s|ing)?\b"),
    ("Internal Audit", r"\binternal\s+audit(?:s|ing)?\b"),
    ("SOX", r"\b(?:SOX|Sarbanes[-\s]Oxley|ICFR)\b"),
    ("COSO", r"\bCOSO\b"),
    ("Internal Controls", r"\binternal\s+control(?:s)?\b"),
    ("FP&A", r"\b(?:FP&A|Financial\s+Planning\s+(?:&|and)\s+Analysis)\b"),
    ("Budgeting", r"\bbudget(?:ing|s)?\b"),
    ("Forecasting", r"\b(?:rolling\s+)?forecast(?:ing|s)?\b"),
    ("Variance Analysis", r"\bvariance\s+analysis\b"),
    ("Financial Modeling", r"\bfinancial\s+model(?:l?ing|s)\b"),
    ("DCF", r"\b(?:DCF|discounted\s+cash\s+flow)\b"),
    ("Consolidation", r"\bconsolidat(?:ion|ed)\b"),
    ("Multi-Entity", r"\bmulti[-\s]entity\b"),
    ("Month-End Close", r"\bmonth[-\s]end(?:\s+close)?\b"),
    ("Year-End Close", r"\byear[-\s]end(?:\s+close)?\b"),
    ("General Ledger", r"\b(?:general\s+ledger|GL)\b"),
    ("Accounts Payable", r"\b(?:accounts\s+payable|AP)\b"),
    ("Accounts Receivable", r"\b(?:accounts\s+receivable|AR)\b"),
    ("Working Capital", r"\bworking\s+capital\b"),
    ("Cash Flow", r"\bcash\s+flow(?:s)?\b"),
    ("Balance Sheet", r"\bbalance\s+sheet(?:s)?\b"),
    ("P&L", r"\b(?:P&L|profit\s+(?:&|and)\s+loss)\b"),
    ("Management Accounts", r"\bmanagement\s+account(?:s|ing)\b"),
    ("Statutory Accounts", r"\bstatutory\s+(?:account(?:s|ing)|financial\s+statements)\b"),
    ("Financial Reporting", r"\bfinancial\s+reporting\b"),
    ("Revenue Recognition", r"\brevenue\s+recognition\b"),
    ("VAT", r"\bVAT\b"),
    ("Corporation Tax", r"\b(?:corporation|corporate)\s+tax\b"),
    ("Tax Compliance", r"\btax\s+compliance\b"),
    ("Transfer Pricing", r"\btransfer\s+pricing\b"),
    ("Due Diligence", r"\bdue\s+diligence\b"),
    ("Risk Management", r"\brisk\s+management\b"),
    ("KPIs", r"\bKPI(?:s)?\b"),
    # ERP, BI & Financial Systems
    ("SAP", r"\bSAP(?:\s+S/4HANA|\s+FI/?CO|\s+ERP)?\b"),
    ("Oracle", r"\bOracle(?:\s+Financials|\s+Fusion|\s+EBS)?\b"),
    ("NetSuite", r"\bNetSuite\b"),
    ("Dynamics 365", r"\b(?:Dynamics\s*365|MS\s+Dynamics|Microsoft\s+Dynamics|Navision)\b"),
    ("Zoho Books", r"\bZoho(?:\s+Books)?\b"),
    ("QuickBooks", r"\bQuickBooks\b"),
    ("Xero", r"\bXero\b"),
    ("Sage", r"\bSage(?:\s+50|\s+200|\s+Intacct)?\b"),
    ("Workday", r"\bWorkday(?:\s+Financials)?\b"),
    ("Anaplan", r"\bAnaplan\b"),
    ("Hyperion", r"\b(?:Hyperion|HFM|Essbase)\b"),
    ("Power BI", r"\bPower\s*BI\b"),
    ("Tableau", r"\bTableau\b"),
    ("Looker", r"\bLooker\b"),
    ("Alteryx", r"\bAlteryx\b"),
    ("Excel", r"\bExcel\b"),
    ("Power Query", r"\bPower\s+Query\b"),
    ("VBA", r"\bVBA\b"),
    ("SQL", r"\bSQL\b"),
    ("ERP Implementation", r"\bERP(?:\s+implementation)?\b"),
    # Software, AI & Data Engineering
    ("Python", r"\bPython\b"),
    ("TypeScript", r"\bTypeScript\b"),
    ("JavaScript", r"\bJavaScript\b"),
    ("Go", r"\b(?:Golang|Go\s+programming)\b"),
    ("Rust", r"\bRust\b"),
    ("Java", r"\bJava\b"),
    ("C++", r"\bC\+\+"),
    ("React", r"\bReact(?:\.js|JS)?\b"),
    ("Next.js", r"\bNext\.?js\b"),
    ("Node.js", r"\bNode\.?js\b"),
    ("FastAPI", r"\bFastAPI\b"),
    ("Django", r"\bDjango\b"),
    ("Flask", r"\bFlask\b"),
    ("GraphQL", r"\bGraphQL\b"),
    ("REST API", r"\bREST(?:ful)?\s+API(?:s)?\b"),
    ("PostgreSQL", r"\b(?:PostgreSQL|Postgres)\b"),
    ("MySQL", r"\bMySQL\b"),
    ("MongoDB", r"\bMongoDB\b"),
    ("Redis", r"\bRedis\b"),
    ("Kafka", r"\bKafka\b"),
    ("Docker", r"\bDocker\b"),
    ("Kubernetes", r"\b(?:Kubernetes|K8s)\b"),
    ("Terraform", r"\bTerraform\b"),
    ("AWS", r"\bAWS|Amazon\s+Web\s+Services\b"),
    ("Azure", r"\bAzure\b"),
    ("GCP", r"\b(?:GCP|Google\s+Cloud)\b"),
    ("CI/CD", r"\bCI/CD\b"),
    ("Microservices", r"\bmicroservices\b"),
    ("PyTorch", r"\bPyTorch\b"),
    ("LangChain", r"\bLangChain\b"),
    ("LangGraph", r"\bLangGraph\b"),
    ("LLM", r"\bLLM(?:s)?|Large\s+Language\s+Model(?:s)?\b"),
    ("RAG", r"\bRAG|Retrieval[-\s]Augmented\s+Generation\b"),
    ("Spark", r"\b(?:Apache\s+)?Spark\b"),
    ("Snowflake", r"\bSnowflake\b"),
    ("Databricks", r"\bDatabricks\b"),
    ("Airflow", r"\bAirflow\b"),
    ("dbt", r"\bdbt\b"),
)

class ATSCoverageResult(TypedDict):
    coverage_pct: int
    baseline_pct: int
    delta_pct: int
    matched_keywords: list[str]
    missing_keywords: list[str]
    matched: NotRequired[list[str]]
    missing: NotRequired[list[str]]
    total_jd_keywords: int


_COMPILED_KEYWORDS: list[tuple[str, re.Pattern[str]]] = [
    (label, re.compile(pat, re.IGNORECASE)) for label, pat in _CANONICAL_KEYWORDS
]


def extract_jd_keywords(
    job_description: str | None,
    profile: dict[str, Any] | None = None,
) -> list[tuple[str, re.Pattern[str]]]:
    """Extract canonical + profile-boundary ATS keywords present in a job description."""
    if not job_description:
        return []

    found: list[tuple[str, re.Pattern[str]]] = []
    seen_labels: set[str] = set()

    for label, pat in _COMPILED_KEYWORDS:
        if pat.search(job_description):
            found.append((label, pat))
            seen_labels.add(label.lower())

    # Also include skills from the candidate's profile skills_boundary if they appear in the JD
    if profile and isinstance(profile.get("skills_boundary"), dict):
        for items in profile["skills_boundary"].values():
            if not isinstance(items, list):
                continue
            for item in items:
                s = str(item).strip()
                if not s or s.lower() in seen_labels or len(s) < 3:
                    continue
                pat = re.compile(r"\b" + re.escape(s) + r"\b", re.IGNORECASE)
                if pat.search(job_description):
                    found.append((s, pat))
                    seen_labels.add(s.lower())

    return found


def compute_ats_coverage(
    job_description: str | None,
    tailored_resume: str | None,
    base_resume: str | None = None,
    profile: dict[str, Any] | None = None,
) -> ATSCoverageResult:
    """Compute deterministic ATS keyword coverage for a tailored resume vs. a job description.

    Returns:
        {
            "coverage_pct": int,         # 0-100 for tailored_resume
            "baseline_pct": int,         # 0-100 for base_resume (if provided)
            "delta_pct": int,            # coverage_pct - baseline_pct
            "matched_keywords": list[str],
            "missing_keywords": list[str],
            "total_jd_keywords": int,
        }
    """
    jd_kws = extract_jd_keywords(job_description, profile=profile)
    if not jd_kws:
        return {
            "coverage_pct": 100 if tailored_resume else 0,
            "baseline_pct": 100 if base_resume else 0,
            "delta_pct": 0,
            "matched_keywords": [],
            "missing_keywords": [],
            "total_jd_keywords": 0,
        }

    tailored_txt = tailored_resume or ""
    base_txt = base_resume or ""

    matched: list[str] = []
    missing: list[str] = []
    base_matched_count = 0

    for label, pat in jd_kws:
        if pat.search(tailored_txt):
            matched.append(label)
        else:
            missing.append(label)
        if base_txt and pat.search(base_txt):
            base_matched_count += 1

    total = len(jd_kws)
    cov_pct = int(round((len(matched) / total) * 100))
    base_pct = int(round((base_matched_count / total) * 100)) if base_txt else cov_pct

    return {
        "coverage_pct": cov_pct,
        "baseline_pct": base_pct,
        "delta_pct": cov_pct - base_pct,
        "matched_keywords": matched,
        "missing_keywords": missing,
        "matched": matched,
        "missing": missing,
        "total_jd_keywords": total,
    }


def select_best_base_resume(
    job_description: str | None,
    default_resume_path: object | None = None,
    profile: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Select the highest-ATS-coverage base resume variant for a job description.

    Checks `<APP_DIR>/resume.txt`, `<APP_DIR>/resume_*.txt`, and `<APP_DIR>/variants/*.txt`.

    Returns:
        Tuple of `(resume_text, variant_name)`.
    """
    from pathlib import Path
    from applypilot import config

    base_path = Path(str(default_resume_path)) if default_resume_path else config.RESUME_PATH
    candidates: list[Path] = []
    if base_path.exists():
        candidates.append(base_path)

    app_dir = base_path.parent
    if app_dir.exists():
        for p in sorted(app_dir.glob("resume_*.txt")):
            if p not in candidates:
                candidates.append(p)
        variants_dir = app_dir / "variants"
        if variants_dir.is_dir():
            for p in sorted(variants_dir.glob("*.txt")):
                if p not in candidates:
                    candidates.append(p)

    if not candidates:
        return ("", "default")

    if len(candidates) == 1 or not (job_description or "").strip():
        return (candidates[0].read_text(encoding="utf-8"), candidates[0].stem)

    best_text = candidates[0].read_text(encoding="utf-8")
    best_name = candidates[0].stem
    best_cov = -1

    for cand in candidates:
        try:
            txt = cand.read_text(encoding="utf-8")
        except Exception:
            continue
        cov = compute_ats_coverage(job_description, txt, profile=profile)["coverage_pct"]
        if cov > best_cov:
            best_cov = cov
            best_text = txt
            best_name = cand.stem

    return (best_text, best_name)

