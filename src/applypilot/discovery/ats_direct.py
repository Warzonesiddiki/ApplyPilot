"""Zero-Browser Direct ATS API Scrapers (Greenhouse, Lever, Ashby, SmartRecruiters).

Scrapes public JSON APIs exposed by employer career portals across the UK, EU,
and global markets. Zero browser overhead and zero LLM tokens — returns full
job descriptions and direct application URLs in a single HTTP request per board,
meaning discovered jobs are immediately pre-enriched for scoring.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from collections.abc import Callable
from typing import Any

import httpx

from applypilot import config
from applypilot.database import close_connection, init_db, store_jobs
from applypilot.discovery.workday import strip_html
from applypilot.network import RetryPolicy, ResponseBodyTooLarge, build_async_client

log = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
_TIMEOUT = 20.0
_MAX_BOARD_CONCURRENCY = 64
_ATS_PERSIST_BATCH_SIZE = 8
# Hard cap on a single ATS board response body, enforced on decoded bytes as
# they stream in. Boards routinely carry thousands of postings with full
# descriptions; 8 MiB is generous headroom while still bounding memory.
_MAX_BOARD_RESPONSE_BYTES = 8_000_000
# A single threshold for "this is a full description". `store_jobs` stamps
# `detail_scraped_at` whenever `full_description` is set and the enrichment
# stage selects `WHERE detail_scraped_at IS NULL`, so a too-low bar permanently
# skips the browser-based three-tier extractor for short blurbs. This matches
# the 500-char floor used by the JobSpy and Workday backends.
_MIN_FULL_DESCRIPTION_CHARS = 500
_ATS_RETRY_POLICY = RetryPolicy(max_attempts=4, base_delay=0.5, max_delay=15.0)
_ATS_RETRY_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.WriteError,
    httpx.WriteTimeout,
)
# SmartRecruiters paginates with an offset. Without walking it a board such as
# BoschGroup (5,033 postings) yielded only the first 100.
_SMARTRECRUITERS_PAGE_LIMIT = 100
_SMARTRECRUITERS_MAX_PAGES = 25
_MAX_KEYWORD_PATTERNS = 512

# ISO-3166 alpha-2 -> readable country name. SmartRecruiters returns `country`
# as a two-letter code, so "Vecsés, hu" was produced and rejected by every
# accept pattern, dropping 100% of the board.
_ISO_ALPHA2_COUNTRIES: dict[str, str] = {
    "at": "Austria", "au": "Australia", "be": "Belgium", "bg": "Bulgaria",
    "br": "Brazil", "ca": "Canada", "ch": "Switzerland", "cy": "Cyprus",
    "cz": "Czechia", "de": "Germany", "dk": "Denmark", "ee": "Estonia",
    "es": "Spain", "fi": "Finland", "fr": "France", "gr": "Greece",
    "hr": "Croatia", "hu": "Hungary", "ie": "Ireland", "il": "Israel",
    "in": "India", "it": "Italy", "jp": "Japan", "kr": "South Korea",
    "lt": "Lithuania", "lu": "Luxembourg", "lv": "Latvia", "mt": "Malta",
    "mx": "Mexico", "nl": "Netherlands", "no": "Norway", "nz": "New Zealand",
    "pl": "Poland", "pt": "Portugal", "ro": "Romania", "se": "Sweden",
    "sg": "Singapore", "si": "Slovenia", "sk": "Slovakia", "tr": "Turkey",
    "ua": "Ukraine", "uk": "United Kingdom", "us": "United States",
    "za": "South Africa",
}


def _extract_query_keywords(search_cfg: dict[str, Any] | None = None) -> list[str]:
    """Extract lowercase query keywords/phrases from searches.yaml for client-side filtering."""
    if search_cfg is None:
        # config.load_search_config() already falls back to the bundled example
        # when the user has no searches.yaml, so never short-circuit on
        # SEARCH_CONFIG_PATH.exists() here -- that silently disables all
        # keyword filtering on a fresh install.
        search_cfg = config.load_search_config()

    queries = search_cfg.get("queries", [])
    keywords: list[str] = []
    for q in queries if isinstance(queries, list) else []:
        term = ""
        if isinstance(q, dict):
            term = str(q.get("query") or q.get("term") or "").strip().lower()
        elif isinstance(q, str):
            term = q.strip().lower()
        if term and term not in keywords:
            keywords.append(term)
            # Also add primary role words (e.g. "accountant", "audit", "fp&a", "engineer", "finance")
            for token in re.split(r"[\s/,&-]+", term):
                if len(token) >= 3 and token not in (
                    "senior", "junior", "lead", "manager", "associate", "specialist", "consultant", "analyst", "the", "and"
                ):
                    if token not in keywords:
                        keywords.append(token)
    return keywords


_keyword_patterns: dict[str, re.Pattern[str]] = {}


def _keyword_pattern(keyword: str) -> re.Pattern[str] | None:
    """Compile (and memoize) a word-boundary matcher for one query keyword."""
    cached = _keyword_patterns.get(keyword)
    if cached is not None:
        return cached
    tokens = [re.escape(part) for part in keyword.split() if part]
    if not tokens:
        return None
    pattern = re.compile(r"(?<![a-z0-9])" + r"[\s/,&-]+".join(tokens) + r"(?![a-z0-9])")
    if len(_keyword_patterns) < _MAX_KEYWORD_PATTERNS:
        _keyword_patterns[keyword] = pattern
    return pattern


def _matches_queries(title: str | None, keywords: list[str]) -> bool:
    """Return True if the job title matches a whole query keyword (or if none configured).

    Matching is anchored to word boundaries so the keyword "site" no longer
    accepts "Website Manager" and "engineer" no longer accepts "Reengineering".
    """
    if not keywords:
        return True
    if not title:
        return False
    t_low = title.lower()
    for keyword in keywords:
        pattern = _keyword_pattern(keyword)
        if pattern is not None and pattern.search(t_low):
            return True
    return False


def _normalize_exclude_terms(exclude: object) -> list[str]:
    """Normalize an ``exclude_titles`` config value into clean lowercase terms."""
    if isinstance(exclude, str):
        raw = [exclude]
    elif isinstance(exclude, (list, tuple, set, frozenset)):
        raw = [item for item in exclude if isinstance(item, str)]
    else:
        return []
    return [term for term in (item.strip().lower() for item in raw) if term]


def _title_excluded(title: str, exclude: list[str]) -> bool:
    """Return True when an exclusion term occurs in ``title`` as a whole phrase.

    ``config.is_title_allowed`` uses a raw substring test, so a user-authored
    ``exclude_titles: ["intern"]`` silently deleted every "Internal Audit" and
    "International" posting. Anchoring to word boundaries keeps "Marketing
    Intern" excluded without touching "Internal Audit Manager".
    """
    terms = _normalize_exclude_terms(exclude)
    if not terms:
        return False
    lowered = title.lower()
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", lowered) for term in terms
    )


def _title_allowed(title: str, search_cfg: dict[str, Any] | None) -> bool:
    """Word-boundary-aware replacement for ``config.is_title_allowed``."""
    if not search_cfg:
        return True
    exclude = search_cfg.get("exclude_titles")
    if not exclude:
        return True
    return not _title_excluded(title, exclude)


def _clean_location_text(value: str) -> str:
    """Collapse a raw location string and drop empty comma-separated segments."""
    parts = [segment.strip() for segment in value.split(",")]
    return ", ".join(part for part in parts if part)


def _country_display_name(code: str) -> str:
    """Map an ISO-3166 alpha-2 code onto a readable country name."""
    cleaned = code.strip()
    if not cleaned:
        return ""
    return _ISO_ALPHA2_COUNTRIES.get(cleaned.lower(), cleaned)


def _format_ats_location(
    location_data: Any,
    *,
    city: str = "",
    country: str = "",
) -> str:
    """Build a human-readable location from ATS location fields.

    ``fullLocation`` is preferred because SmartRecruiters returns it already
    formatted, but it frequently contains empty segments ("Vecsés, , Hungary").
    The fallback joins city + a mapped country name so a two-letter code never
    reaches the database (which would break location filters and fingerprints).
    """
    obj = location_data if isinstance(location_data, dict) else {}

    full_value = str(obj.get("fullLocation") or obj.get("full_location") or "").strip()
    full = _clean_location_text(full_value)
    if full:
        return full

    city_value = (str(obj.get("city") or "").strip()) or city.strip()
    country_value = (str(obj.get("country") or "").strip()) or country.strip()
    if country_value and not country_value.isalpha():
        country_value = _country_display_name(country_value)
    return ", ".join(part for part in (city_value, country_value) if part)


def _decode_response_body(body: bytes, content_type: str | None = None) -> str:
    """Decode a board body using the charset the server declared.

    EU career portals (Roche-CH, ASML-NL, Siemens-DE) still serve Latin-1 or
    Shift_JIS. Decoding everything as UTF-8 with ``errors="replace"`` silently
    corrupts titles and locations, which in turn corrupts
    ``compute_job_fingerprint`` and therefore deduplication.
    """
    encoding = "utf-8"
    if content_type:
        _, _, raw_charset = content_type.lower().partition("charset=")
        declared = raw_charset.split(";")[0].strip().strip("\"'")
        if declared:
            encoding = declared
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        log.debug("Unsupported charset %r; decoding as utf-8", encoding)
        return body.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Platform-Specific JSON Parsers
# ---------------------------------------------------------------------------

def parse_greenhouse_jobs(
    payload: Any,
    employer_name: str,
    accept_locs: list[str] | None = None,
    reject_locs: list[str] | None = None,
    search_cfg: dict[str, Any] | None = None,
    keywords: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Parse Greenhouse `/v1/boards/{token}/jobs?content=true` JSON response."""
    raw_jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    parsed: list[dict[str, Any]] = []

    for item in raw_jobs:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("absolute_url") or "").strip()
        if not title or not url:
            continue

        loc_obj = item.get("location")
        location = str(loc_obj.get("name") or "") if isinstance(loc_obj, dict) else str(loc_obj or "")

        if not _title_allowed(title, search_cfg):
            continue
        if keywords and not _matches_queries(title, keywords):
            continue
        if not config.is_location_allowed(location, accept=accept_locs, reject=reject_locs):
            continue

        raw_html = html.unescape(str(item.get("content") or ""))
        clean_desc = strip_html(raw_html) if raw_html else ""

        parsed.append(
            {
                "url": url,
                "title": title,
                "company": employer_name,
                "location": location,
                "description": clean_desc[:500] if clean_desc else None,
                "full_description": clean_desc if len(clean_desc) >= _MIN_FULL_DESCRIPTION_CHARS else None,
                "application_url": url,
                "salary": None,
            }
        )
    return parsed


