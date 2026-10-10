"""Detail page enrichment: scrapes full descriptions and apply URLs.

For each job URL in the database, navigates to the detail page and extracts:
  - full_description: the complete job posting text
  - application_url: the "Apply" button/link URL

Three-tier extraction cascade (cheapest first):
  Tier 1: JSON-LD JobPosting structured data (0 tokens)
  Tier 2: Deterministic CSS pattern matching (0 tokens)
  Tier 3: LLM-assisted extraction (1 LLM call)
"""

import json
import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Iterator
from datetime import UTC, datetime
from threading import Event, Lock
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import Page, Response, sync_playwright

from applypilot.browser import install_dom_scraping_routes
from applypilot.database import close_connection, init_db
from applypilot.llm import get_client
from applypilot.schemas import JobDescriptionResponse

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_DETAIL_DB_BATCH_SIZE = 500
_MAX_DETAIL_SITE_WORKERS = 8
_URL_RESOLUTION_BATCH_SIZE = 500
_DETAIL_JSON_LD_MAX_DOCUMENTS = 16
_DETAIL_JSON_LD_MAX_DOCUMENT_CHARS = 256_000
_DETAIL_JSON_LD_MAX_TOTAL_CHARS = 1_000_000
_WTTJ_RESPONSE_MAX_CHARS = 8_000_000

# Sites that block scraping -- skip detail extraction entirely
SKIP_DETAIL_SITES = {"glassdoor", "google", "Workopolis"}

# Module-level proxy config (set from CLI or caller)
_PROXY_CONFIG: dict[str, Any] | None = None


def set_proxy(proxy_str: str | None) -> None:
    """Set proxy config from an external caller."""
    global _PROXY_CONFIG
    if proxy_str:
        from applypilot.discovery.jobspy import parse_proxy
        _PROXY_CONFIG = parse_proxy(proxy_str)


# -- URL resolution ----------------------------------------------------------

def _load_base_urls() -> dict[str, str | None]:
    """Load site base URLs from config/sites.yaml."""
    from applypilot.config import load_base_urls
    return load_base_urls()


def resolve_url(raw_url: str, site: str) -> str | None:
    """Resolve a stored URL to an absolute URL."""
    if not raw_url:
        return None

    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        return raw_url

    if site == "WelcomeToTheJungle":
        return None

    if site == "Randstad Canada" and "/" not in raw_url:
        return f"https://www.randstad.ca/jobs/search/{raw_url}"

    if site == "4DayWeek" and raw_url in ("/", "/jobs"):
        return None

    base = _load_base_urls().get(site)
    if not base:
        return None

    if ";jsessionid=" in raw_url:
        raw_url = raw_url.split(";jsessionid=")[0]

    return urljoin(base, raw_url)


def resolve_all_urls(conn: sqlite3.Connection) -> dict[str, int]:
    """Resolve stored relative URLs in bounded rowid batches."""
    max_row = conn.execute("SELECT MAX(rowid) FROM jobs").fetchone()
    max_rowid = int(max_row[0]) if max_row is not None and max_row[0] is not None else 0
    resolved = 0
    failed = 0
    already_absolute = 0
    last_rowid = 0

    while last_rowid < max_rowid:
        rows = conn.execute(
            "SELECT rowid, url, site FROM jobs WHERE rowid > ? AND rowid <= ? "
            "ORDER BY rowid LIMIT ?",
            (last_rowid, max_rowid, _URL_RESOLUTION_BATCH_SIZE),
        ).fetchall()
        if not rows:
            break
        last_rowid = int(rows[-1][0])
        for row in rows:
            url, site = row[1], row[2]
            if url.startswith("http://") or url.startswith("https://"):
                already_absolute += 1
                continue

            new_url = resolve_url(url, site)
            if new_url and new_url != url:
                try:
                    conn.execute("UPDATE jobs SET url = ? WHERE url = ?", (new_url, url))
                    resolved += 1
                except sqlite3.IntegrityError:
                    conn.execute("DELETE FROM jobs WHERE url = ?", (url,))
                    resolved += 1
            else:
                failed += 1

    # Also resolve relative application_urls without loading all remaining jobs.
    app_resolved = 0
    last_rowid = 0
    while last_rowid < max_rowid:
        rows = conn.execute(
            "SELECT rowid, url, site, application_url FROM jobs "
            "WHERE rowid > ? AND rowid <= ? AND application_url IS NOT NULL "
            "AND application_url != '' AND application_url NOT LIKE 'http%' "
            "ORDER BY rowid LIMIT ?",
            (last_rowid, max_rowid, _URL_RESOLUTION_BATCH_SIZE),
        ).fetchall()
        if not rows:
            break
        last_rowid = int(rows[-1][0])
        for row in rows:
            url, site, app_url = row[1], row[2], row[3]
            new_app = resolve_url(app_url, site)
            if new_app and new_app != app_url:
                conn.execute("UPDATE jobs SET application_url = ? WHERE url = ?", (new_app, url))
                app_resolved += 1

    conn.commit()
    return {
        "resolved": resolved,
        "failed": failed,
        "already_absolute": already_absolute,
        "app_resolved": app_resolved,
    }


