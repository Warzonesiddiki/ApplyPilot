"""UK & EU Visa Sponsorship Matcher & Structured Job Intelligence.

Cross-references employer names against curated UK Home Office Skilled Worker
Licensed Sponsors, Netherlands IND Recognized Sponsors, Ireland Critical Skills
Employers, and Pan-EU Blue Card Sponsors, combined with job description NLP signals.
Also classifies work arrangement (remote / hybrid / onsite) and detects expired listings.
"""

from __future__ import annotations

import re

# Curated registry of verified UK Home Office Skilled Worker & EU Recognized Sponsors
# (normalized lowercase substrings).
LICENSED_UK_EU_SPONSORS: tuple[str, ...] = (
    # Big 4 & Top Accountancy / Audit / Advisory Firms
    "pwc", "pricewaterhousecoopers", "deloitte", "ernst & young", "ey ", "kpmg",
    "bdo", "grant thornton", "rsm", "forvis mazars", "mazars", "crowe",
    "baker tilly", "moore kingston", "pkf", "saffery", "evelyn partners",
    "azets", "alvarez & marsal", "alvarez and marsal", "fti consulting",
    "kroll", "accenture", "capgemini", "mckinsey", "boston consulting", "bain & company",
    "oliver wyman", "kearney", "roland berger", "pa consulting",
    # UK & European Banking, FinTech, Payments & Financial Services
    "barclays", "hsbc", "natwest", "lloyds banking", "lloyds bank", "standard chartered",
    "santander", "revolut", "wise", "transferwise", "monzo", "starling bank",
    "checkout.com", "stripe", "adyen", "klarna", "n26", "trade republic",
    "qonto", "mollie", "gocardless", "oaknorth", "zopa", "marqeta", "plaid",
    "sumup", "pleo", "spendesk", "alan", "deutsche bank", "commerzbank",
    "ubs", "julius baer", "ing ", "ing group", "ing bank", "abn amro", "rabobank",
    "bnp paribas", "societe generale", "société générale", "credit agricole",
    "unicredit", "intesa sanpaolo", "bbva", "nordea", "danske bank", "seb ",
    "swedbank", "axa", "allianz", "aviva", "zurich insurance", "zurich",
    "legal & general", "prudential", "swiss re", "munich re", "hiscox",
    "lseg", "london stock exchange", "euroclear", "clearstream", "swift",
    "euronext", "blackrock", "vanguard", "fidelity", "schroders", "man group",
    "baillie gifford", "citadel", "jane street", "optiver", "imc trading",
    "flow traders", "jpmorgan", "jp morgan", "goldman sachs", "morgan stanley",
    "citigroup", "citi ", "bank of america", "macquarie", "nomura", "jefferies",
    "lazard", "rothschild", "mastercard", "visa", "american express", "paypal",
    "fis ", "fiserv", "worldpay", "experian", "equifax", "transunion",
    "moody's", "s&p global", "bloomberg", "msci", "state street", "bny mellon",
    "northern trust", "brookfield", "blackstone", "kkr", "carlyle", "apollo",
    "apax", "cvc capital", "eqt", "permira", "cinven", "bridgepoint",
    # UK & EU Tech, AI, Pharma, Industrials & Multinationals
    "astrazeneca", "gsk", "glaxosmithkline", "unilever", "haleon", "reckitt",
    "diageo", "relx", "compass group", "bae systems", "rolls-royce", "dyson",
    "arm ", "arm limited", "deliveroo", "ocado", "auto trader", "rightmove",
    "sage", "darktrace", "wayve", "synthesia", "elevenlabs", "deepmind",
    "google", "alphabet", "meta ", "amazon", "aws", "microsoft", "apple",
    "netflix", "nvidia", "amd", "intel", "qualcomm", "salesforce", "adobe",
    "servicenow", "workday", "oracle", "sap", "ibm", "cisco", "cloudflare",
    "databricks", "snowflake", "datadog", "elastic", "mongodb", "confluent",
    "palantir", "twilio", "okta", "crowdstrike", "palo alto networks",
    "zscaler", "fortinet", "spotify", "booking.com", "booking holdings",
    "zalando", "delivery hero", "hellofresh", "personio", "celonis",
    "contentful", "deepl", "helsing", "mistral", "doctolib", "criteo",
    "ubisoft", "dassault", "schneider electric", "sanofi", "l'oreal", "lvmh",
    "airbus", "siemens", "bmw", "mercedes-benz", "volkswagen", "porsche",
    "bosch", "basf", "bayer", "merck", "henkel", "infineon", "asml",
    "philips", "shell", "bp ", "totalenergies", "equinor", "orsted", "vestas",
    "maersk", "novo nordisk", "carlsberg", "lego", "ikea", "ericsson",
    "volvo", "atlas copco", "sandvik", "abb ", "roche", "novartis", "nestle",
    "glencore", "chubb", "medtronic", "eaton", "johnson controls", "icon plc",
    "ryanair", "crh", "flutter", "entain", "thomson reuters", "finnet",
)

_NO_SPONSORSHIP_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(?:no|not|cannot|unable\s+to|do\s+not|does\s+not|doesn't|won't)\s+(?:offer|provide|support|consider)\s+(?:visa\s+)?sponsorship\b",
        r"\bwithout\s+(?:the\s+need\s+for\s+|requiring\s+)?(?:visa\s+)?sponsorship\b",
        r"\bmust\s+have\s+(?:the\s+)?(?:existing\s+|permanent\s+|unrestricted\s+)?right\s+to\s+work\b",
        r"\bsponsorship\s+is\s+not\s+available\b",
        r"\bno\s+visa\s+sponsorship\b",
        r"\bus\s+citizens?\s+only\b",
        r"\bsecurity\s+clearance\s+required\b",
        r"\bdv\s+cleared\b",
        r"\bsc\s+cleared\s+only\b",
    )
)