def parse_lever_jobs(
    payload: Any,
    employer_name: str,
    accept_locs: list[str] | None = None,
    reject_locs: list[str] | None = None,
    search_cfg: dict[str, Any] | None = None,
    keywords: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Parse Lever `/v0/postings/{token}?mode=json` JSON response."""
    raw_jobs = payload if isinstance(payload, list) else payload.get("data", []) if isinstance(payload, dict) else []
    parsed: list[dict[str, Any]] = []

    for item in raw_jobs:
        if not isinstance(item, dict):
            continue
        title = str(item.get("text") or item.get("title") or "").strip()
        url = str(item.get("hostedUrl") or item.get("applyUrl") or "").strip()
        apply_url = str(item.get("applyUrl") or url).strip()
        if not title or not url:
            continue

        categories = item.get("categories")
        cats = categories if isinstance(categories, dict) else {}
        location = str(cats.get("location") or item.get("workplaceType") or "").strip()

        if not _title_allowed(title, search_cfg):
            continue
        if keywords and not _matches_queries(title, keywords):
            continue
        if not config.is_location_allowed(location, accept=accept_locs, reject=reject_locs):
            continue

        desc_parts: list[str] = []
        if item.get("descriptionPlain"):
            desc_parts.append(str(item["descriptionPlain"]))
        elif item.get("description"):
            desc_parts.append(strip_html(str(item["description"])))

        for lst in item.get("lists", []) or []:
            if isinstance(lst, dict):
                hdr = str(lst.get("text") or "").strip()
                content = strip_html(str(lst.get("content") or ""))
                if hdr or content:
                    desc_parts.append(f"{hdr}\n{content}".strip())

        if item.get("additionalPlain"):
            desc_parts.append(str(item["additionalPlain"]))

        clean_desc = "\n\n".join(p for p in desc_parts if p).strip()

        salary_str = None
        sal_range = item.get("salaryRange")
        if isinstance(sal_range, dict) and (sal_range.get("min") or sal_range.get("max")):
            cur = sal_range.get("currency", "")
            s_min = sal_range.get("min", "")
            s_max = sal_range.get("max", "")
            salary_str = f"{cur} {s_min}-{s_max}".strip()

        parsed.append(
            {
                "url": url,
                "title": title,
                "company": employer_name,
                "location": location,
                "salary": salary_str,
                "description": clean_desc[:500] if clean_desc else None,
                "full_description": clean_desc if len(clean_desc) >= _MIN_FULL_DESCRIPTION_CHARS else None,
                "application_url": apply_url,
            }
        )
    return parsed


def parse_ashby_jobs(
    payload: Any,
    employer_name: str,
    accept_locs: list[str] | None = None,
    reject_locs: list[str] | None = None,
    search_cfg: dict[str, Any] | None = None,
    keywords: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Parse Ashby `/posting-api/job-board/{token}?includeCompensation=true` JSON response."""
    raw_jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    parsed: list[dict[str, Any]] = []

    for item in raw_jobs:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("jobUrl") or item.get("applyUrl") or "").strip()
        apply_url = str(item.get("applyUrl") or url).strip()
        if not title or not url:
            continue

        location = str(item.get("location") or "").strip()
        if item.get("isRemote") and "remote" not in location.lower():
            location = f"{location} (Remote)".strip()

        if not _title_allowed(title, search_cfg):
            continue
        if keywords and not _matches_queries(title, keywords):
            continue
        if not config.is_location_allowed(location, accept=accept_locs, reject=reject_locs):
            continue

        clean_desc = str(item.get("descriptionPlain") or "").strip()
        if not clean_desc and item.get("descriptionHtml"):
            clean_desc = strip_html(str(item["descriptionHtml"]))

        comp = item.get("compensation")
        salary_str = None
        if isinstance(comp, dict):
            salary_str = comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary")

        parsed.append(
            {
                "url": url,
                "title": title,
                "company": employer_name,
                "location": location,
                "salary": salary_str,
                "description": clean_desc[:500] if clean_desc else None,
                "full_description": clean_desc if len(clean_desc) >= _MIN_FULL_DESCRIPTION_CHARS else None,
                "application_url": apply_url,
            }
        )
    return parsed