def resolve_wttj_urls(conn: sqlite3.Connection) -> int:
    """Re-fetch WTTJ Algolia API to get proper detail URLs and fix slug-as-title.
    Returns count of URLs updated."""
    wttj_count_row = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE site = 'WelcomeToTheJungle'"
    ).fetchone()
    if wttj_count_row is None or int(wttj_count_row[0]) == 0:
        return 0
    max_row = conn.execute(
        "SELECT MAX(rowid) FROM jobs WHERE site = 'WelcomeToTheJungle'"
    ).fetchone()
    max_rowid = int(max_row[0]) if max_row is not None and max_row[0] is not None else 0

    algolia_data: dict[str, Any] = {}

    def capture_algolia(response: Response) -> None:
        if "algolia.net" not in response.url or "/queries" not in response.url:
            return
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > _WTTJ_RESPONSE_MAX_CHARS:
                    return
            except ValueError:
                return
        try:
            response_text = response.text()
            if len(response_text) <= _WTTJ_RESPONSE_MAX_CHARS:
                algolia_data["response"] = json.loads(response_text)
        except Exception:
            return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = None
        page = None
        try:
            context = browser.new_context(user_agent=UA)
            install_dom_scraping_routes(context)
            page = context.new_page()
            page.on("response", capture_algolia)
            page.goto(
                "https://www.welcometothejungle.com/en/jobs?query=developer&refinementList%5Bremote%5D%5B%5D=fulltime",
                timeout=60000,
            )
            page.wait_for_load_state("networkidle")
        finally:
            if page is not None:
                page.close()
            if context is not None:
                context.close()
            browser.close()

    if not algolia_data.get("response"):
        log.warning("WTTJ: No Algolia response captured")
        return 0

    response_data = algolia_data.get("response")
    if not isinstance(response_data, dict):
        log.warning("WTTJ: Algolia response was not a JSON object")
        return 0
    results = response_data.get("results", [])
    if not isinstance(results, list):
        log.warning("WTTJ: Algolia results were not a list")
        return 0

    def _build_slug_map(results_list: list[Any]) -> dict[str, dict[str, str]]:
        slug_results: dict[str, dict[str, str]] = {}
        for result in results_list:
            if not isinstance(result, dict):
                continue
            hits = result.get("hits", [])
            if not isinstance(hits, list):
                continue
            for hit in hits:
                if not isinstance(hit, dict):
                    continue
                slug = str(hit.get("slug") or "")
                organization = hit.get("organization", {})
                org_slug = str(organization.get("slug") or "") if isinstance(organization, dict) else ""
                name = str(hit.get("name") or "")
                if slug and org_slug:
                    detail_url = f"https://www.welcometothejungle.com/en/companies/{org_slug}/jobs/{slug}"
                    slug_results[slug] = {"url": detail_url, "name": name}
        return slug_results

    slug_map = _build_slug_map(results)
    response_data.clear()
    algolia_data.clear()
    del results
    del response_data

    def _update_wttj_job(old_url: str, old_title: str) -> bool:
        slug = old_url.split("_DFNS_")[0] if "_DFNS_" in old_url else old_url
        match = slug_map.get(slug) or slug_map.get(old_url)
        if match:
            try:
                conn.execute(
                    "UPDATE jobs SET url = ?, title = ? WHERE url = ?",
                    (match["url"], match["name"] or old_title, old_url),
                )
            except sqlite3.IntegrityError:
                conn.execute("DELETE FROM jobs WHERE url = ?", (old_url,))
            return True

        for candidate_slug, data in slug_map.items():
            if candidate_slug in old_url or old_url in candidate_slug:
                try:
                    conn.execute(
                        "UPDATE jobs SET url = ?, title = ? WHERE url = ?",
                        (data["url"], data["name"] or old_title, old_url),
                    )
                except sqlite3.IntegrityError:
                    conn.execute("DELETE FROM jobs WHERE url = ?", (old_url,))
                return True
        return False

    updated = 0
    last_rowid = 0
    while last_rowid < max_rowid:
        wttj_rows = conn.execute(
            "SELECT rowid, url, title FROM jobs "
            "WHERE site = 'WelcomeToTheJungle' AND rowid > ? AND rowid <= ? "
            "ORDER BY rowid LIMIT ?",
            (last_rowid, max_rowid, _URL_RESOLUTION_BATCH_SIZE),
        ).fetchall()
        if not wttj_rows:
            break
        last_rowid = int(wttj_rows[-1][0])
        for row in wttj_rows:
            if _update_wttj_job(str(row[1]), str(row[2] or "")):
                updated += 1

    conn.commit()
    return updated


