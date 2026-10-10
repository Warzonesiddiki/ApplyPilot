"""JobSpy wrapper unit tests with a scripted scraper and duck-typed DataFrame."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import applypilot.config as config
import applypilot.database as database
import applypilot.discovery.jobspy as jobspy


class _FakeDataFrame:
    """Minimal pandas.DataFrame stand-in exposing iterrows()."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def iterrows(self) -> Iterator[tuple[int, dict[str, Any]]]:
        yield from enumerate(self._rows)


@pytest.fixture()
def jobspy_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile("jobspy-tests")
    config.ensure_dirs()
    database.init_db()
    yield None
    database.close_connection()
    config.set_active_profile(None)


def test_parse_proxy_supports_credentialed_and_bare_forms() -> None:
    full = jobspy.parse_proxy("proxy.example.test:8080:user:secret")
    assert full["host"] == "proxy.example.test"
    assert full["port"] == "8080"
    assert full["user"] == "user"
    assert full["pass"] == "secret"
    assert full["jobspy"] == "user:secret@proxy.example.test:8080"
    assert full["playwright"]["server"] == "http://proxy.example.test:8080"

    bare = jobspy.parse_proxy("proxy.example.test:3128")
    assert bare["jobspy"] == "proxy.example.test:3128"
    assert bare["playwright"]["server"] == "http://proxy.example.test:3128"


def test_scrape_with_retry_retries_transient_errors_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)

    attempts = {"count": 0}

    def flaky(**kwargs: Any) -> str:
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise ConnectionError("remote connection reset by peer")
        return "frame"

    monkeypatch.setattr(jobspy, "_get_scrape_jobs", lambda: flaky)
    assert jobspy._scrape_with_retry({}, max_retries=2, backoff=1.0) == "frame"
    assert attempts["count"] == 3
    assert sleeps == [1.0, 2.0]

    def fatal(**kwargs: Any) -> str:
        raise ValueError("invalid country code")

    monkeypatch.setattr(jobspy, "_get_scrape_jobs", lambda: fatal)
    with pytest.raises(ValueError, match="invalid country code"):
        jobspy._scrape_with_retry({}, max_retries=3, backoff=1.0)
    assert len(sleeps) == 2  # no sleep for non-transient failures


def test_store_jobspy_results_formats_rows_and_deduplicates(jobspy_env: None) -> None:
    conn = database.get_connection()
    long_description = "Senior audit role covering statutory reporting under IFRS and UK GAAP. " * 10
    rows: list[dict[str, Any]] = [
        {
            "job_url": "https://uk.indeed.com/job/1",
            "title": "Audit Manager",
            "company": "PwC UK",
            "location": "London",
            "min_amount": 60000,
            "max_amount": 80000,
            "interval": "year",
            "currency": "£",
            "description": long_description,
            "site": "indeed",
            "is_remote": True,
            "job_url_direct": "https://pwc.com/careers/1",
        },
        {
            "job_url": "https://uk.indeed.com/job/2",
            "title": "Chef",
            "company": "Canteen",
            "location": "Leeds",
            "min_amount": None,
            "max_amount": None,
            "interval": "nan",
            "currency": "nan",
            "description": "nan",
            "site": "indeed",
            "is_remote": False,
            "job_url_direct": "nan",
        },
    ]

    new, existing = jobspy.store_jobspy_results(conn, _FakeDataFrame(rows), "indeed")
    assert (new, existing) == (2, 0)

    stored = conn.execute(
        "SELECT title, salary, full_description IS NOT NULL, location, application_url, strategy "
        "FROM jobs WHERE url = 'https://uk.indeed.com/job/1'"
    ).fetchone()
    assert stored["title"] == "Audit Manager"
    assert stored["salary"] == "£60,000-£80,000/year"
    assert stored["full_description IS NOT NULL"] == 1  # long descriptions are pre-enriched
    assert stored["location"] == "London (Remote)"
    assert stored["application_url"] == "https://pwc.com/careers/1"
    assert stored["strategy"] == "jobspy"

    short_row = dict(rows[0])
    short_row["description"] = "Short"
    new2, existing2 = jobspy.store_jobspy_results(conn, _FakeDataFrame([short_row]), "indeed")
    assert (new2, existing2) == (0, 1)  # URL collision counted as existing


