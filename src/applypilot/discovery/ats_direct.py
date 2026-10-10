"""Zero-Browser Direct ATS API Scrapers (Greenhouse, Lever, Ashby, SmartRecruiters).

Scrapes public JSON APIs exposed by employer career portals across the UK, EU,
and global markets. Zero browser overhead and zero LLM tokens — returns full
job descriptions and direct application URLs in a single HTTP request per board,
meaning discovered jobs are immediately pre-enriched for scoring.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import html
import logging
import re
from typing import Callable

import httpx

from applypilot import config
from applypilot.database import close_connection, init_db, store_jobs
from applypilot.discovery.workday import strip_html

log = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
_TIMEOUT = 20.0


def _extract_query_keywords(search_cfg: dict | None = None) -> list[str]:
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
    payload: dict,
    employer_name: str,
    accept_locs: list[str] | None = None,
    reject_locs: list[str] | None = None,
    search_cfg: dict | None = None,
    keywords: list[str] | None = None,
) -> list[dict]:
    """Parse Greenhouse `/v1/boards/{token}/jobs?content=true` JSON response."""
    raw_jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    parsed: list[dict] = []

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
    payload: list | dict,
    employer_name: str,
    accept_locs: list[str] | None = None,
    reject_locs: list[str] | None = None,
    search_cfg: dict | None = None,
    keywords: list[str] | None = None,
) -> list[dict]:
    """Parse Lever `/v0/postings/{token}?mode=json` JSON response."""
    raw_jobs = payload if isinstance(payload, list) else payload.get("data", []) if isinstance(payload, dict) else []
    parsed: list[dict] = []

    for item in raw_jobs:
        if not isinstance(item, dict):
            continue
        title = str(item.get("text") or item.get("title") or "").strip()
        url = str(item.get("hostedUrl") or item.get("applyUrl") or "").strip()
        apply_url = str(item.get("applyUrl") or url).strip()
        if not title or not url:
            continue

        cats = item.get("categories") if isinstance(item.get("categories"), dict) else {}
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
    payload: dict,
    employer_name: str,
    accept_locs: list[str] | None = None,
    reject_locs: list[str] | None = None,
    search_cfg: dict | None = None,
    keywords: list[str] | None = None,
) -> list[dict]:
    """Parse Ashby `/posting-api/job-board/{token}?includeCompensation=true` JSON response."""
    raw_jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    parsed: list[dict] = []

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
    payload: dict,
    employer_name: str,
    company_token: str,
    accept_locs: list[str] | None = None,
    reject_locs: list[str] | None = None,
    search_cfg: dict | None = None,
    keywords: list[str] | None = None,
) -> list[dict]:
    """Parse SmartRecruiters `/v1/companies/{token}/postings` JSON response."""
    raw_jobs = payload.get("content", []) if isinstance(payload, dict) else []
    parsed: list[dict] = []

    for item in raw_jobs:
        if not isinstance(item, dict):
            continue
        title = str(item.get("name") or item.get("title") or "").strip()
        posting_id = str(item.get("id") or "").strip()
        if not title or not posting_id:
            continue

        loc_obj = item.get("location") if isinstance(item.get("location"), dict) else {}
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
    payload: list | dict | str,
    employer_name: str,
    company_token: str,
    accept_locs: list[str] | None = None,
    reject_locs: list[str] | None = None,
    search_cfg: dict | None = None,
    keywords: list[str] | None = None,
) -> list[dict]:
    """Parse Personio `/search.json` JSON or `/xml` feed response."""
    parsed: list[dict] = []

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
    payload: dict | list,
    employer_name: str,
    company_token: str,
    accept_locs: list[str] | None = None,
    reject_locs: list[str] | None = None,
    search_cfg: dict | None = None,
    keywords: list[str] | None = None,
) -> list[dict]:
    """Parse Workable public widget API (`/api/v1/widget/accounts/{token}?details=true`) response."""
    raw_jobs = payload.get("jobs", []) if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])
    parsed: list[dict] = []

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
    payload: dict | list,
    employer_name: str,
    company_token: str,
    accept_locs: list[str] | None = None,
    reject_locs: list[str] | None = None,
    search_cfg: dict | None = None,
    keywords: list[str] | None = None,
) -> list[dict]:
    """Parse Recruitee public `/api/offers` JSON response."""
    raw_jobs = payload.get("offers", []) if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])
    parsed: list[dict] = []

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
# Fetcher & Runner
# ---------------------------------------------------------------------------

def _scrape_single_ats_board(
    platform_name: str,
    employer: dict,
    accept_locs: list[str],
    reject_locs: list[str],
    search_cfg: dict,
    keywords: list[str],
    http_get: Callable[[str], httpx.Response] | None = None,
) -> tuple[str, str, list[dict]]:
    """Fetch and parse a single employer board for Greenhouse, Lever, Ashby, SmartRecruiters, or Personio."""
    name = employer.get("name") or employer.get("token") or "Unknown"
    token = employer.get("token", "")
    if not token:
        return (platform_name, name, [])

    if platform_name == "greenhouse":
        api_url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    elif platform_name == "lever":
        api_url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    elif platform_name == "ashby":
        api_url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
    elif platform_name == "smartrecruiters":
        api_url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100"
    elif platform_name == "personio":
        host = f"{token}.jobs.personio.de" if "." not in token else token
        api_url = f"https://{host}/search.json"
    elif platform_name == "workable":
        api_url = f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"
    elif platform_name == "recruitee":
        api_url = f"https://{token}.recruitee.com/api/offers"
    else:
        return (platform_name, name, [])

    try:
        if http_get is not None:
            resp = http_get(api_url)
        else:
            with httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": _UA}, follow_redirects=True) as client:
                resp = client.get(api_url)

        if resp.status_code != 200:
            log.debug("%s/%s returned HTTP %d", platform_name, name, resp.status_code)
            return (platform_name, name, [])

        if platform_name == "personio" and resp.text.lstrip().startswith("<"):
            jobs = parse_personio_jobs(resp.text, name, token, accept_locs, reject_locs, search_cfg, keywords)
            return (platform_name, name, jobs)

        data = resp.json()
        if platform_name == "greenhouse":
            jobs = parse_greenhouse_jobs(data, name, accept_locs, reject_locs, search_cfg, keywords)
        elif platform_name == "lever":
            jobs = parse_lever_jobs(data, name, accept_locs, reject_locs, search_cfg, keywords)
        elif platform_name == "ashby":
            jobs = parse_ashby_jobs(data, name, accept_locs, reject_locs, search_cfg, keywords)
        elif platform_name == "personio":
            jobs = parse_personio_jobs(data, name, token, accept_locs, reject_locs, search_cfg, keywords)
        elif platform_name == "workable":
            jobs = parse_workable_jobs(data, name, token, accept_locs, reject_locs, search_cfg, keywords)
        elif platform_name == "recruitee":
            jobs = parse_recruitee_jobs(data, name, token, accept_locs, reject_locs, search_cfg, keywords)
        else:
            jobs = parse_smartrecruiters_jobs(data, name, token, accept_locs, reject_locs, search_cfg, keywords)

        return (platform_name, name, jobs)
    except Exception as exc:
        log.debug("Direct ATS fetch failed for %s (%s): %s", name, platform_name, exc)
        return (platform_name, name, [])


def run_ats_direct_discovery(
    workers: int = 4,
    platforms: list[str] | None = None,
    http_get: Callable[[str], httpx.Response] | None = None,
) -> dict:
    """Run zero-browser direct ATS discovery across Greenhouse, Lever, Ashby, and SmartRecruiters.

    Args:
        workers: Parallel worker threads.
        platforms: Optional subset of ['greenhouse', 'lever', 'ashby', 'smartrecruiters'].
        http_get: Optional custom HTTP getter (used for unit testing without network).

    Returns:
        Summary dict with boards_checked, new_jobs, duplicate_jobs, by_platform.
    """
    conn = init_db()
    search_cfg = config.load_search_config() if config.SEARCH_CONFIG_PATH.exists() else {}
    accept_locs, reject_locs = config.load_location_filters(search_cfg)
    keywords = _extract_query_keywords(search_cfg) if search_cfg else []
    ats_registry = config.load_ats_employers()

    active_platforms = platforms or [
        "greenhouse",
        "lever",
        "ashby",
        "smartrecruiters",
        "personio",
        "workable",
        "recruitee",
    ]
    tasks: list[tuple[str, dict]] = []
    for plat in active_platforms:
        for emp in ats_registry.get(plat, []) or []:
            if isinstance(emp, dict) and emp.get("token"):
                tasks.append((plat, emp))

    if not tasks:
        return {"boards_checked": 0, "new_jobs": 0, "duplicate_jobs": 0, "by_platform": {}}

    total_new = 0
    total_dup = 0
    by_platform: dict[str, int] = {p: 0 for p in active_platforms}

    try:
        max_w = max(1, min(workers, len(tasks)))
        with ThreadPoolExecutor(max_workers=max_w) as pool:
            futures = [
                pool.submit(
                    _scrape_single_ats_board,
                    plat,
                    emp,
                    accept_locs,
                    reject_locs,
                    search_cfg,
                    keywords,
                    http_get,
                )
                for plat, emp in tasks
            ]
            for fut in as_completed(futures):
                try:
                    plat, emp_name, jobs = fut.result()
                    if jobs:
                        new_c, dup_c = store_jobs(conn, jobs, site=emp_name, strategy=f"{plat}_api")
                        total_new += new_c
                        total_dup += dup_c
                        by_platform[plat] = by_platform.get(plat, 0) + new_c
                except Exception as exc:
                    log.warning("ATS board worker error: %s", exc)
    finally:
        close_connection()

    log.info(
        "Direct ATS discovery complete: %d boards checked, %d new jobs (%d duplicates)",
        len(tasks),
        total_new,
        total_dup,
    )
    return {
        "boards_checked": len(tasks),
        "new_jobs": total_new,
        "duplicate_jobs": total_dup,
        "by_platform": by_platform,
    }