# -- Detail page intelligence ------------------------------------------------

def collect_detail_intelligence(page: Page) -> dict[str, Any]:
    """Collect bounded JSON-LD signals from a detail page without API interception."""
    intel: dict[str, Any] = {"json_ld": [], "page_title": "", "final_url": ""}

    intel["page_title"] = page.title()
    intel["final_url"] = page.url
    scripts = page.locator('script[type="application/ld+json"]')
    script_count = min(scripts.count(), _DETAIL_JSON_LD_MAX_DOCUMENTS)
    total_chars = 0

    for index in range(script_count):
        remaining_chars = _DETAIL_JSON_LD_MAX_TOTAL_CHARS - total_chars
        if remaining_chars <= 0:
            break
        max_chars = min(_DETAIL_JSON_LD_MAX_DOCUMENT_CHARS, remaining_chars)
        try:
            text = scripts.nth(index).evaluate(
                "(element, limit) => String(element.textContent || '').slice(0, limit + 1)",
                max_chars,
            )
            if not isinstance(text, str) or len(text) > max_chars:
                continue
            data = json.loads(text)
            intel["json_ld"].append(data)
            total_chars += len(text)
        except Exception:
            continue

    return intel


# -- Tier 1: JSON-LD extraction -----------------------------------------------

def extract_from_json_ld(intel: dict[str, Any]) -> dict[str, Any] | None:
    """Extract description and apply URL from JSON-LD JobPosting.
    Returns {"full_description": str, "application_url": str|None} or None."""

    def find_job_posting(data: Any) -> dict[str, Any] | None:
        if isinstance(data, dict):
            if data.get("@type") == "JobPosting":
                return data
            if "@graph" in data and isinstance(data["@graph"], list):
                for item in data["@graph"]:
                    result = find_job_posting(item)
                    if result:
                        return result
        elif isinstance(data, list):
            for item in data:
                result = find_job_posting(item)
                if result:
                    return result
        return None

    for ld in intel.get("json_ld", []):
        posting = find_job_posting(ld)
        if not posting:
            continue

        desc = posting.get("description", "")
        if not desc:
            continue

        desc_clean = clean_description(desc)
        if len(desc_clean) < 50:
            continue

        apply_url = None
        if posting.get("directApply"):
            apply_url = posting.get("url")
        if not apply_url:
            contact = posting.get("applicationContact")
            if isinstance(contact, dict):
                apply_url = contact.get("url")
        if not apply_url:
            apply_url = posting.get("url")

        return {
            "full_description": desc_clean,
            "application_url": apply_url,
        }

    return None


# -- Tier 2: Deterministic pattern matching ----------------------------------

APPLY_SELECTORS = [
    'a[href*="apply"]',
    'a[data-testid*="apply"]',
    'a[class*="apply"]',
    'a[aria-label*="pply"]',
    'button[data-testid*="apply"]',
    'a#apply_button',
    '.postings-btn-wrapper a',
    'a.ashby-job-posting-apply-button',
    '#grnhse_app a[href*="apply"]',
    'a[data-qa="btn-apply"]',
    'a[class*="btn-apply"]',
    'a[class*="apply-btn"]',
    'a[class*="apply-button"]',
    # European & UK ATS platforms (Personio, Teamtailor, Pinpoint, SmartRecruiters, Workable, Recruitee)
    'a.career-btn-apply',
    '[data-controller="career--apply-button"] a',
    'a[href*="/applications/new"]',
    'a#st-apply',
    'a[data-ui="overview-apply-now"]',
    'a[href*="/c/new"]',
]

DESCRIPTION_SELECTORS = [
    '#job-description',
    '#job_description',
    '#jobDescriptionText',
    '.job-description',
    '.job_description',
    '[class*="job-description"]',
    '[class*="jobDescription"]',
    '[data-testid*="description"]',
    '[data-testid="job-description"]',
    '.posting-page .posting-categories + div',
    '#content .posting-page',
    '#app_body .content',
    '#grnhse_app .content',
    '.ashby-job-posting-description',
    '[class*="posting-description"]',
    '[class*="job-detail"]',
    '[class*="jobDetail"]',
    '[class*="job-content"]',
    '[class*="job-body"]',
    '[role="main"] article',
    'main article',
    'article[class*="job"]',
    '.job-posting-content',
    # European & UK ATS platforms (Personio, Teamtailor, Pinpoint, SmartRecruiters, Workable)
    '.job-detail-description',
    '.body-text',
    '[itemprop="description"]',
    '[data-ui="job-description"]',
    '.rt-text',
]