def parse_smartrecruiters_jobs(
    payload: Any,
    employer_name: str,
    company_token: str,
    accept_locs: list[str] | None = None,
    reject_locs: list[str] | None = None,
    search_cfg: dict[str, Any] | None = None,
    keywords: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Parse SmartRecruiters `/v1/companies/{token}/postings` JSON response."""
    raw_jobs = payload.get("content", []) if isinstance(payload, dict) else []
    parsed: list[dict[str, Any]] = []

    for item in raw_jobs:
        if not isinstance(item, dict):
            continue
        title = str(item.get("name") or item.get("title") or "").strip()
        posting_id = str(item.get("id") or "").strip()
        if not title or not posting_id:
            continue

        location_data = item.get("location")
        loc_obj = location_data if isinstance(location_data, dict) else {}
        location = _format_ats_location(location_data)
        if loc_obj.get("remote") and "remote" not in location.lower():
            location = f"{location} (Remote)".strip()

        if not _title_allowed(title, search_cfg):
            continue
        if keywords and not _matches_queries(title, keywords):
            continue
        if not config.is_location_allowed(location, accept=accept_locs, reject=reject_locs):
            continue

        url = f"https://jobs.smartrecruiters.com/{company_token}/{posting_id}"
        parsed.append(
            {
                "url": url,
                "title": title,
                "company": employer_name,
                "location": location,
                "salary": None,
                "description": None,
                "full_description": None,
                "application_url": url,
            }
        )
    return parsed


def parse_personio_jobs(
    payload: Any,
    employer_name: str,
    company_token: str,
    accept_locs: list[str] | None = None,
    reject_locs: list[str] | None = None,
    search_cfg: dict[str, Any] | None = None,
    keywords: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Parse Personio `/xml` feed (XML) or `/search.json` response."""
    parsed: list[dict[str, Any]] = []

    if isinstance(payload, str) and "<position" in payload:
        import xml.etree.ElementTree as ET

        try:
            root = ET.fromstring(payload)
            items = []
            for pos in root.findall(".//position"):
                pos_id = pos.findtext("id") or ""
                name = pos.findtext("name") or ""
                office = pos.findtext("office") or ""
                desc_parts = [
                    (jd.findtext("name") or "") + "\n" + strip_html(jd.findtext("value") or "")
                    for jd in pos.findall(".//jobDescription")
                ]
                items.append(
                    {
                        "id": pos_id,
                        "name": name,
                        "office": office,
                        "description": "\n\n".join(p.strip() for p in desc_parts if p.strip()),
                    }
                )
        except Exception:
            items = []
    elif isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("positions") or payload.get("jobs") or payload.get("data") or []
    else:
        items = []

    base_host = f"https://{company_token}.jobs.personio.de" if "." not in company_token else f"https://{company_token}"
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("name") or item.get("title") or "").strip()
        pos_id = str(item.get("id") or "").strip()
        url = str(item.get("url") or (f"{base_host}/job/{pos_id}" if pos_id else "")).strip()
        if not title or not url:
            continue

        location = str(item.get("office") or item.get("location") or "").strip()
        if not _title_allowed(title, search_cfg):
            continue
        if keywords and not _matches_queries(title, keywords):
            continue
        if not config.is_location_allowed(location, accept=accept_locs, reject=reject_locs):
            continue

        raw_desc = str(item.get("description") or item.get("jobDescriptions") or "").strip()
        clean_desc = strip_html(raw_desc) if "<" in raw_desc else raw_desc

        parsed.append(
            {
                "url": url,
                "title": title,
                "company": employer_name,
                "location": location,
                "salary": None,
                "description": clean_desc[:500] if clean_desc else None,
                "full_description": clean_desc if len(clean_desc) >= _MIN_FULL_DESCRIPTION_CHARS else None,
                "application_url": url,
            }
        )
    return parsed


