"""Workday ATS direct API scraper: searches employer career portals.

Scrapes Workday-powered career sites (TD, RBC, NVIDIA, Salesforce, etc.)
via the undocumented CXS JSON API. Zero LLM, zero browser -- pure HTTP.

Employer registry is loaded from config/employers.yaml instead of being
hardcoded. Supports sequential search + detail fetching with proxy.
"""

import asyncio
import logging
import re
import sqlite3
import time
from collections.abc import AsyncIterator, Coroutine
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, TypeVar
from urllib.parse import quote

import httpx

import yaml

from applypilot import config
from applypilot.config import CONFIG_DIR
from applypilot.database import close_connection, get_connection, init_db
from applypilot.network import RetryPolicy, build_async_client, request_bounded_json

log = logging.getLogger(__name__)


# -- Employer registry from YAML --------------------------------------------

def load_employers() -> dict[str, Any]:
    """Load Workday employer registry from config/employers.yaml."""
    path = CONFIG_DIR / "employers.yaml"
    if not path.exists():
        log.warning("employers.yaml not found at %s", path)
        return {}
    data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        log.warning("Invalid employers.yaml: expected a top-level mapping")
        return {}

    raw_employers = data.get("employers", {})
    if not isinstance(raw_employers, dict):
        log.warning("Invalid employers.yaml: 'employers' must be a mapping")
        return {}

    employers: dict[str, Any] = {}
    for employer_key, employer in raw_employers.items():
        if not isinstance(employer_key, str) or not isinstance(employer, dict):
            log.warning("Skipping invalid Workday employer entry %r", employer_key)
            continue
        employers[employer_key] = employer
    return employers


# -- Location filtering from search config -----------------------------------

def _load_location_filter(search_cfg: dict[str, Any] | None = None) -> tuple[list[str], list[str]]:
    """Load location accept/reject lists from search config."""
    return config.load_location_filters(search_cfg)


def _location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    """Check if a job location passes the user's location filter."""
    return config.is_location_allowed(location, accept=accept, reject=reject)


# -- HTML stripper -----------------------------------------------------------

class _HTMLStripper(HTMLParser):
    """Strip HTML tags, keep text content."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip = True
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = False
        elif tag in ("p", "div", "li", "tr"):
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def get_text(self) -> str:
        text = "".join(self._parts)
        text = re.sub(r"[^\S\n]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def strip_html(html: str) -> str:
    """Convert HTML to plain text."""
    if not html:
        return ""
    stripper = _HTMLStripper()
    stripper.feed(html)
    return stripper.get_text()


# -- Proxy -------------------------------------------------------------------

_proxy_url: str | None = None


def setup_proxy(proxy_str: str | None) -> None:
    """Configure the proxy URL used by the synchronous and async HTTP clients."""
    global _proxy_url
    _proxy_url = None
    if not proxy_str:
        return

    parts = proxy_str.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        _proxy_url = f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    elif len(parts) == 2:
        host, port = parts
        _proxy_url = f"http://{host}:{port}"
    else:
        log.warning("Proxy format not recognized (expected host:port:user:pass or host:port)")
        return
    log.info("Proxy configured for Workday HTTP clients: %s:%s", parts[0], parts[1])


# -- Workday API -------------------------------------------------------------

_WORKDAY_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_MAX_WORKDAY_CONCURRENCY = 32
_DETAIL_WORKERS_PER_EMPLOYER = 4
# Hard cap on a single Workday CXS response body, enforced on decoded bytes as
# they stream in. A board page is ~20 postings, so 2 MiB is generous headroom
# while preventing a hostile or misbehaving endpoint from exhausting memory.
_MAX_WORKDAY_RESPONSE_BYTES = 2_000_000


def _json_object_payload(payload: Any) -> dict[str, Any]:
    """Reject non-object JSON payloads from Workday endpoints."""
    if not isinstance(payload, dict):
        raise ValueError("Workday API response must be a JSON object")
    return payload


def workday_search(employer: dict[str, Any], search_text: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """Synchronous compatibility helper for a Workday CXS job search."""
    url = f"{employer['base_url']}/wday/cxs/{employer['tenant']}/{employer['site_id']}/jobs"
    payload = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": search_text}
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=5.0), proxy=_proxy_url, follow_redirects=True) as client:
        response = client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": _WORKDAY_UA},
        )
        response.raise_for_status()
        return _json_object_payload(response.json())


def workday_detail(employer: dict[str, Any], external_path: str) -> dict[str, Any]:
    """Synchronous compatibility helper for a Workday CXS detail request."""
    url = f"{employer['base_url']}/wday/cxs/{employer['tenant']}/{employer['site_id']}{external_path}"
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=5.0), proxy=_proxy_url, follow_redirects=True) as client:
        response = client.get(url, headers={"Accept": "application/json", "User-Agent": _WORKDAY_UA})
        response.raise_for_status()
        return _json_object_payload(response.json())


async def workday_search_async(
    client: httpx.AsyncClient,
    employer: dict[str, Any],
    search_text: str,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Search Workday's CXS API without blocking the running event loop."""
    url = f"{employer['base_url']}/wday/cxs/{employer['tenant']}/{employer['site_id']}/jobs"
    payload = await request_bounded_json(
        client,
        "POST",
        url,
        json={"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": search_text},
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": _WORKDAY_UA},
        max_bytes=_MAX_WORKDAY_RESPONSE_BYTES,
        policy=RetryPolicy(max_attempts=4, base_delay=0.5, max_delay=15.0),
        logger=log,
    )
    return _json_object_payload(payload)