def extract_apply_url_deterministic(page: Page) -> str | None:
    """Try known CSS patterns for apply buttons/links."""
    for sel in APPLY_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                href = el.get_attribute("href")
                if href and href != "#":
                    return href
                tag = el.evaluate("el => el.tagName.toLowerCase()")
                if tag == "button":
                    parent_href = el.evaluate("el => el.parentElement?.querySelector('a')?.href || null")
                    if isinstance(parent_href, str) and parent_href:
                        return parent_href
                    return page.url
        except Exception:
            continue

    try:
        links = page.query_selector_all("a")
        for link in links:
            text = link.inner_text().strip().lower()
            if "apply" in text and len(text) < 50:
                href = link.get_attribute("href")
                if href and href != "#" and "javascript:" not in href:
                    return href
    except Exception:
        pass

    return None


def extract_description_deterministic(page: Page) -> str | None:
    """Try known CSS patterns for the job description block."""
    for sel in DESCRIPTION_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                text = el.inner_text().strip()
                if len(text) >= 100:
                    return clean_description(text)
        except Exception:
            continue

    return None


# -- Tier 3: LLM extraction -------------------------------------------------

DETAIL_EXTRACT_PROMPT = """You are extracting job details from a single job posting page.

PAGE URL: {url}
PAGE TITLE: {title}

Find TWO things in the HTML below:
1. The full job description text (responsibilities, requirements, etc.)
2. The URL of the "Apply" button/link

Rules:
- For description: extract the FULL text. Include all sections (About, Responsibilities, Requirements, etc.)
- For apply URL: find the href of the link/button that starts the application process
- If you cannot find one, set it to null

Return ONLY valid JSON:
{{"full_description": "the complete job description text here", "application_url": "https://..." or null}}

No explanation, no markdown. Keep reasoning under 20 words.

HTML:
{content}"""


def extract_main_content(page: Page) -> str:
    """Extract the main content area, stripped of navigation noise."""
    for sel in ["main", "article", '[role="main"]', "#content", ".content"]:
        try:
            el = page.query_selector(sel)
            if el:
                text_len = len(el.inner_text().strip())
                if text_len > 200:
                    html = el.inner_html()
                    if len(html) < 50000:
                        return clean_content_html(html)
        except Exception:
            continue

    try:
        html = page.evaluate("""
            () => {
                const clone = document.body.cloneNode(true);
                clone.querySelectorAll('nav, header, footer, script, style, noscript, svg, iframe').forEach(el => el.remove());
                return clone.innerHTML;
            }
        """)
        return clean_content_html(html[:50000])
    except Exception:
        return ""