def parse_workable_jobs(
    payload: Any,
    employer_name: str,
    company_token: str,
    accept_locs: list[str] | None = None,
    reject_locs: list[str] | None = None,
    search_cfg: dict[str, Any] | None = None,
    keywords: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Parse Workable public widget API (`/api/v1/widget/accounts/{token}?details=true`) response."""
    raw_jobs = payload.get("jobs", []) if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])
    parsed: list[dict[str, Any]] = []

    for item in raw_jobs:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("name") or "").strip()
        shortcode = str(item.get("shortcode") or item.get("code") or item.get("id") or "").strip()
        url = str(item.get("url") or item.get("shortlink") or (f"https://apply.workable.com/{company_token}/j/{shortcode}/" if shortcode else "")).strip()
        if not title or not url:
            continue

        # Workable may nest city/country inside `location` and omit the
        # top-level keys; str(dict) there would store "{'city': 'London'...}".
        location = _format_ats_location(
            item.get("location"),
            city=str(item.get("city") or ""),
            country=str(item.get("country") or ""),
        )
        if item.get("telecommuting") and "remote" not in location.lower():
            location = f"{location} (Remote)".strip()

        if not _title_allowed(title, search_cfg):
            continue
        if keywords and not _matches_queries(title, keywords):
            continue
        if not config.is_location_allowed(location, accept=accept_locs, reject=reject_locs):
            continue

        raw_desc = str(item.get("description") or "").strip()
        clean_desc = strip_html(raw_desc) if "<" in raw_desc else raw_desc

        parsed.append(
            {
                "url": url,
                "title": title,
                "company": employer_name,
                "location": location,
                "salary": None,
                "description": clean_desc[:500] if clean_desc else None,
                "full_description": clean_desc if len(clean_desc) >= _MIN_FULL_DESCRIPTION_CHARS else None,
                "application_url": str(item.get("application_url") or url).strip(),
            }
        )
    return parsed


def parse_recruitee_jobs(
    payload: Any,
    employer_name: str,
    company_token: str,
    accept_locs: list[str] | None = None,
    reject_locs: list[str] | None = None,
    search_cfg: dict[str, Any] | None = None,
    keywords: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Parse Recruitee public `/api/offers` JSON response."""
    raw_jobs = payload.get("offers", []) if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])
    parsed: list[dict[str, Any]] = []

    for item in raw_jobs:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        slug = str(item.get("slug") or item.get("id") or "").strip()
        url = str(item.get("careers_url") or (f"https://{company_token}.recruitee.com/o/{slug}" if slug else "")).strip()
        if not title or not url:
            continue

        location = str(item.get("location") or item.get("city") or "").strip()
        if item.get("remote") and "remote" not in location.lower():
            location = f"{location} (Remote)".strip()

        if not _title_allowed(title, search_cfg):
            continue
        if keywords and not _matches_queries(title, keywords):
            continue
        if not config.is_location_allowed(location, accept=accept_locs, reject=reject_locs):
            continue

        raw_desc = "\n\n".join(
            p for p in (str(item.get("description") or ""), str(item.get("requirements") or "")) if p
        ).strip()
        clean_desc = strip_html(raw_desc) if "<" in raw_desc else raw_desc

        parsed.append(
            {
                "url": url,
                "title": title,
                "company": employer_name,
                "location": location,
                "salary": None,
                "description": clean_desc[:500] if clean_desc else None,
                "full_description": clean_desc if len(clean_desc) >= _MIN_FULL_DESCRIPTION_CHARS else None,
                "application_url": str(item.get("careers_apply_url") or url).strip(),
            }
        )
    return parsed


