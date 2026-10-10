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
from applypilot.network import RetryPolicy, build_async_client, request_bounded_bytes

log = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
_TIMEOUT = 20.0
_MAX_BOARD_CONCURRENCY = 64
_ATS_PERSIST_BATCH_SIZE = 8
# Hard cap on a single ATS board response body, enforced on decoded bytes as
# they stream in. Boards routinely carry thousands of postings with full
# descriptions; 8 MiB is generous headroom while still bounding memory.
_MAX_BOARD_RESPONSE_BYTES = 8_000_000


def _extract_query_keywords(search_cfg: dict[str, Any] | None = None) -> list[str]:
    """Extract lowercase query keywords/phrases from searches.yaml for client-side filtering."""
    if search_cfg is None:
        if not config.SEARCH_CONFIG_PATH.exists():
            return []
        search_cfg = config.load_search_config()

    queries = search_cfg.get("queries", [])
    keywords: list[str] = []
    for q in queries:
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


def _matches_queries(title: str | None, keywords: list[str]) -> bool:
    """Return True if the job title matches at least one query keyword (or if no keywords configured)."""
    if not keywords:
        return True
    if not title:
        return False
    t_low = title.lower()
    return any(kw in t_low for kw in keywords)


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

        if search_cfg and not config.is_title_allowed(title, search_cfg):
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
                "full_description": clean_desc if len(clean_desc) >= 40 else None,
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

        if search_cfg and not config.is_title_allowed(title, search_cfg):
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
                "full_description": clean_desc if len(clean_desc) >= 120 else None,
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

        if search_cfg and not config.is_title_allowed(title, search_cfg):
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
                "full_description": clean_desc if len(clean_desc) >= 40 else None,
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
        loc_parts = [
            str(loc_obj.get("city") or "").strip(),
            str(loc_obj.get("country") or "").strip(),
        ]
        location = ", ".join(p for p in loc_parts if p)
        if loc_obj.get("remote") and "remote" not in location.lower():
            location = f"{location} (Remote)".strip()

        if search_cfg and not config.is_title_allowed(title, search_cfg):
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
    """Parse Personio `/search.json` JSON or `/xml` feed response."""
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
        if search_cfg and not config.is_title_allowed(title, search_cfg):
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
                "full_description": clean_desc if len(clean_desc) >= 40 else None,
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

        loc_parts = [
            str(item.get("city") or "").strip(),
            str(item.get("country") or "").strip(),
        ]
        location = ", ".join(p for p in loc_parts if p) or str(item.get("location") or "").strip()
        if item.get("telecommuting") and "remote" not in location.lower():
            location = f"{location} (Remote)".strip()

        if search_cfg and not config.is_title_allowed(title, search_cfg):
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
                "full_description": clean_desc if len(clean_desc) >= 40 else None,
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

        if search_cfg and not config.is_title_allowed(title, search_cfg):
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
                "full_description": clean_desc if len(clean_desc) >= 40 else None,
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


def _board_api_url(platform_name: str, employer: dict[str, Any]) -> str | None:
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
        return f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100"
    if platform_name == "personio":
        host = f"{token}.jobs.personio.de" if "." not in token else token
        return f"https://{host}/search.json"
    if platform_name == "workable":
        return f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"
    if platform_name == "recruitee":
        return f"https://{token}.recruitee.com/api/offers"
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
    """Fetch one ATS board with shared pooling; test getters run off-loop."""
    name = str(employer.get("name") or employer.get("token") or "Unknown")
    api_url = _board_api_url(platform_name, employer)
    if api_url is None:
        return platform_name, name, []
    try:
        async with semaphore:
            if http_get is not None:
                response = await asyncio.to_thread(http_get, api_url)
                status_code = response.status_code
                body_text = response.text
            else:
                body = await request_bounded_bytes(
                    client,
                    "GET",
                    api_url,
                    max_bytes=_MAX_BOARD_RESPONSE_BYTES,
                    policy=RetryPolicy(max_attempts=4, base_delay=0.5, max_delay=15.0),
                    logger=log,
                )
                status_code = 200
                body_text = body.decode("utf-8", errors="replace")
        return _parse_ats_board_payload(
            platform_name, employer, status_code, body_text, accept_locs, reject_locs, search_cfg, keywords
        )
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        log.debug("Direct ATS fetch failed for %s (%s): %s", name, platform_name, exc)
        return platform_name, name, []
    except Exception:
        log.exception("Unexpected ATS parser failure for %s (%s)", name, platform_name)
        return platform_name, name, []


def _load_ats_discovery_config() -> tuple[dict[str, Any], list[str], list[str], list[str], dict[str, Any]]:
    search_cfg = config.load_search_config() if config.SEARCH_CONFIG_PATH.exists() else {}
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