def clean_content_html(html: str) -> str:
    """Clean detail page HTML for LLM consumption."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.select("script, style, noscript, svg, iframe, nav, header, footer"):
        tag.decompose()

    for tag in soup.find_all(True):
        new_attrs: dict[str, Any] = {}
        for attr, val in list(tag.attrs.items()):
            if attr in ("id", "href", "class", "role", "aria-label", "data-testid", "name", "for", "type"):
                if attr == "class":
                    classes = val if isinstance(val, list) else val.split()
                    kept = [c for c in classes if len(c) < 30 and not re.match(r"^[a-z]{1,2}-\d+$", c)]
                    if kept:
                        new_attrs["class"] = " ".join(kept[:3])
                else:
                    new_attrs[attr] = val
            elif attr.startswith("data-") or attr.startswith("aria-"):
                new_attrs[attr] = val
        tag.attrs = new_attrs

    return str(soup)


def extract_with_llm(page: Page, url: str) -> dict[str, Any]:
    """Send focused HTML to LLM for extraction. Fallback tier."""
    content = extract_main_content(page)
    if not content:
        return {"full_description": None, "application_url": None}

    title = ""
    try:
        title = page.title()
    except Exception:
        pass

    prompt = DETAIL_EXTRACT_PROMPT.format(
        url=url,
        title=title,
        content=content[:30000],
    )

    try:
        client = get_client()
        t0 = time.time()
        result = client.ask_structured(
            prompt,
            response_model=JobDescriptionResponse,
            temperature=0.0,
            max_tokens=4096,
        )
        elapsed = time.time() - t0
        log.info("LLM: %d chars in, %.1fs", len(prompt), elapsed)
        description = clean_description(result.full_description) if result.full_description else None
        return {"full_description": description, "application_url": result.application_url}
    except Exception as e:
        log.error("LLM ERROR: %s", e)
        return {"full_description": None, "application_url": None}


# -- Description cleaning ---------------------------------------------------

def clean_description(text: str) -> str:
    """Convert HTML description to clean readable text."""
    if not text:
        return ""

    if "<" in text and ">" in text:
        soup = BeautifulSoup(text, "html.parser")
        for br in soup.find_all("br"):
            br.replace_with("\n")
        for tag in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "li", "tr"]):
            tag.insert_before("\n")
            tag.insert_after("\n")
        for li in soup.find_all("li"):
            li.insert_before("- ")
        text = soup.get_text()

    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if line:
            lines.append(line)

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# -- Orchestration -----------------------------------------------------------

SITE_DELAYS = {
    "RemoteOK": 3.0,
    "WelcomeToTheJungle": 2.0,
    "Job Bank Canada": 1.5,
    "CareerJet Canada": 3.0,
    "Hacker News Jobs": 1.0,
    "BuiltIn Remote": 2.0,
}

RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}
PERMANENT_FAILURES = {404, 410, 451}


def scrape_detail_page(page: Page, url: str) -> dict[str, Any]:
    """Full cascade for one detail page."""
    result: dict[str, Any] = {
        "full_description": None,
        "application_url": None,
        "status": "error",
        "tier_used": None,
        "error": None,
    }
    t0 = time.time()

    try:
        resp = page.goto(url, timeout=45000)
        if resp and resp.status in PERMANENT_FAILURES:
            result["error"] = f"HTTP {resp.status}"
            result["elapsed"] = time.time() - t0
            return result
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
    except Exception as e:
        err_str = str(e)
        if "timeout" in err_str.lower():
            result["error"] = "timeout"
        else:
            result["error"] = err_str[:200]
        result["elapsed"] = time.time() - t0
        return result

    intel = collect_detail_intelligence(page)

    # Tier 1: JSON-LD
    json_ld_result = extract_from_json_ld(intel)
    if json_ld_result and json_ld_result.get("full_description"):
        result.update(json_ld_result)
        result["tier_used"] = 1
        if not result.get("application_url"):
            apply = extract_apply_url_deterministic(page)
            if apply:
                result["application_url"] = apply
        result["status"] = "ok" if result.get("application_url") else "partial"
        result["elapsed"] = time.time() - t0
        return result

    # Tier 2: Deterministic CSS
    desc = extract_description_deterministic(page)
    apply = extract_apply_url_deterministic(page)

    if desc:
        result["full_description"] = desc
        result["application_url"] = apply
        result["tier_used"] = 2
        result["status"] = "ok" if apply else "partial"
        result["elapsed"] = time.time() - t0
        return result

    tier2_apply = apply

    # Tier 3: LLM
    llm_result = extract_with_llm(page, url)
    result["full_description"] = llm_result.get("full_description")
    result["application_url"] = llm_result.get("application_url") or tier2_apply
    result["tier_used"] = 3

    if result.get("full_description"):
        result["status"] = "ok" if result.get("application_url") else "partial"
    elif result.get("application_url"):
        result["status"] = "partial"
    else:
        result["status"] = "error"
        result["error"] = "no data extracted"

    result["elapsed"] = time.time() - t0
    return result


def scrape_site_batch(
    conn: sqlite3.Connection | None,
    site: str,
    jobs: list[tuple[str, str]],
    delay: float = 2.0,
    max_jobs: int | None = None,
) -> dict[str, Any]:
    """Scrape one site's listings with a pooled context and guaranteed teardown.

    A context is recycled every 50 navigations to bound browser memory and
    cookie/cache growth. Images, fonts, stylesheets, media, and analytics are
    routed away because extraction depends on text and DOM structure only.
    """
    stats: dict[str, Any] = {"processed": 0, "ok": 0, "partial": 0, "error": 0, "tiers": {1: 0, 2: 0, 3: 0}}
    selected_jobs = jobs[:max_jobs] if max_jobs else jobs
    if not selected_jobs:
        return stats

    own_conn = conn is None
    database_conn = conn if conn is not None else init_db()
    now = datetime.now(UTC).isoformat()

    try:
        with sync_playwright() as playwright:
            launch_options: dict[str, Any] = {"headless": True}
            if _PROXY_CONFIG:
                launch_options["proxy"] = _PROXY_CONFIG["playwright"]
            browser = playwright.chromium.launch(**launch_options)
            context = None
            page = None
            try:
                context = browser.new_context(user_agent=UA)
                install_dom_scraping_routes(context)
                page = context.new_page()

                for index, (url, title) in enumerate(selected_jobs):
                    log.info("[%d/%d] %s", index + 1, len(selected_jobs), title[:50] if title else url[:50])
                    try:
                        result = scrape_detail_page(page, url)
                    except Exception as exc:
                        log.exception("Detail scrape failed for %s", url)
                        result = {"status": "error", "error": str(exc)[:200], "elapsed": 0.0}

                    stats["processed"] += 1
                    tier = result.get("tier_used")
                    status = str(result.get("status", "error"))
                    elapsed = float(result.get("elapsed", 0.0))
                    if tier:
                        stats["tiers"][tier] = stats["tiers"].get(tier, 0) + 1

                    desc_len = len(result.get("full_description") or "")
                    err_str = f" | err={result['error']}" if result.get("error") else ""
                    log.info(
                        "  %s | %s | desc=%s chars | apply=%s | %.1fs%s",
                        status,
                        f"T{tier}" if tier else "--",
                        f"{desc_len:,}",
                        "yes" if result.get("application_url") else "no",
                        elapsed,
                        err_str,
                    )

                    if status in ("ok", "partial"):
                        stats[status] += 1
                        from applypilot.enrichment.sponsors import evaluate_sponsorship_and_metadata

                        row_meta = database_conn.execute(
                            "SELECT company, location FROM jobs WHERE url = ?", (url,)
                        ).fetchone()
                        company = (row_meta[0] if row_meta else None) or site
                        location = row_meta[1] if row_meta else None
                        metadata = evaluate_sponsorship_and_metadata(
                            company, title, result.get("full_description"), location
                        )
                        if metadata["is_expired"]:
                            database_conn.execute(
                                "UPDATE jobs SET full_description = ?, application_url = ?, "
                                "detail_scraped_at = ?, detail_error = 'expired_listing', "
                                "apply_status = 'expired', crm_stage = 'rejected', "
                                "sponsor_status = ?, work_arrangement = ? WHERE url = ?",
                                (
                                    result.get("full_description"), result.get("application_url"), now,
                                    metadata["sponsor_status"], metadata["work_arrangement"], url,
                                ),
                            )
                        else:
                            database_conn.execute(
                                "UPDATE jobs SET full_description = ?, application_url = ?, "
                                "detail_scraped_at = ?, detail_error = NULL, "
                                "sponsor_status = ?, work_arrangement = ? WHERE url = ?",
                                (
                                    result.get("full_description"), result.get("application_url"), now,
                                    metadata["sponsor_status"], metadata["work_arrangement"], url,
                                ),
                            )
                    else:
                        stats["error"] += 1
                        database_conn.execute(
                            "UPDATE jobs SET detail_error = ?, detail_scraped_at = ? WHERE url = ?",
                            (result.get("error", "unknown"), now, url),
                        )
                    database_conn.commit()

                    if delay > 0 and index < len(selected_jobs) - 1:
                        time.sleep(delay)
                    if (index + 1) % 50 == 0 and index + 1 < len(selected_jobs):
                        page.close()
                        context.close()
                        context = browser.new_context(user_agent=UA)
                        install_dom_scraping_routes(context)
                        page = context.new_page()
            finally:
                try:
                    if page is not None:
                        page.close()
                finally:
                    try:
                        if context is not None:
                            context.close()
                    finally:
                        browser.close()
    finally:
        if own_conn:
            close_connection()
    return stats


def _iter_pending_site_job_batches(
    conn: sqlite3.Connection,
    site: str,
    max_rowid: int,
    job_count: int,
    *,
    batch_size: int = _DETAIL_DB_BATCH_SIZE,
) -> Iterator[list[tuple[str, str]]]:
    """Stream a stable per-site snapshot without materializing every pending row."""
    last_rowid = 0
    emitted = 0
    while emitted < job_count:
        limit = min(batch_size, job_count - emitted)
        rows = conn.execute(
            "SELECT rowid, url, title FROM jobs "
            "WHERE COALESCE(site, '') = ? AND detail_scraped_at IS NULL AND rowid > ? AND rowid <= ? "
            "ORDER BY rowid LIMIT ?",
            (site, last_rowid, max_rowid, limit),
        ).fetchall()
        if not rows:
            break
        batch: list[tuple[str, str]] = []
        for row in rows:
            last_rowid = int(row[0])
            batch.append((str(row[1]), str(row[2] or "")))
        emitted += len(batch)
        yield batch


def _run_detail_scraper(
    conn: sqlite3.Connection,
    sites: list[str] | None = None,
    max_per_site: int | None = None,
    workers: int = 1,
) -> dict[str, Any]:
    """Stream bounded job batches per site, optionally scraping sites concurrently."""
    workers = max(1, min(workers, _MAX_DETAIL_SITE_WORKERS))
    skipped_sites = tuple(sorted(SKIP_DETAIL_SITES))
    placeholders = ", ".join("?" for _ in skipped_sites)
    rows = conn.execute(
        "SELECT COALESCE(site, '') AS site, COUNT(*), MAX(rowid) FROM jobs "
        f"WHERE detail_scraped_at IS NULL AND COALESCE(site, '') NOT IN ({placeholders}) "
        "GROUP BY COALESCE(site, '')",
        skipped_sites,
    ).fetchall()
    if not rows:
        log.info("No pending jobs to scrape.")
        return {"processed": 0, "ok": 0, "partial": 0, "error": 0}

    database_row = conn.execute("PRAGMA database_list").fetchone()
    database_path = str(database_row[2]) if database_row is not None else ""
    site_snapshots: dict[str, tuple[int, int]] = {
        str(row[0] or ""): (int(row[1]), int(row[2])) for row in rows
    }
    site_jobs = {
        site: snapshot
        for site, snapshot in site_snapshots.items()
        if not sites or site in sites
    }
    log.info(
        "Pending: %d jobs across %d sites (workers=%d)",
        sum(count for count, _ in site_snapshots.values()),
        len(site_jobs),
        workers,
    )

    known_order = [
        "RemoteOK", "Job Bank Canada", "BuiltIn Remote",
        "WelcomeToTheJungle", "CareerJet Canada", "Hacker News Jobs",
    ]
    order = [site for site in known_order if site in site_jobs]
    order += [site for site in sorted(site_jobs) if site not in order]

    total_stats: dict[str, Any] = {
        "processed": 0,
        "ok": 0,
        "partial": 0,
        "error": 0,
        "tiers": {1: 0, 2: 0, 3: 0},
    }

    def _merge_stats(target: dict[str, Any], stats: dict[str, Any]) -> None:
        for key in ("processed", "ok", "partial", "error"):
            target[key] += stats[key]
        for tier, count in stats["tiers"].items():
            target["tiers"][tier] = target["tiers"].get(tier, 0) + count

    def _selected_job_count(count: int) -> int:
        if max_per_site is None or max_per_site == 0:
            return count
        if max_per_site > 0:
            return min(count, max_per_site)
        return max(0, count + max_per_site)

    def _scrape_site_batches(database_conn: sqlite3.Connection, site: str) -> dict[str, Any]:
        count, max_rowid = site_jobs[site]
        selected_count = _selected_job_count(count)
        delay = SITE_DELAYS.get(site, 2.0)
        log.info("%s -- %d jobs (delay=%.1fs)", site, selected_count, delay)
        site_stats: dict[str, Any] = {
            "processed": 0,
            "ok": 0,
            "partial": 0,
            "error": 0,
            "tiers": {1: 0, 2: 0, 3: 0},
        }
        for batch_index, jobs in enumerate(
            _iter_pending_site_job_batches(database_conn, site, max_rowid, selected_count),
            start=1,
        ):
            if batch_index > 1 and delay > 0:
                time.sleep(delay)
            stats = scrape_site_batch(database_conn, site, jobs, delay=delay)
            _merge_stats(site_stats, stats)
        log.info(
            "%s summary: %d ok, %d partial, %d error | T1=%d T2=%d T3=%d",
            site,
            site_stats["ok"],
            site_stats["partial"],
            site_stats["error"],
            site_stats["tiers"].get(1, 0),
            site_stats["tiers"].get(2, 0),
            site_stats["tiers"].get(3, 0),
        )
        return site_stats

    if workers > 1 and len(order) > 1 and database_path:
        next_site_index = 0
        site_index_lock = Lock()
        worker_count = min(workers, len(order))

        def _scrape_site_group_in_worker() -> dict[str, Any]:
            nonlocal next_site_index
            worker_conn = init_db(database_path)
            worker_stats: dict[str, Any] = {
                "processed": 0,
                "ok": 0,
                "partial": 0,
                "error": 0,
                "tiers": {1: 0, 2: 0, 3: 0},
            }
            try:
                while True:
                    with site_index_lock:
                        if next_site_index >= len(order):
                            break
                        site = order[next_site_index]
                        next_site_index += 1
                    _merge_stats(worker_stats, _scrape_site_batches(worker_conn, site))
                return worker_stats
            finally:
                close_connection(database_path)

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = [pool.submit(_scrape_site_group_in_worker) for _ in range(worker_count)]
            for future in as_completed(futures):
                _merge_stats(total_stats, future.result())
    else:
        for site in order:
            _merge_stats(total_stats, _scrape_site_batches(conn, site))

    log.info(
        "TOTAL: %d processed | %d ok | %d partial | %d error",
        total_stats["processed"],
        total_stats["ok"],
        total_stats["partial"],
        total_stats["error"],
    )
    log.info(
        "Tier distribution: T1=%d T2=%d T3=%d",
        total_stats["tiers"].get(1, 0),
        total_stats["tiers"].get(2, 0),
        total_stats["tiers"].get(3, 0),
    )

    llm_calls = total_stats["tiers"].get(3, 0)
    total = total_stats["processed"]
    if total > 0:
        savings = ((total - llm_calls) / total) * 100
        log.info("LLM calls: %d/%d (%.0f%% saved)", llm_calls, total, savings)

    return total_stats

# -- Streaming detail scraper (for sequential pipeline) ----------------------

def stream_detail(
    upstream_done: Event | None,
    my_done: Event,
    proxy_str: str | None = None,
    poll_interval: float = 5.0,
) -> None:
    """Streaming detail scraper: polls DB for un-scraped jobs, scrapes sites sequentially.

    Args:
        upstream_done: Event set when discover+extract done. None = run once.
        my_done: Event to set when this stage completes.
        proxy_str: Proxy in host:port:user:pass format.
        poll_interval: Seconds to sleep when no pending jobs found.
    """
    if proxy_str:
        set_proxy(proxy_str)

    conn = init_db()

    url_stats = resolve_all_urls(conn)
    log.info("URL resolution: %d resolved, %d absolute",
             url_stats['resolved'], url_stats['already_absolute'])

    total_ok = 0
    total_err = 0
    t0 = time.time()

    try:
        while True:
            skipped_sites = tuple(sorted(SKIP_DETAIL_SITES))
            placeholders = ", ".join("?" for _ in skipped_sites)
            rows = conn.execute(
                "SELECT url, title, site FROM jobs "
                f"WHERE detail_scraped_at IS NULL AND site NOT IN ({placeholders}) "
                "ORDER BY site LIMIT 200",
                skipped_sites,
            ).fetchall()

            if rows:
                site_jobs: dict[str, list[tuple[str, str]]] = {}
                for row in rows:
                    url, title, site = row[0], row[1], row[2]
                    site_jobs.setdefault(site, []).append((url, title))

                for site, jobs in site_jobs.items():
                    delay = SITE_DELAYS.get(site, 2.0)
                    log.info("%s: %d jobs (delay=%.1fs)", site, len(jobs), delay)

                    try:
                        stats = scrape_site_batch(conn, site, jobs, delay=delay)
                        total_ok += stats["ok"] + stats["partial"]
                        total_err += stats["error"]
                        log.info("%s: %d ok, %d partial, %d error",
                                 site, stats['ok'], stats['partial'], stats['error'])
                    except Exception as e:
                        log.error("%s: CRASHED: %s", site, e)

            upstream_finished = upstream_done is None or upstream_done.is_set()
            if upstream_finished and not rows:
                break
            if not rows:
                time.sleep(poll_interval)
    finally:
        elapsed = time.time() - t0
        if total_ok or total_err:
            log.info("DONE: %d ok, %d errors in %.1fs", total_ok, total_err, elapsed)
        close_connection()
        my_done.set()


# -- Public entry point ------------------------------------------------------

def run_enrichment(limit: int = 100, workers: int = 1) -> dict[str, Any]:
    """Main entry point for detail page enrichment.

    Fetches pending jobs from the database (those without full_description),
    resolves relative URLs, then runs the three-tier extraction cascade on
    each detail page.

    Args:
        limit: Maximum number of jobs per site to process.
        workers: Number of parallel threads for site batch processing. Default 1 (sequential).

    Returns:
        Dict with stats: processed, ok, partial, error, tiers.
    """
    conn = init_db()

    # URL resolution first
    url_stats = resolve_all_urls(conn)
    log.info("URL resolution: %d resolved, %d absolute, %d failed",
             url_stats["resolved"], url_stats["already_absolute"], url_stats["failed"])

    # WTTJ special handling
    wttj_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE site = 'WelcomeToTheJungle'"
    ).fetchone()[0]
    if wttj_count > 0:
        sample = conn.execute(
            "SELECT url FROM jobs WHERE site = 'WelcomeToTheJungle' LIMIT 1"
        ).fetchone()
        if sample and not sample[0].startswith("http"):
            updated = resolve_wttj_urls(conn)
            log.info("WTTJ: %d URLs updated", updated)

    # Run the detail scraper
    stats = _run_detail_scraper(conn, max_per_site=limit, workers=workers)

    return stats