async def workday_detail_async(
    client: httpx.AsyncClient,
    employer: dict[str, Any],
    external_path: str,
) -> dict[str, Any]:
    """Fetch a Workday job detail using the shared async connection pool."""
    url = f"{employer['base_url']}/wday/cxs/{employer['tenant']}/{employer['site_id']}{external_path}"
    payload = await request_bounded_json(
        client,
        "GET",
        url,
        headers={"Accept": "application/json", "User-Agent": _WORKDAY_UA},
        max_bytes=_MAX_WORKDAY_RESPONSE_BYTES,
        policy=RetryPolicy(max_attempts=4, base_delay=0.5, max_delay=15.0),
        logger=log,
    )
    return _json_object_payload(payload)


# -- Search + paginate -------------------------------------------------------

def search_employer(
    employer_key: str,
    employer: dict[str, Any],
    search_text: str,
    location_filter: bool = True,
    max_results: int = 0,
    accept_locs: list[str] | None = None,
    reject_locs: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Search an employer, paginate through all results, optionally filter by location."""
    log.info("%s: searching \"%s\"...", employer["name"], search_text)

    all_jobs: list[dict[str, Any]] = []
    offset = 0
    page_size = 20
    max_pages = 25  # Cap at 500 results
    total = None

    while True:
        try:
            data = workday_search(employer, search_text, limit=page_size, offset=offset)
        except Exception as e:
            log.error("%s: API error at offset %d: %s", employer["name"], offset, e)
            break

        if total is None:
            total = data.get("total", 0)
            log.info("%s: %d total results", employer["name"], total)

        postings = data.get("jobPostings", [])
        if not postings:
            break

        for j in postings:
            loc = j.get("locationsText", "")
            if location_filter and accept_locs is not None and reject_locs is not None:
                if not _location_ok(loc, accept_locs, reject_locs):
                    continue

            all_jobs.append({
                "title": j.get("title", ""),
                "location": loc,
                "posted": j.get("postedOn", ""),
                "external_path": j.get("externalPath", ""),
                "employer_key": employer_key,
                "employer_name": employer["name"],
            })

        offset += page_size
        page_num = offset // page_size
        if offset >= total:
            break
        if page_num >= max_pages:
            log.info("%s: capped at %d pages (%d results scanned)", employer["name"], max_pages, offset)
            break
        if max_results and len(all_jobs) >= max_results:
            all_jobs = all_jobs[:max_results]
            break

    log.info("%s: %d jobs found%s", employer["name"], len(all_jobs),
             " (filtered)" if location_filter else "")
    return all_jobs


# -- Fetch details -----------------------------------------------------------

def _fetch_one_detail(employer: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """Fetch detail for a single job."""
    try:
        detail = workday_detail(employer, job["external_path"])
        info = detail.get("jobPostingInfo", {})

        raw_desc = info.get("jobDescription", "")
        job["full_description"] = strip_html(raw_desc)
        job["apply_url"] = info.get("externalUrl", "")
        job["job_req_id"] = info.get("jobReqId", "")
        job["time_type"] = info.get("timeType", "")
        job["remote_type"] = info.get("remoteType", "")

    except Exception as e:
        job["full_description"] = ""
        job["apply_url"] = ""
        job["detail_error"] = str(e)

    return job


def fetch_details(employer: dict[str, Any], jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fetch full description + apply URL for each job sequentially."""
    log.info("%s: fetching details for %d jobs...", employer["name"], len(jobs))

    completed = 0
    errors = 0
    t0 = time.time()

    for job in jobs:
        _fetch_one_detail(employer, job)
        completed += 1
        if "detail_error" in job:
            errors += 1

        if completed % 20 == 0 or completed == len(jobs):
            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            log.info("%s: %d/%d (%d errors) [%.1f jobs/sec]",
                     employer["name"], completed, len(jobs), errors, rate)

    elapsed = time.time() - t0
    log.info("%s: done in %.1fs (%.1f jobs/sec)", employer["name"], elapsed, len(jobs) / elapsed if elapsed > 0 else 0)
    return jobs


async def _iter_search_employer_pages(
    client: httpx.AsyncClient,
    employer_key: str,
    employer: dict[str, Any],
    search_text: str,
    location_filter: bool,
    max_results: int,
    accept_locs: list[str],
    reject_locs: list[str],
) -> AsyncIterator[list[dict[str, Any]]]:
    """Yield filtered search pages so details can be fetched and stored incrementally."""
    log.info("%s: searching %r...", employer.get("name", employer_key), search_text)
    offset = 0
    page_size = 20
    max_pages = 25
    total: int | None = None
    emitted = 0

    for _ in range(max_pages):
        try:
            payload = await workday_search_async(client, employer, search_text, page_size, offset)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("%s: Workday API error at offset %d: %s", employer.get("name", employer_key), offset, exc)
            break
        if total is None:
            total_value = payload.get("total") or 0
            try:
                total = int(total_value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("Workday search response has an invalid total") from exc
        postings = payload.get("jobPostings", [])
        if not isinstance(postings, list) or not postings:
            break

        page_jobs: list[dict[str, Any]] = []
        for posting in postings:
            if not isinstance(posting, dict):
                continue
            location = str(posting.get("locationsText") or "")
            if location_filter and not config.is_location_allowed(location, accept=accept_locs, reject=reject_locs):
                continue
            page_jobs.append(
                {
                    "title": str(posting.get("title") or ""),
                    "location": location,
                    "posted": str(posting.get("postedOn") or ""),
                    "external_path": str(posting.get("externalPath") or ""),
                    "employer_key": employer_key,
                    "employer_name": str(employer.get("name") or employer_key),
                }
            )

        if max_results > 0:
            page_jobs = page_jobs[: max_results - emitted]
        if page_jobs:
            emitted += len(page_jobs)
            yield page_jobs
        offset += page_size
        if offset >= (total or 0) or (max_results > 0 and emitted >= max_results):
            break


async def _fetch_one_detail_async(
    client: httpx.AsyncClient,
    employer: dict[str, Any],
    job: dict[str, Any],
) -> dict[str, Any]:
    try:
        detail = await workday_detail_async(client, employer, str(job["external_path"]))
        info = detail.get("jobPostingInfo", {})
        if not isinstance(info, dict):
            raise ValueError("Workday response omitted jobPostingInfo")
        job["full_description"] = strip_html(str(info.get("jobDescription") or ""))
        job["apply_url"] = str(info.get("externalUrl") or "")
        job["job_req_id"] = str(info.get("jobReqId") or "")
        job["time_type"] = str(info.get("timeType") or "")
        job["remote_type"] = str(info.get("remoteType") or "")
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        job["full_description"] = ""
        job["apply_url"] = ""
        job["detail_error"] = str(exc)
    return job


async def _fetch_details_async(
    client: httpx.AsyncClient,
    employer: dict[str, Any],
    jobs: list[dict[str, Any]],
    semaphore: asyncio.Semaphore,
) -> list[dict[str, Any]]:
    if not jobs:
        return []

    results: list[dict[str, Any] | None] = [None] * len(jobs)
    next_index = 0

    async def fetch_worker() -> None:
        nonlocal next_index
        while next_index < len(jobs):
            index = next_index
            next_index += 1
            async with semaphore:
                results[index] = await _fetch_one_detail_async(client, employer, jobs[index])

    worker_count = min(len(jobs), _DETAIL_WORKERS_PER_EMPLOYER)
    await asyncio.gather(*(fetch_worker() for _ in range(worker_count)))
    return [job for job in results if job is not None]


def _store_workday_employer_results(
    employer: dict[str, Any],
    jobs: list[dict[str, Any]],
    employers: dict[str, Any],
) -> tuple[int, int]:
    conn = init_db()
    try:
        return store_results(conn, jobs, employers)
    finally:
        close_connection()


async def _scrape_employers_with_client(
    client: httpx.AsyncClient,
    search_text: str,
    employers: dict[str, Any],
    employer_keys: list[str],
    location_filter: bool,
    max_results: int,
    accept_locs: list[str],
    reject_locs: list[str],
    workers: int,
) -> dict[str, int]:
    workers = max(1, min(workers, _MAX_WORKDAY_CONCURRENCY))
    employer_semaphore = asyncio.Semaphore(workers)
    detail_semaphore = asyncio.Semaphore(min(_MAX_WORKDAY_CONCURRENCY, workers * _DETAIL_WORKERS_PER_EMPLOYER))

    async def process(employer_key: str) -> dict[str, int]:
        employer = employers[employer_key]
        totals = {"found": 0, "new": 0, "existing": 0, "errors": 0}
        async with employer_semaphore:
            try:
                async for page_jobs in _iter_search_employer_pages(
                    client,
                    employer_key,
                    employer,
                    search_text,
                    location_filter,
                    max_results,
                    accept_locs,
                    reject_locs,
                ):
                    detailed_jobs = await _fetch_details_async(client, employer, page_jobs, detail_semaphore)
                    totals["found"] += len(detailed_jobs)
                    if detailed_jobs:
                        new_count, existing_count = await asyncio.to_thread(
                            _store_workday_employer_results, employer, detailed_jobs, employers
                        )
                        totals["new"] += new_count
                        totals["existing"] += existing_count
                return totals
            except Exception:
                log.exception("Workday employer worker failed for %s (%s)", employer_key, search_text)
                totals["errors"] += 1
                return totals

    totals = {"found": 0, "new": 0, "existing": 0, "errors": 0}
    next_index = 0

    async def employer_worker() -> None:
        nonlocal next_index
        while next_index < len(employer_keys):
            index = next_index
            next_index += 1
            result = await process(employer_keys[index])
            for key in totals:
                totals[key] += result[key]

    worker_count = min(workers, len(employer_keys))
    await asyncio.gather(*(employer_worker() for _ in range(worker_count)))
    return totals


async def scrape_employers_async(
    search_text: str,
    employers: dict[str, Any],
    employer_keys: list[str] | None = None,
    location_filter: bool = True,
    max_results: int = 0,
    accept_locs: list[str] | None = None,
    reject_locs: list[str] | None = None,
    workers: int = 1,
) -> dict[str, int]:
    """Async public Workday scraper with connection pooling and bounded concurrency."""
    valid_keys = [key for key in (employer_keys or list(employers)) if key in employers]
    if not valid_keys:
        return {"found": 0, "new": 0, "existing": 0, "errors": 0}
    concurrency = max(1, min(workers, len(valid_keys), _MAX_WORKDAY_CONCURRENCY))
    async with build_async_client(
        timeout=httpx.Timeout(30.0, connect=5.0),
        max_connections=max(8, concurrency * 4),
        max_keepalive_connections=max(4, concurrency * 2),
        headers={"User-Agent": _WORKDAY_UA},
        proxy=_proxy_url,
    ) as client:
        return await _scrape_employers_with_client(
            client,
            search_text,
            employers,
            valid_keys,
            location_filter,
            max_results,
            accept_locs or [],
            reject_locs or [],
            concurrency,
        )


# -- DB storage --------------------------------------------------------------

def store_results(conn: sqlite3.Connection, jobs: list[dict[str, Any]], employers: dict[str, Any]) -> tuple[int, int]:
    """Store corporate jobs in DB. Returns (new, existing)."""
    from applypilot.database import compute_job_fingerprint, ensure_columns
    from applypilot.enrichment.sponsors import detect_sponsor_status, detect_work_arrangement

    ensure_columns(conn)
    now = datetime.now(UTC).isoformat()
    new = 0
    existing = 0

    for job in jobs:
        url = job.get("apply_url", "")
        if not url:
            emp = employers.get(job.get("employer_key", ""), {})
            if emp and job.get("external_path"):
                url = f"{emp['base_url']}/{emp['site_id']}{job['external_path']}"
        if not url:
            continue

        description = job.get("full_description", "")
        short_desc = description[:500] if description else None
        full_description = description if len(description) > 200 else None
        detail_scraped_at = now if full_description else None
        detail_error = job.get("detail_error")

        site = job.get("employer_name", "Corporate")
        strategy = "workday_api"
        title = job.get("title")
        loc = job.get("location")
        fp = compute_job_fingerprint(site, title, loc)
        sponsor = detect_sponsor_status(site, title, full_description or short_desc, loc)
        work_arr = detect_work_arrangement(title, loc, full_description or short_desc)

        try:
            conn.execute(
                "INSERT INTO jobs (url, title, company, salary, description, location, site, strategy, "
                "discovered_at, full_description, application_url, detail_scraped_at, detail_error, "
                "fingerprint, sponsor_status, work_arrangement, crm_stage) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (url, title, site, None, short_desc, loc,
                 site, strategy, now, full_description, url, detail_scraped_at, detail_error,
                 fp, sponsor, work_arr, "discovered"),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    conn.commit()
    return new, existing


def _process_one(
    employer_key: str,
    employers: dict[str, Any],
    search_text: str,
    location_filter: bool,
    accept_locs: list[str],
    reject_locs: list[str],
) -> dict[str, Any]:
    """Search one employer, fetch details, store results."""
    emp = employers[employer_key]

    try:
        jobs = search_employer(
            employer_key, emp, search_text,
            location_filter=location_filter,
            accept_locs=accept_locs,
            reject_locs=reject_locs,
        )
    except Exception as e:
        log.error("%s: ERROR searching '%s': %s", emp["name"], search_text, e)
        return {"employer": emp["name"], "query": search_text,
                "found": 0, "new": 0, "existing": 0, "error": str(e)}

    if not jobs:
        return {"employer": emp["name"], "query": search_text,
                "found": 0, "new": 0, "existing": 0}

    try:
        jobs = fetch_details(emp, jobs)
    except Exception as e:
        log.error("%s: ERROR fetching details for '%s': %s", emp["name"], search_text, e)

    conn = get_connection()
    new, existing = store_results(conn, jobs, employers)
    log.info("%s: %d new, %d already in DB", emp["name"], new, existing)

    return {"employer": emp["name"], "query": search_text,
            "found": len(jobs), "new": new, "existing": existing}


# -- Public sync/async orchestrators ------------------------------------------

_AsyncResult = TypeVar("_AsyncResult")


def _run_async_sync(coroutine: Coroutine[Any, Any, _AsyncResult]) -> _AsyncResult:
    """Run an async operation from the legacy synchronous CLI interface."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    raise RuntimeError("This synchronous Workday entry point cannot run inside an event loop; await its async variant")


def scrape_employers(
    search_text: str,
    employers: dict[str, Any],
    employer_keys: list[str] | None = None,
    location_filter: bool = True,
    max_results: int = 0,
    accept_locs: list[str] | None = None,
    reject_locs: list[str] | None = None,
    workers: int = 1,
) -> dict[str, int]:
    """Backward-compatible synchronous wrapper for async Workday discovery."""
    return _run_async_sync(
        scrape_employers_async(
            search_text=search_text,
            employers=employers,
            employer_keys=employer_keys,
            location_filter=location_filter,
            max_results=max_results,
            accept_locs=accept_locs,
            reject_locs=reject_locs,
            workers=workers,
        )
    )


async def run_workday_discovery_async(
    employers: dict[str, Any] | None = None,
    workers: int = 1,
    search_cfg: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Async Workday crawl; shares one HTTP pool across queries and employers."""
    if employers is None:
        employers = await asyncio.to_thread(load_employers)
    if not employers:
        log.warning("No employers configured. Create config/employers.yaml.")
        return {"found": 0, "new": 0, "existing": 0, "queries": 0}

    if search_cfg is None:
        loaded_search_cfg = await asyncio.to_thread(config.load_search_config)
        search_cfg = loaded_search_cfg if isinstance(loaded_search_cfg, dict) else {}
    resolved_search_cfg: dict[str, Any] = search_cfg
    queries_cfg = resolved_search_cfg.get("queries", [])
    accept_locs, reject_locs = config.load_location_filters(resolved_search_cfg)
    max_tier = resolved_search_cfg.get("workday_max_tier", 2)
    queries = [str(query["query"]) for query in queries_cfg if query.get("tier", 99) <= max_tier]
    if not queries:
        queries = [str(query["query"]) for query in queries_cfg if query.get("query")]
    if not queries:
        log.warning("No search queries configured in searches.yaml.")
        return {"found": 0, "new": 0, "existing": 0, "queries": 0}

    proxy = resolved_search_cfg.get("proxy")
    if proxy:
        setup_proxy(str(proxy))
    location_filter = bool(resolved_search_cfg.get("workday_location_filter", True))
    concurrency = max(1, min(workers, max(1, len(employers)), _MAX_WORKDAY_CONCURRENCY))
    log.info("Workday crawl: %d queries x %d employers (workers=%d)", len(queries), len(employers), concurrency)

    totals = {"found": 0, "new": 0, "existing": 0}
    async with build_async_client(
        timeout=httpx.Timeout(30.0, connect=5.0),
        max_connections=max(8, concurrency * 4),
        max_keepalive_connections=max(4, concurrency * 2),
        headers={"User-Agent": _WORKDAY_UA},
        proxy=_proxy_url,
    ) as client:
        for index, query in enumerate(queries, 1):
            log.info("Query %d/%d: %r", index, len(queries), query)
            result = await _scrape_employers_with_client(
                client,
                query,
                employers,
                list(employers),
                location_filter,
                0,
                accept_locs,
                reject_locs,
                concurrency,
            )
            for key in totals:
                totals[key] += result[key]

    log.info(
        "Workday crawl done: %d found, %d new, %d existing across %d queries x %d employers",
        totals["found"], totals["new"], totals["existing"], len(queries), len(employers),
    )
    return {**totals, "queries": len(queries)}


def run_workday_discovery(employers: dict[str, Any] | None = None, workers: int = 1) -> dict[str, int]:
    """Synchronous CLI entry point delegating all network work to asyncio."""
    return _run_async_sync(run_workday_discovery_async(employers=employers, workers=workers))