# ---------------------------------------------------------------------------
# Async pooled fetcher & runner
# ---------------------------------------------------------------------------

DEFAULT_PLATFORMS = (
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
    "personio",
    "workable",
    "recruitee",
)
ATSJob = dict[str, Any]
BoardTask = tuple[str, dict[str, Any]]
BoardResult = tuple[str, str, list[ATSJob]]
HttpGetter = Callable[[str], httpx.Response]


def _board_api_url(platform_name: str, employer: dict[str, Any], offset: int = 0) -> str | None:
    """Build the public board API URL for one employer token.

    ``offset`` only affects SmartRecruiters, the single paginated API; the other
    boards return their whole board in one response.
    """
    token = str(employer.get("token") or "").strip()
    if not token:
        return None
    if platform_name == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    if platform_name == "lever":
        return f"https://api.lever.co/v0/postings/{token}?mode=json"
    if platform_name == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
    if platform_name == "smartrecruiters":
        return (
            f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
            f"?limit={_SMARTRECRUITERS_PAGE_LIMIT}&offset={max(0, offset)}"
        )
    if platform_name == "personio":
        host = f"{token}.jobs.personio.de" if "." not in token else token
        # Personio's public board feed is /xml (a <workzag-jobs> document);
        # /search.json 404s on every hosted board.
        return f"https://{host}/xml"
    if platform_name == "workable":
        return f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"
    if platform_name == "recruitee":
        return f"https://{token}.recruitee.com/api/offers"
    return None