_SPONSORSHIP_OFFERED_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bvisa\s+sponsorship\s+(?:is\s+)?(?:available|offered|provided|supported)\b",
        r"\b(?:we\s+)?(?:offer|provide|support)\s+(?:skilled\s+worker\s+)?visa\s+sponsorship\b",
        r"\bskilled\s+worker\s+visa\s+sponsorship\b",
        r"\brelocation\s+(?:and\s+visa\s+)?(?:support|package|assistance)\b",
        r"\beu\s+blue\s+card\s+sponsorship\b",
        r"\bcritical\s+skills\s+employment\s+permit\b",
        r"\bind\s+recognized\s+sponsor\b",
        r"\b30%\s+ruling\b",
    )
)

_EXPIRED_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bthis\s+job\s+is\s+no\s+longer\s+(?:available|accepting\s+applications|open)\b",
        r"\bno\s+longer\s+accepting\s+applications\b",
        r"\bthis\s+position\s+has\s+been\s+(?:filled|closed)\b",
        r"\bjob\s+(?:posting|listing)\s+has\s+expired\b",
        r"\bsorry,\s+this\s+job\s+(?:has\s+expired|is\s+closed)\b",
        r"\bvacancy\s+is\s+now\s+closed\b",
    )
)

_QUALIFICATION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("ACA", re.compile(r"\b(?:ACA|ICAEW)\b")),
    ("ACCA", re.compile(r"\bACCA\b")),
    ("CIMA", re.compile(r"\bCIMA\b")),
    ("CPA", re.compile(r"\bCPA\b")),
    ("CFA", re.compile(r"\bCFA\b")),
    ("IFRS", re.compile(r"\bIFRS(?:\s*\d+)?\b", re.IGNORECASE)),
    ("UK GAAP", re.compile(r"\b(?:UK\s+GAAP|FRS\s*10[12])\b", re.IGNORECASE)),
    ("ISA", re.compile(r"\bISA\s+(?:audit|standards)|\bstatutory\s+audit\b", re.IGNORECASE)),
    ("SOX", re.compile(r"\b(?:SOX|Sarbanes[-\s]Oxley|ICFR)\b", re.IGNORECASE)),
    ("FP&A", re.compile(r"\bFP&A|Financial\s+Planning\s+(?:&|and)\s+Analysis\b", re.IGNORECASE)),
]


def is_licensed_sponsor(company: str | None) -> bool:
    """Return True if the company matches a known UK Home Office or EU Recognized Sponsor."""
    if not company:
        return False
    comp = f" {company.strip().lower()} "
    return any(sp in comp for sp in LICENSED_UK_EU_SPONSORS)


def detect_sponsor_status(
    company: str | None,
    title: str | None = None,
    description: str | None = None,
    location: str | None = None,
) -> str:
    """Classify a job's visa sponsorship status.

    Returns one of:
      - 'no_sponsorship_stated': JD explicitly rules out sponsorship or requires clearance
      - 'sponsorship_offered': JD explicitly mentions visa sponsorship / relocation support
      - 'licensed_sponsor': Employer is a known UK Home Office / EU Licensed Sponsor
      - 'unknown': No explicit signal found
    """
    combined = " ".join(p for p in (title, location, description) if p)

    if combined:
        for pat in _NO_SPONSORSHIP_PATTERNS:
            if pat.search(combined):
                return "no_sponsorship_stated"

        for pat in _SPONSORSHIP_OFFERED_PATTERNS:
            if pat.search(combined):
                return "sponsorship_offered"

    if is_licensed_sponsor(company):
        return "licensed_sponsor"

    return "unknown"


def detect_work_arrangement(
    title: str | None = None,
    location: str | None = None,
    description: str | None = None,
) -> str:
    """Classify work arrangement as 'remote', 'hybrid', or 'onsite'."""
    header = " ".join(p for p in (title, location) if p).lower()
    body = (description or "")[:3000].lower()

    if any(k in header for k in ("remote", "work from home", "wfh", "distributed", "home-based", "anywhere")):
        if "hybrid" in header:
            return "hybrid"
        return "remote"

    if "hybrid" in header or "hybrid" in body:
        return "hybrid"

    if any(k in body for k in ("100% remote", "fully remote", "work remotely from anywhere")):
        return "remote"

    return "onsite"


def is_job_expired(description: str | None, title: str | None = None) -> bool:
    """Return True if the job description or page text indicates an expired/closed listing."""
    combined = " ".join(p for p in (title, description) if p)
    if not combined:
        return False
    return any(pat.search(combined) for pat in _EXPIRED_PATTERNS)


def extract_qualifications(description: str | None) -> list[str]:
    """Extract mentioned accounting/finance qualifications & standards from text."""
    if not description:
        return []
    found: list[str] = []
    for label, pat in _QUALIFICATION_PATTERNS:
        if pat.search(description):
            found.append(label)
    return found


def evaluate_sponsorship_and_metadata(
    company: str | None,
    title: str | None = None,
    description: str | None = None,
    location: str | None = None,
) -> dict:
    """Evaluate sponsor status, work arrangement, expiration, and qualifications."""
    return {
        "sponsor_status": detect_sponsor_status(company, title, description, location),
        "work_arrangement": detect_work_arrangement(title, location, description),
        "is_expired": is_job_expired(description, title),
        "qualifications": extract_qualifications(description),
    }
