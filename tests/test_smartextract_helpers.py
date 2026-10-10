"""Smart-extract parsing, cleaning, and orchestration helpers."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

import applypilot.config as config
import applypilot.discovery.smartextract as smartextract
from applypilot.schemas import ApiRelevanceResponse
from applypilot.discovery.smartextract import (
    _bounded_worker_count,
    build_scrape_targets,
    clean_card_html,
    clean_page_html,
    execute_api_response,
    execute_json_ld,
    extract_json,
    format_strategy_briefing,
    judge_api_responses,
    load_sites,
    resolve_json_path,
    resolve_json_path_raw,
)


@pytest.fixture()
def smartextract_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile("smartextract-tests")
    config.ensure_dirs()
    yield tmp_path
    config.set_active_profile(None)


# -- Worker bounds ------------------------------------------------------------


def test_bounded_worker_count_clamps_to_a_safe_range() -> None:
    assert _bounded_worker_count(0) == 1
    assert _bounded_worker_count(-5) == 1
    assert _bounded_worker_count(4) == 4
    assert _bounded_worker_count(100) == smartextract._MAX_SCRAPE_WORKERS


# -- Site configuration -------------------------------------------------------


def test_load_sites_validates_yaml_shapes(
    smartextract_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # load_sites reads the module-level CONFIG_DIR binding imported from
    # applypilot.config; redirect it to a scratch directory so the bundled,
    # repo-tracked sites.yaml is never touched by the test.
    scratch_config_dir = smartextract_env / "scratch-config"
    scratch_config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(smartextract, "CONFIG_DIR", scratch_config_dir)
    sites_file = scratch_config_dir / "sites.yaml"
    sites_file.write_text(
        yaml.safe_dump(
            {
                "sites": [
                    {"name": "Example Board", "url": "https://example.test/jobs", "type": "static"},
                    {"name": "Example Search", "url": "https://search.test/?q={query_encoded}", "type": "search"},
                    "not-a-mapping",
                ]
            }
        ),
        encoding="utf-8",
    )
    sites = load_sites()
    assert len(sites) == 2
    assert sites[0]["name"] == "Example Board"

    sites_file.write_text("- just\n- a\n- list\n", encoding="utf-8")
    assert load_sites() == []

    sites_file.write_text("scalar: true\n", encoding="utf-8")
    assert load_sites() == []

    sites_file.unlink()
    assert load_sites() == []


def test_build_scrape_targets_expands_search_sites_per_query() -> None:
    sites = [
        {"name": "SearchBoard", "url": "https://search.test/jobs?q={query_encoded}&loc={location_encoded}", "type": "search"},
        {"name": "StaticBoard", "url": "https://static.test/jobs", "type": "static"},
    ]
    search_cfg = {
        "queries": [{"query": "AI Engineer"}, {"query": "Audit Manager"}],
        "locations": [{"location": "London, UK"}],
    }

    targets = build_scrape_targets(sites=sites, search_cfg=search_cfg)

    assert len(targets) == 3  # 2 queries x search site + 1 static
    search_targets = [t for t in targets if t["name"] == "SearchBoard"]
    assert [t["query"] for t in search_targets] == ["AI Engineer", "Audit Manager"]
    assert search_targets[0]["url"] == "https://search.test/jobs?q=AI+Engineer&loc=London%2C+UK"
    static = next(t for t in targets if t["name"] == "StaticBoard")
    assert static["url"] == "https://static.test/jobs"
    assert static["query"] is None

    # A search site with no configured queries degrades to a single static scrape.
    empty = build_scrape_targets(sites=sites, search_cfg={"queries": [], "locations": []})
    assert len(empty) == 2
    assert all(t["query"] is None for t in empty)


# -- HTML cleaning ------------------------------------------------------------


def test_clean_card_html_keeps_semantic_attributes_and_drops_utility_classes() -> None:
    html = (
        '<div class="mb-4 rounded-lg css-1x2y3z" data-testid="job-card" '
        'style="color:red" onclick="evil()" id="card-1">'
        '<a class="text-blue font-bold" href="/jobs/1" aria-label="Role">Engineer</a></div>'
    )
    cleaned = clean_card_html(html)

    assert 'data-testid="job-card"' in cleaned
    assert 'href="/jobs/1"' in cleaned
    assert 'aria-label="Role"' in cleaned
    assert 'id="card-1"' in cleaned
    assert "style" not in cleaned
    assert "onclick" not in cleaned
    assert "css-1x2y3" not in cleaned
    assert "mb-4" not in cleaned
    assert "rounded-lg" not in cleaned


def test_clean_page_html_reduces_to_main_content_and_truncates() -> None:
    html = f"""
    <html><head><title>t</title><style>.x {{ color: red }}</style></head>
    <body>
      <nav class="site-nav"><a>skip</a></nav>
      <main role="main"><article data-testid="card">{ "<p>Senior Audit Manager role</p>" * 400 }</article></main>
      <footer>foot</footer>
      <script>window.x = 1;</script>
    </body></html>
    """
    cleaned = clean_page_html(html, max_chars=5_000)

    assert "site-nav" not in cleaned
    assert "window.x" not in cleaned
    assert "foot" not in cleaned
    assert "Senior Audit Manager role" in cleaned
    assert len(cleaned) <= 5_000 + len("\n<!-- TRUNCATED -->")
    assert cleaned.endswith("<!-- TRUNCATED -->")


# -- JSON helpers -------------------------------------------------------------


def test_extract_json_requires_an_object() -> None:
    assert extract_json('{"job_card": "article"}') == {"job_card": "article"}
    with pytest.raises(ValueError, match="Expected a JSON object"):
        extract_json('["array"]')


def test_resolve_json_path_variants() -> None:
    data: dict[str, Any] = {
        "jobs": [
            {"title": "Engineer", "org": {"name": "Acme"}, "locs": [{"name": "London"}, {"name": "Leeds"}]},
            {"title": "Auditor", "org": {"text": "PwC"}},
        ],
        "count": 2,
    }

    assert resolve_json_path_raw(data, "jobs[0].org") == {"name": "Acme"}
    assert resolve_json_path_raw(data, "jobs[1].title") == "Auditor"
    assert resolve_json_path_raw(data, "missing.path") is None
    assert resolve_json_path_raw(data, "jobs[99].title") is None
    assert resolve_json_path_raw(data, "") is None

    assert resolve_json_path(data, "jobs[0].org") == "Acme"  # dict -> name
    assert resolve_json_path(data, "jobs[1].org") == "PwC"  # dict -> text
    assert resolve_json_path(data, "jobs[0].locs") == "London, Leeds"  # list of dicts
    assert resolve_json_path(data, "count") == "2"  # int coercion
    assert resolve_json_path(data, "nope") is None


# -- Extraction executors -----------------------------------------------------


def test_execute_json_ld_maps_only_jobposting_entries() -> None:
    intel = {
        "json_ld": [
            {
                "@type": "JobPosting",
                "title": "Audit Manager",
                "jobLocation": {"address": {"addressLocality": "Leeds"}},
                "baseSalary": {"value": {"minValue": 60000}},
                "description": "Statutory audit.",
                "url": "https://example.test/1",
            },
            {"@type": "Organization", "name": "Not a job"},
            "just a string",
        ]
    }
    plan = {
        "extraction": {
            "title": "title",
            "location": "jobLocation.address.addressLocality",
            "salary": "baseSalary.value.minValue",
            "description": "description",
            "url": "url",
        }
    }

    jobs = execute_json_ld(intel, plan)

    assert len(jobs) == 1
    assert jobs[0]["title"] == "Audit Manager"
    assert jobs[0]["location"] == "Leeds"
    assert jobs[0]["salary"] == "60000"
    assert jobs[0]["url"] == "https://example.test/1"

    # Untouched fields fall back to null instead of raising.
    sparse = execute_json_ld({"json_ld": [{"@type": "JobPosting"}]}, plan)
    assert sparse == [{"title": None, "salary": None, "description": None, "location": None, "url": None}]


def test_execute_api_response_resolves_items_and_fields() -> None:
    intel = {
        "api_responses": [
            {"url": "https://api.example.test/assets", "_raw_data": {"ignored": True}},
            {
                "url": "https://api.example.test/jobs/search",
                "_raw_data": {
                    "results": {
                        "items": [
                            {"position": {"title": "Engineer"}, "company": {"name": "Acme"}, "link": "/j/1"},
                            {"position": {"title": "Auditor"}, "company": {"name": "PwC"}, "link": "/j/2"},
                            "not-a-dict",
                        ]
                    }
                },
            },
        ]
    }
    plan = {
        "extraction": {
            "url_pattern": "api.example.test/jobs/search",
            "items_path": "results.items",
            "title": "position.title",
            "location": None,
            "url": "link",
        }
    }

    jobs = execute_api_response(intel, plan)

    assert len(jobs) == 2
    assert jobs[0] == {"title": "Engineer", "salary": None, "description": None, "location": None, "url": "/j/1"}

    # A path that resolves to a non-list yields no jobs, not a crash.
    bad = execute_api_response(
        intel, {"extraction": {"url_pattern": "jobs/search", "items_path": "results", "title": "position.title"}}
    )
    assert bad == []

    missing = execute_api_response(
        intel, {"extraction": {"url_pattern": "nothing-matches", "items_path": "results.items"}}
    )
    assert missing == []


# -- Strategy briefing --------------------------------------------------------


def test_format_strategy_briefing_reports_all_intelligence_signals() -> None:
    intel = {
        "url": "https://example.test/jobs",
        "page_title": "Example Jobs",
        "json_ld": [
            {"@type": "JobPosting", "title": "Audit Manager"},
            {"@type": "BreadcrumbList", "name": "breadcrumbs"},
        ],
        "api_responses": [
            {
                "url": "https://api.example.test/jobs",
                "status": 200,
                "size": 12_345,
                "type": "json",
                "first_item_keys": ["title", "company"],
                "first_item_sample": {"title": "Engineer"},
            }
        ],
        "dom_stats": {"total_elements": 900, "links": 120, "headings": 30, "tables": 0, "articles": 25, "has_data_ids": 5},
        "data_testids": [
            {"testid": "job-card", "tag": "article", "text": "job card element"},
            {"testid": "apply-btn", "tag": "button", "text": "Apply now"},
        ],
        "card_candidates": [
            {
                "parent_selector": "main > ul",
                "child_selector": "li",
                "total_children": 25,
                "with_text": 25,
                "with_links": 25,
            }
        ],
    }

    briefing = format_strategy_briefing(intel)

    assert "PAGE: https://example.test/jobs" in briefing
    assert "TITLE: Example Jobs" in briefing
    assert "1 JobPosting entries found (usable!)" in briefing
    assert "Other JSON-LD types (NOT job data): ['BreadcrumbList']" in briefing
    assert "API RESPONSES INTERCEPTED: 1 calls" in briefing
    assert "Item keys: ['title', 'company']" in briefing
    assert "DOM STATS: 900 elements, 120 links" in briefing
    assert "REPEATING ELEMENTS DETECTED: 1 candidate groups" in briefing
    assert "parent=main > ul child=li count=25" in briefing

    empty = format_strategy_briefing(
        {
            "url": "https://x.test",
            "page_title": "Empty",
            "json_ld": [],
            "api_responses": [],
            "dom_stats": {},
            "data_testids": [],
            "card_candidates": [],
        }
    )
    assert "JSON-LD: none" in empty
    assert "JSON-LD: NO JobPosting entries" not in empty
    assert "REPEATING ELEMENTS: none detected" in empty


# -- LLM judge ----------------------------------------------------------------


class _ScriptedJudgeClient:
    def __init__(self, verdicts: list[ApiRelevanceResponse | None]) -> None:
        self._verdicts = list(verdicts)
        self.prompts: list[str] = []

    def ask_structured(
        self,
        prompt: str,
        response_model: type[ApiRelevanceResponse],
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> ApiRelevanceResponse:
        assert response_model is ApiRelevanceResponse
        self.prompts.append(prompt)
        verdict = self._verdicts.pop(0)
        if verdict is None:
            raise RuntimeError("LLM unavailable")
        return verdict


def test_judge_api_responses_keeps_relevant_and_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        {"url": "https://api.example.test/jobs", "status": 200, "size": 500, "type": "json",
         "first_item_keys": ["title"], "first_item_sample": {"title": "Engineer"}},
        {"url": "https://api.example.test/analytics", "status": 200, "size": 50, "type": "json",
         "keys": ["event", "ts"]},
        {"url": "https://api.example.test/broken", "status": 200, "size": 10, "type": "unknown"},
    ]
    client = _ScriptedJudgeClient([
        ApiRelevanceResponse(relevant=True, reason="job listings"),
        ApiRelevanceResponse(relevant=False, reason="analytics telemetry"),
        None,  # judge raises -> fail open and keep
    ])
    monkeypatch.setattr(smartextract, "get_client", lambda: client)

    kept = judge_api_responses(list(responses))

    assert [r["url"] for r in kept] == [
        "https://api.example.test/jobs",
        "https://api.example.test/broken",
    ]
    assert len(client.prompts) == 3
    assert "api.example.test/analytics" in client.prompts[1]
    assert "KEEP" not in client.prompts  # prompts carry the evidence, not the verdicts

    assert judge_api_responses([]) == []


# -- Orchestration ------------------------------------------------------------


def test_run_all_sequential_persists_and_trims_summaries(
    smartextract_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import applypilot.database as database

    stored: list[tuple[str, list[dict[str, Any]]]] = []

    def fake_run_one_site(name: str, url: str) -> dict[str, Any]:
        return {
            "name": name,
            "status": "PASS",
            "strategy": "json_ld",
            "total": 1,
            "titles": 1,
            "jobs": [{"url": url, "title": f"Role at {name}"}],
            "sample": ["discarded"],
        }

    def fake_store(
        _conn: Any,
        jobs: list[dict[str, Any]],
        site: str,
        strategy: str,
        _accept: list[str],
        _reject: list[str],
    ) -> tuple[int, int]:
        stored.append((site, jobs))
        assert strategy == "json_ld"
        return len(jobs), 0

    monkeypatch.setattr(smartextract, "_run_one_site", fake_run_one_site)
    monkeypatch.setattr(smartextract, "_store_jobs_filtered", fake_store)

    targets = [
        {"name": "board-a", "url": "https://a.example.test/jobs", "query": "engineer"},
        {"name": "board-b", "url": "https://b.example.test/jobs"},
    ]
    summary = smartextract._run_all(targets, [], [], workers=1)

    assert summary == {"total_new": 2, "total_existing": 0, "passed": 2, "total": 2}
    assert [site for site, _ in stored] == ["board-a", "board-b"]
    database.close_connection()


def test_run_all_crashed_workers_are_reported_not_raised(
    smartextract_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def flaky_run_one_site(name: str, url: str) -> dict[str, Any]:
        if name == "crashes":
            raise RuntimeError("browser died")
        return {"name": name, "status": "PASS", "strategy": "css", "total": 0, "titles": 0, "jobs": []}

    monkeypatch.setattr(smartextract, "_run_one_site", flaky_run_one_site)
    monkeypatch.setattr(
        smartextract,
        "_store_jobs_filtered",
        lambda _conn, jobs, site, strategy, _a, _r: (len(jobs), 0),
    )

    targets = [
        {"name": "crashes", "url": "https://x.example.test"},
        {"name": "healthy", "url": "https://y.example.test"},
    ]
    summary = smartextract._run_all(targets, [], [], workers=2)

    assert summary["passed"] == 1
    assert summary["total"] == 2

    import applypilot.database as database

    database.close_connection()


def test_run_smart_extract_end_to_end_with_scripted_site_worker(
    smartextract_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config,
        "load_search_config",
        lambda: {"queries": [{"query": "audit"}], "locations": [{"location": "London, UK"}]},
    )

    def fake_run_one_site(name: str, url: str) -> dict[str, Any]:
        return {
            "name": name,
            "status": "PARTIAL",
            "strategy": "json_ld",
            "total": 2,
            "titles": 2,
            "jobs": [
                {"url": f"{url}/1", "title": "Audit Manager", "location": "London, UK"},
                {"url": f"{url}/2", "title": "Auditor", "location": "Berlin, DE"},
            ],
        }

    monkeypatch.setattr(smartextract, "_run_one_site", fake_run_one_site)

    sites = [{"name": "ExampleSearch", "url": "https://example.test/jobs?q={query_encoded}", "type": "search"}]
    summary = smartextract.run_smart_extract(sites=sites, workers=1)

    # One target expanded from the single configured query. The London job is
    # stored; the Berlin job is filtered out by the location accept list that
    # falls back to the configured search locations.
    assert summary["total"] == 1
    assert summary["total_new"] == 1
    assert summary["passed"] == 0  # PARTIAL is not PASS

    import applypilot.database as database

    conn = database.get_connection()
    urls = {row["url"] for row in conn.execute("SELECT url FROM jobs")}
    assert urls == {"https://example.test/jobs?q=audit/1"}
    database.close_connection()


def test_run_smart_extract_without_targets_is_a_noop(
    smartextract_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "load_search_config", lambda: {"queries": [], "locations": []})
    monkeypatch.setattr(smartextract, "load_sites", lambda: [])

    summary = smartextract.run_smart_extract(workers=1)

    assert summary == {"total_new": 0, "total_existing": 0, "passed": 0, "total": 0}