def _board_max_pages(platform_name: str) -> int:
    """Return how many pages should be walked for a board."""
    return _SMARTRECRUITERS_MAX_PAGES if platform_name == "smartrecruiters" else 1


def _smartrecruiters_total_found(body_text: str) -> int | None:
    """Read SmartRecruiters' ``totalFound`` so pagination knows when to stop."""
    try:
        payload = json.loads(body_text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return int(payload.get("totalFound"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_ats_board_payload(
    platform_name: str,
    employer: dict[str, Any],
    status_code: int,
    body_text: str,
    accept_locs: list[str],
    reject_locs: list[str],
    search_cfg: dict[str, Any],
    keywords: list[str],
) -> BoardResult:
    """Parse a fetched board payload from its status code and decoded body."""
    name = str(employer.get("name") or employer.get("token") or "Unknown")
    token = str(employer.get("token") or "")
    if status_code != 200:
        log.debug("%s/%s returned HTTP %d", platform_name, name, status_code)
        return platform_name, name, []
    if platform_name == "personio" and body_text.lstrip().startswith("<"):
        return (
            platform_name,
            name,
            parse_personio_jobs(body_text, name, token, accept_locs, reject_locs, search_cfg, keywords),
        )

    payload: Any = json.loads(body_text)
    parser_args = (payload, name, accept_locs, reject_locs, search_cfg, keywords)
    if platform_name == "greenhouse":
        jobs = parse_greenhouse_jobs(*parser_args)
    elif platform_name == "lever":
        jobs = parse_lever_jobs(*parser_args)
    elif platform_name == "ashby":
        jobs = parse_ashby_jobs(*parser_args)
    elif platform_name == "smartrecruiters":
        jobs = parse_smartrecruiters_jobs(payload, name, token, accept_locs, reject_locs, search_cfg, keywords)
    elif platform_name == "personio":
        jobs = parse_personio_jobs(payload, name, token, accept_locs, reject_locs, search_cfg, keywords)
    elif platform_name == "workable":
        jobs = parse_workable_jobs(payload, name, token, accept_locs, reject_locs, search_cfg, keywords)
    elif platform_name == "recruitee":
        jobs = parse_recruitee_jobs(payload, name, token, accept_locs, reject_locs, search_cfg, keywords)
    else:
        jobs = []
    return platform_name, name, jobs


async def _read_bounded_board_body(client: httpx.AsyncClient, url: str) -> tuple[bytes, str | None]:
    """Stream one ATS board body under the byte cap and report its content type.

    ``network.request_bounded_bytes`` returns only the decoded bytes, but the
    charset still has to come from the response headers to avoid mojibake, so
    the bounded read is done here with the same policy and guarantees.
    """
    for attempt in range(_ATS_RETRY_POLICY.max_attempts):
        retry_wait: float | None = None
        try:
            async with client.stream("GET", url) as response:
                if (
                    response.status_code in _ATS_RETRY_POLICY.retry_status_codes
                    and attempt + 1 < _ATS_RETRY_POLICY.max_attempts
                ):
                    retry_wait = _ATS_RETRY_POLICY.delay(attempt, response.headers)
                    log.warning(
                        "Transient HTTP %d (ATS board), retry %d/%d in %.2fs: %s",
                        response.status_code,
                        attempt + 1,
                        _ATS_RETRY_POLICY.max_attempts - 1,
                        retry_wait,
                        url,
                    )
                else:
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    received = 0
                    async for chunk in response.aiter_bytes():
                        received += len(chunk)
                        if received > _MAX_BOARD_RESPONSE_BYTES:
                            raise ResponseBodyTooLarge(
                                f"ATS board {url} exceeded the {_MAX_BOARD_RESPONSE_BYTES}-byte cap "
                                f"after receiving {received} decoded bytes"
                            )
                        chunks.append(chunk)
                    return b"".join(chunks), response.headers.get("content-type")
        except _ATS_RETRY_EXCEPTIONS as exc:
            if attempt + 1 >= _ATS_RETRY_POLICY.max_attempts:
                raise
            retry_wait = _ATS_RETRY_POLICY.delay(attempt)
            log.warning(
                "Transient ATS board transport error (%s), retry %d/%d in %.2fs: %s",
                type(exc).__name__,
                attempt + 1,
                _ATS_RETRY_POLICY.max_attempts - 1,
                retry_wait,
                url,
            )
        if retry_wait is not None:
            await asyncio.sleep(retry_wait)

    raise RuntimeError("ATS board retry loop exited unexpectedly")


async def _fetch_board_page(client: httpx.AsyncClient, url: str) -> tuple[int, str]:
    """Fetch and decode one board page."""
    body, content_type = await _read_bounded_board_body(client, url)
    return 200, _decode_response_body(body, content_type)


async def _scrape_single_ats_board_async(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    platform_name: str,
    employer: dict[str, Any],
    accept_locs: list[str],
    reject_locs: list[str],
    search_cfg: dict[str, Any],
    keywords: list[str],
    http_get: HttpGetter | None = None,
) -> BoardResult:
    """Fetch one ATS board with shared pooling; test getters run off-loop.

    SmartRecruiters is paginated with an offset, so every page up to
    ``totalFound`` (bounded by ``_SMARTRECRUITERS_MAX_PAGES``) is walked and the
    jobs from all pages are returned together. All other boards are unpaginated.
    """
    name = str(employer.get("name") or employer.get("token") or "Unknown")
    api_url = _board_api_url(platform_name, employer)
    if api_url is None:
        return platform_name, name, []
    try:
        async with semaphore:
            if http_get is not None:
                response = await asyncio.to_thread(http_get, api_url)
                return _parse_ats_board_payload(
                    platform_name, employer, response.status_code, response.text,
                    accept_locs, reject_locs, search_cfg, keywords,
                )

            jobs: list[ATSJob] = []
            offset = 0
            for page_index in range(_board_max_pages(platform_name)):
                page_url = api_url if offset == 0 else _board_api_url(platform_name, employer, offset=offset)
                if page_url is None:
                    break
                status_code, body_text = await _fetch_board_page(client, page_url)
                _, _, page_jobs = _parse_ats_board_payload(
                    platform_name, employer, status_code, body_text,
                    accept_locs, reject_locs, search_cfg, keywords,
                )
                jobs.extend(page_jobs)
                if platform_name != "smartrecruiters":
                    break
                total_found = _smartrecruiters_total_found(body_text)
                offset += _SMARTRECRUITERS_PAGE_LIMIT
                if not page_jobs or total_found is None or offset >= total_found:
                    break
                if page_index + 1 >= _SMARTRECRUITERS_MAX_PAGES:
                    log.info(
                        "SmartRecruiters %s: capped at %d pages -- only %d of %d postings discovered",
                        name, _SMARTRECRUITERS_MAX_PAGES, offset, total_found,
                    )
            return platform_name, name, jobs
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        log.debug("Direct ATS fetch failed for %s (%s): %s", name, platform_name, exc)
        return platform_name, name, []
    except Exception:
        log.exception("Unexpected ATS parser failure for %s (%s)", name, platform_name)
        return platform_name, name, []


def _load_ats_discovery_config() -> tuple[dict[str, Any], list[str], list[str], list[str], dict[str, Any]]:
    # config.load_search_config() already falls back to the bundled
    # searches.example.yaml when the user has no searches.yaml. Pre-empting that
    # with a SEARCH_CONFIG_PATH.exists() guard substituted `{}` and silently
    # disabled every ATS filter on a fresh install, unlike workday.py and
    # smartextract.py which load the config un-guarded.
    search_cfg = config.load_search_config()
    accept_locs, reject_locs = config.load_location_filters(search_cfg)
    keywords = _extract_query_keywords(search_cfg) if search_cfg else []
    return search_cfg, accept_locs, reject_locs, keywords, config.load_ats_employers()


def _persist_ats_results(results: list[BoardResult], platforms: list[str]) -> dict[str, Any]:
    conn = init_db()
    total_new = 0
    total_duplicates = 0
    by_platform = {platform: 0 for platform in platforms}
    try:
        for platform, employer_name, jobs in results:
            if not jobs:
                continue
            try:
                new_count, duplicate_count = store_jobs(
                    conn, jobs, site=employer_name, strategy=f"{platform}_api"
                )
            except Exception:
                log.exception("Failed to store ATS results for %s (%s)", employer_name, platform)
                continue
            total_new += new_count
            total_duplicates += duplicate_count
            by_platform[platform] = by_platform.get(platform, 0) + new_count
    finally:
        close_connection()
    return {
        "boards_checked": len(results),
        "new_jobs": total_new,
        "duplicate_jobs": total_duplicates,
        "by_platform": by_platform,
    }


async def run_ats_direct_discovery_async(
    workers: int = 4,
    platforms: list[str] | None = None,
    http_get: HttpGetter | None = None,
) -> dict[str, Any]:
    """Discover jobs from public ATS APIs using one pooled AsyncClient.

    Configuration reads and SQLite writes are delegated to worker threads, so
    the event loop remains available while a batch fetches employer boards.
    `http_get` remains available for backwards-compatible synchronous tests.
    """
    active_platforms = list(platforms or DEFAULT_PLATFORMS)
    search_cfg, accept_locs, reject_locs, keywords, registry = await asyncio.to_thread(
        _load_ats_discovery_config
    )
    tasks: list[BoardTask] = [
        (platform, employer)
        for platform in active_platforms
        for employer in (registry.get(platform, []) or [])
        if isinstance(employer, dict) and employer.get("token")
    ]
    if not tasks:
        return {"boards_checked": 0, "new_jobs": 0, "duplicate_jobs": 0, "by_platform": {}}

    concurrency = max(1, min(workers, len(tasks), _MAX_BOARD_CONCURRENCY))
    semaphore = asyncio.Semaphore(concurrency)
    summary: dict[str, Any] = {
        "boards_checked": 0,
        "new_jobs": 0,
        "duplicate_jobs": 0,
        "by_platform": {platform: 0 for platform in active_platforms},
    }
    pending_results: list[BoardResult] = []
    persist_lock = asyncio.Lock()

    async def _flush_pending_results() -> None:
        if not pending_results:
            return
        batch = pending_results.copy()
        pending_results.clear()
        partial = await asyncio.to_thread(_persist_ats_results, batch, active_platforms)
        summary["boards_checked"] += partial["boards_checked"]
        summary["new_jobs"] += partial["new_jobs"]
        summary["duplicate_jobs"] += partial["duplicate_jobs"]
        for platform, count in partial["by_platform"].items():
            summary["by_platform"][platform] = summary["by_platform"].get(platform, 0) + count

    async def _persist_completed_result(result: BoardResult) -> None:
        async with persist_lock:
            pending_results.append(result)
            if len(pending_results) >= _ATS_PERSIST_BATCH_SIZE:
                await _flush_pending_results()

    async with build_async_client(
        timeout=httpx.Timeout(_TIMEOUT, connect=5.0),
        max_connections=concurrency,
        max_keepalive_connections=concurrency,
        headers={"User-Agent": _UA},
    ) as client:
        next_index = 0

        async def board_worker() -> None:
            nonlocal next_index
            while next_index < len(tasks):
                index = next_index
                next_index += 1
                platform, employer = tasks[index]
                result = await _scrape_single_ats_board_async(
                    client,
                    semaphore,
                    platform,
                    employer,
                    accept_locs,
                    reject_locs,
                    search_cfg,
                    keywords,
                    http_get,
                )
                await _persist_completed_result(result)

        await asyncio.gather(*(board_worker() for _ in range(concurrency)))
        async with persist_lock:
            await _flush_pending_results()
    log.info(
        "Direct ATS discovery complete: %d boards checked, %d new jobs (%d duplicates)",
        summary["boards_checked"],
        summary["new_jobs"],
        summary["duplicate_jobs"],
    )
    return summary


def run_ats_direct_discovery(
    workers: int = 4,
    platforms: list[str] | None = None,
    http_get: HttpGetter | None = None,
) -> dict[str, Any]:
    """Backward-compatible synchronous CLI wrapper for async ATS discovery."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            run_ats_direct_discovery_async(workers=workers, platforms=platforms, http_get=http_get)
        )
    raise RuntimeError(
        "run_ats_direct_discovery() cannot run inside an event loop; "
        "await run_ats_direct_discovery_async() instead"
    )
