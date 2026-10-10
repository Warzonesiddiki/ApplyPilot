"""Bounded database batching tests for detail enrichment."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any, cast

import pytest
from playwright.sync_api import Page

import applypilot.database as database
import applypilot.enrichment.detail as detail


class _FakeJsonLdLocator:
    def __init__(self, texts: list[str], index: int | None = None) -> None:
        self._texts = texts
        self._index = index

    def count(self) -> int:
        return len(self._texts)

    def nth(self, index: int) -> _FakeJsonLdLocator:
        return _FakeJsonLdLocator(self._texts, index)

    def evaluate(self, _expression: str, limit: int) -> str:
        assert self._index is not None
        return self._texts[self._index][: limit + 1]


class _FakeDetailPage:
    def __init__(self, texts: list[str]) -> None:
        self._texts = texts
        self.url = "https://example.test/job"

    def title(self) -> str:
        return "Engineer"

    def locator(self, _selector: str) -> _FakeJsonLdLocator:
        return _FakeJsonLdLocator(self._texts)


def test_detail_intelligence_caps_json_ld_documents_and_total_bytes() -> None:
    payloads = [json.dumps({"index": index}) for index in range(20)]
    intel = detail.collect_detail_intelligence(cast(Page, _FakeDetailPage(payloads)))

    assert intel["page_title"] == "Engineer"
    assert intel["final_url"] == "https://example.test/job"
    assert len(intel["json_ld"]) == detail._DETAIL_JSON_LD_MAX_DOCUMENTS

    large_payload = json.dumps({"description": "x" * (detail._DETAIL_JSON_LD_MAX_DOCUMENT_CHARS - 100)})
    limited_payloads = [large_payload] * 5 + [json.dumps({"kept": True})]
    bounded_intel = detail.collect_detail_intelligence(cast(Page, _FakeDetailPage(limited_payloads)))
    assert len(bounded_intel["json_ld"]) == 4
    assert sum(len(json.dumps(item)) for item in bounded_intel["json_ld"]) <= (
        detail._DETAIL_JSON_LD_MAX_TOTAL_CHARS
    )


def test_detail_scraper_streams_per_site_batches_and_honors_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = database.init_db(tmp_path / "detail.sqlite3")
    conn.executemany(
        "INSERT INTO jobs (url, title, company, site, discovered_at) VALUES (?, ?, ?, ?, ?)",
        [
            (
                f"https://example.test/{index:04d}",
                f"Engineer {index}",
                "Example Corp",
                "Example",
                "2026-10-10T00:00:00Z",
            )
            for index in range(1_200)
        ]
        + [
            (
                f"https://other.test/{index:04d}",
                f"Analyst {index}",
                "Other Corp",
                "Other",
                "2026-10-10T00:00:00Z",
            )
            for index in range(20)
        ],
    )
    conn.commit()

    batches: list[tuple[str, int]] = []

    def fake_scrape_batch(
        _conn: sqlite3.Connection,
        site: str,
        jobs: list[tuple[str, str]],
        delay: float,
    ) -> dict[str, Any]:
        assert delay == 0
        batches.append((site, len(jobs)))
        return {
            "processed": len(jobs),
            "ok": len(jobs),
            "partial": 0,
            "error": 0,
            "tiers": {1: len(jobs), 2: 0, 3: 0},
        }

    monkeypatch.setattr(detail, "scrape_site_batch", fake_scrape_batch)
    monkeypatch.setitem(detail.SITE_DELAYS, "Example", 0)

    summary = detail._run_detail_scraper(conn, sites=["Example"], max_per_site=600, workers=1)

    assert batches == [("Example", 500), ("Example", 100)]
    assert summary["processed"] == 600
    assert summary["ok"] == 600
    assert summary["tiers"] == {1: 600, 2: 0, 3: 0}
    database.close_connection(tmp_path / "detail.sqlite3")


def test_detail_scraper_includes_jobs_with_a_null_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "null-site.sqlite3"
    conn = database.init_db(db_path)
    conn.execute(
        "INSERT INTO jobs (url, title, company, site, discovered_at) VALUES (?, ?, ?, ?, ?)",
        ("https://unknown.test/job", "Engineer", "Unknown Corp", None, "2026-10-10"),
    )
    conn.commit()
    seen_sites: list[str] = []

    def fake_scrape_batch(
        _conn: sqlite3.Connection,
        site: str,
        jobs: list[tuple[str, str]],
        delay: float,
    ) -> dict[str, Any]:
        seen_sites.append(site)
        return {
            "processed": len(jobs),
            "ok": len(jobs),
            "partial": 0,
            "error": 0,
            "tiers": {1: len(jobs), 2: 0, 3: 0},
        }

    monkeypatch.setattr(detail, "scrape_site_batch", fake_scrape_batch)

    summary = detail._run_detail_scraper(conn, workers=1)

    assert seen_sites == [""]
    assert summary["processed"] == 1
    database.close_connection(db_path)


def test_detail_scraper_uses_worker_connections_for_the_source_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "parallel-detail.sqlite3"
    conn = database.init_db(db_path)
    conn.executemany(
        "INSERT INTO jobs (url, title, company, site, discovered_at) VALUES (?, ?, ?, ?, ?)",
        [
            (
                f"https://site-{index}.test/job",
                f"Role {index}",
                f"Company {index}",
                f"Site-{index}",
                "2026-10-10",
            )
            for index in range(10)
        ],
    )
    conn.commit()

    worker_connection_ids: list[int] = []
    worker_db_paths: list[str] = []
    result_lock = Lock()

    def fake_scrape_batch(
        worker_conn: sqlite3.Connection,
        _site: str,
        jobs: list[tuple[str, str]],
        delay: float,
    ) -> dict[str, Any]:
        with result_lock:
            worker_connection_ids.append(id(worker_conn))
            database_row = worker_conn.execute("PRAGMA database_list").fetchone()
            worker_db_paths.append(str(database_row[2]))
        return {
            "processed": len(jobs),
            "ok": len(jobs),
            "partial": 0,
            "error": 0,
            "tiers": {1: len(jobs), 2: 0, 3: 0},
        }

    created_worker_counts: list[int] = []
    real_executor = ThreadPoolExecutor

    def record_executor(max_workers: int) -> ThreadPoolExecutor:
        created_worker_counts.append(max_workers)
        return real_executor(max_workers=max_workers)

    monkeypatch.setattr(detail, "ThreadPoolExecutor", record_executor)
    monkeypatch.setattr(detail, "scrape_site_batch", fake_scrape_batch)
    for index in range(10):
        monkeypatch.setitem(detail.SITE_DELAYS, f"Site-{index}", 0)

    summary = detail._run_detail_scraper(conn, workers=1_000_000)

    assert summary["processed"] == 10
    assert created_worker_counts == [detail._MAX_DETAIL_SITE_WORKERS]
    assert worker_connection_ids
    assert id(conn) not in worker_connection_ids
    assert set(worker_db_paths) == {str(db_path.resolve())}
    database.close_connection(db_path)


def test_resolve_all_urls_processes_large_tables_in_bounded_rowid_batches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = database.init_db(tmp_path / "resolve.sqlite3")
    conn.executemany(
        "INSERT INTO jobs (url, title, company, site, application_url, discovered_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                "https://existing.test/job" if index == 0 else f"jobs/{index}",
                f"Engineer {index}",
                "Example Corp",
                "Example",
                f"apply/{index}" if index < 900 else None,
                "2026-10-10",
            )
            for index in range(1_000)
        ]
        + [
            (
                f"invalid/{index}",
                f"Analyst {index}",
                "Example Corp",
                "Example",
                None,
                "2026-10-10",
            )
            for index in range(100)
        ],
    )
    conn.commit()

    def fake_resolve(raw_url: str, site: str) -> str | None:
        if raw_url.startswith(("jobs/", "apply/")):
            return f"https://{site.lower()}.example.test/{raw_url}"
        return None

    monkeypatch.setattr(detail, "resolve_url", fake_resolve)

    stats = detail.resolve_all_urls(conn)

    assert stats == {"resolved": 999, "failed": 100, "already_absolute": 1, "app_resolved": 900}
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE url LIKE 'jobs/%'").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE application_url LIKE 'apply/%'"
    ).fetchone()[0] == 0
    database.close_connection(tmp_path / "resolve.sqlite3")


def test_algolia_capture_requires_bounded_declared_content_length() -> None:
    """Unknown-size (chunked) or oversized Algolia responses are skipped before materialization."""
    cap = detail._WTTJ_RESPONSE_MAX_CHARS
    assert detail._algolia_capture_within_limit({"Content-Length": "12345"})
    assert detail._algolia_capture_within_limit({"content-length": str(cap)})
    assert not detail._algolia_capture_within_limit({"Content-Length": str(cap + 1)})
    assert not detail._algolia_capture_within_limit({})  # chunked / unknown size
    assert not detail._algolia_capture_within_limit({"Content-Length": "not-a-number"})
    assert not detail._algolia_capture_within_limit({"Content-Length": "-5"})
