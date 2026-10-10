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