def test_store_jobspy_results_skips_rows_without_urls(jobspy_env: None) -> None:
    conn = database.get_connection()
    rows = [
        {"job_url": "nan", "title": "Ghost", "company": "Nowhere", "location": "N/A"},
        {"job_url": "", "title": "Empty", "company": "Nowhere", "location": "N/A"},
        {"job_url": "https://uk.indeed.com/job/3", "title": "Real", "company": "Somewhere", "location": "Bristol"},
    ]
    new, existing = jobspy.store_jobspy_results(conn, _FakeDataFrame(rows), "indeed")
    assert (new, existing) == (1, 0)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


# -- Single-search orchestration ---------------------------------------------


def _jobs_frame(rows: list[dict[str, Any]]) -> "Any":
    import pandas as pd

    columns = [
        "job_url", "title", "company", "location", "min_amount", "max_amount",
        "interval", "currency", "description", "site", "is_remote", "job_url_direct",
    ]
    normalized = [{col: row.get(col) for col in columns} for row in rows]
    return pd.DataFrame(normalized, columns=columns)


def test_run_one_search_splits_glassdoor_and_applies_location_filter(
    jobspy_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scrape_calls: list[dict[str, Any]] = []

    def fake_scrape(kwargs: dict[str, Any], max_retries: int = 2, backoff: float = 5.0) -> Any:
        scrape_calls.append(kwargs)
        if "glassdoor" in kwargs["site_name"]:
            return _jobs_frame(
                [{"job_url": "https://glassdoor.test/gd-1", "title": "Auditor", "company": "PwC",
                  "location": "Amsterdam, NL", "site": "glassdoor"}]
            )
        return _jobs_frame(
            [
                {"job_url": "https://indeed.test/in-1", "title": "Audit Manager", "company": "ING",
                 "location": "Amsterdam, NL", "site": "indeed"},
                {"job_url": "https://indeed.test/in-2", "title": "Chef", "company": "Canteen",
                 "location": "Sydney, AU", "site": "indeed"},
            ]
        )

    monkeypatch.setattr(jobspy, "_scrape_with_retry", fake_scrape)

    search = {"query": "audit manager", "location": "Amsterdam, Netherlands", "remote": True, "tier": 1}
    result = jobspy._run_one_search(
        search,
        sites=["indeed", "glassdoor"],
        results_per_site=25,
        hours_old=72,
        proxy_config=None,
        defaults={},
        max_retries=1,
        accept_locs=["amsterdam"],
        reject_locs=[],
        glassdoor_map={"Amsterdam, Netherlands": "Amsterdam"},
    )

    # Glassdoor runs separately with the simplified location; other sites keep the original.
    assert len(scrape_calls) == 2
    assert scrape_calls[0]["site_name"] == ["indeed"]
    assert scrape_calls[0]["location"] == "Amsterdam, Netherlands"
    assert scrape_calls[0]["is_remote"] is True
    assert scrape_calls[0]["results_wanted"] == 25
    assert scrape_calls[1]["site_name"] == ["glassdoor"]
    assert scrape_calls[1]["location"] == "Amsterdam"

    # The Sydney row is filtered by the location accept list before storage.
    assert result["new"] == 2
    assert result["filtered"] == 1
    assert result["errors"] == 0
    assert result["total"] == 3
    assert "audit manager" in result["label"]

    import applypilot.database as database

    conn = database.get_connection()
    stored_urls = {row["url"] for row in conn.execute("SELECT url FROM jobs")}
    assert stored_urls == {"https://indeed.test/in-1", "https://glassdoor.test/gd-1"}


def test_run_one_search_reports_all_sites_failed(
    jobspy_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing(kwargs: dict[str, Any], max_retries: int = 2, backoff: float = 5.0) -> Any:
        raise RuntimeError("blocked")

    monkeypatch.setattr(jobspy, "_scrape_with_retry", failing)

    result = jobspy._run_one_search(
        {"query": "engineer", "location": "London, UK"},
        sites=["indeed"],
        results_per_site=10,
        hours_old=72,
        proxy_config=None,
        defaults={},
        max_retries=0,
        accept_locs=[],
        reject_locs=[],
        glassdoor_map={},
    )

    assert result == {
        "new": 0, "existing": 0, "errors": 1, "filtered": 0, "total": 0,
        "label": '"engineer" in London, UK [uk] ',
    }


def test_run_one_search_returns_zero_counts_for_empty_frames(
    jobspy_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jobspy, "_scrape_with_retry", lambda kwargs, max_retries=2, backoff=5.0: _jobs_frame([]))

    result = jobspy._run_one_search(
        {"query": "engineer", "location": "London, UK"},
        sites=["indeed"],
        results_per_site=10,
        hours_old=72,
        proxy_config=None,
        defaults={},
        max_retries=1,
        accept_locs=[],
        reject_locs=[],
        glassdoor_map={},
    )

    assert result["new"] == 0
    assert result["errors"] == 0
    assert result["total"] == 0


def test_run_one_search_forwards_proxy_and_linkedin_options(
    jobspy_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scrape_calls: list[dict[str, Any]] = []

    def fake_scrape(kwargs: dict[str, Any], max_retries: int = 2, backoff: float = 5.0) -> Any:
        scrape_calls.append(kwargs)
        return _jobs_frame([])

    monkeypatch.setattr(jobspy, "_scrape_with_retry", fake_scrape)

    jobspy._run_one_search(
        {"query": "engineer", "location": "London, UK"},
        sites=["indeed", "linkedin"],
        results_per_site=10,
        hours_old=72,
        proxy_config={"jobspy": "user:pass@proxy.test:8080"},
        defaults={},
        max_retries=1,
        accept_locs=[],
        reject_locs=[],
        glassdoor_map={},
    )

    kwargs = scrape_calls[0]
    assert kwargs["site_name"] == ["indeed", "linkedin"]
    assert kwargs["linkedin_fetch_description"] is True
    assert kwargs["proxies"] == ["user:pass@proxy.test:8080"]


# -- Public entry points ------------------------------------------------------


def test_search_jobs_stores_results_and_reports_counts(
    jobspy_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_scrape_jobs(**kwargs: Any) -> Any:
        assert kwargs["search_term"] == "audit manager"
        assert kwargs["location"] == "London, UK"
        assert kwargs["country_indeed"] == "uk"
        assert kwargs["is_remote"] is True
        return _jobs_frame(
            [
                {"job_url": "https://indeed.test/1", "title": "Audit Manager", "company": "PwC",
                 "location": "London, UK", "site": "indeed"},
                {"job_url": "https://indeed.test/2", "title": "Auditor", "company": "EY",
                 "location": "London, UK", "site": "indeed"},
            ]
        )

    monkeypatch.setattr(jobspy, "_get_scrape_jobs", lambda: fake_scrape_jobs)

    result = jobspy.search_jobs("audit manager", "London, UK", remote_only=True)

    assert result == {"total": 2, "new": 2, "existing": 0}


def test_search_jobs_reports_failures_as_error_payload(
    jobspy_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing(**kwargs: Any) -> Any:
        raise RuntimeError("rate limited")

    monkeypatch.setattr(jobspy, "_get_scrape_jobs", lambda: failing)

    result = jobspy.search_jobs("engineer", "Berlin, DE")

    assert result["total"] == 0
    assert result["new"] == 0
    assert "rate limited" in str(result["error"])


def test_run_discovery_crawls_all_query_location_combinations(
    jobspy_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[dict[str, Any]] = []

    def fake_scrape(kwargs: dict[str, Any], max_retries: int = 2, backoff: float = 5.0) -> Any:
        executed.append(kwargs)
        return _jobs_frame(
            [{"job_url": f"https://indeed.test/{kwargs['search_term']}-{kwargs['location']}",
              "title": kwargs["search_term"].title(), "company": "Acme",
              "location": kwargs["location"], "site": "indeed"}]
        )

    monkeypatch.setattr(jobspy, "_scrape_with_retry", fake_scrape)

    cfg = {
        "queries": [{"query": "audit manager", "tier": 1}, {"query": "ai engineer", "tier": 2}],
        "locations": [
            {"location": "London, United Kingdom", "remote": False},
            {"location": "Amsterdam, Netherlands", "remote": True},
        ],
        "defaults": {"country_indeed": "uk", "results_per_site": 5, "hours_old": 24},
        "sites": ["indeed"],
    }
    summary = jobspy.run_discovery(cfg)

    assert summary["queries"] == 4  # 2 queries x 2 locations
    assert summary["new"] == 4
    assert summary["errors"] == 0
    assert summary["db_total"] == 4
    assert len(executed) == 4
    assert {call["search_term"] for call in executed} == {"audit manager", "ai engineer"}

    import applypilot.database as database

    conn = database.get_connection()
    urls = {row["url"] for row in conn.execute("SELECT url FROM jobs")}
    assert "https://indeed.test/audit manager-London, United Kingdom" in urls


def test_run_discovery_without_config_is_a_noop(
    jobspy_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "load_search_config", lambda: {})

    summary = jobspy.run_discovery(None)

    assert summary == {"new": 0, "existing": 0, "errors": 0, "db_total": 0, "queries": 0}
