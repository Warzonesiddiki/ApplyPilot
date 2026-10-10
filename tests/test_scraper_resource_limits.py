"""Memory-boundary tests for browser collection and batch processing helpers."""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import Page

import applypilot.discovery.smartextract as smartextract
from applypilot.discovery.smartextract import _capture_bounded_page_html


def test_capture_bounded_page_html_transfers_only_a_configured_prefix() -> None:
    page = MagicMock(spec=Page)
    page.evaluate.return_value = {"html": "<html>prefix</html>", "truncated": True}

    html, truncated = _capture_bounded_page_html(page, max_chars=128)

    assert html == "<html>prefix</html>"
    assert truncated
    page.evaluate.assert_called_once()
    script, max_chars = page.evaluate.call_args.args
    assert "html.slice(0, end)" in script
    assert "0xD800" in script
    assert max_chars == 128


def test_capture_bounded_page_html_rejects_invalid_limits_and_browser_payloads() -> None:
    page = MagicMock(spec=Page)
    with pytest.raises(ValueError, match="positive"):
        _capture_bounded_page_html(page, max_chars=0)
    page.evaluate.assert_not_called()

    page.evaluate.return_value = ["not", "an", "object"]
    with pytest.raises(TypeError, match="invalid bounded HTML data"):
        _capture_bounded_page_html(page)

    page.evaluate.return_value = {"html": "not-a-flag", "truncated": "yes"}
    with pytest.raises(TypeError, match="invalid bounded HTML fields"):
        _capture_bounded_page_html(page)


def test_parallel_smart_extract_limits_workers_and_discards_job_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    peak = 0
    active_lock = threading.Lock()
    stored: list[tuple[str, int]] = []

    def fake_run_one_site(name: str, url: str) -> dict[str, Any]:
        nonlocal active, peak
        with active_lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.005)
            return {
                "name": name,
                "status": "PASS",
                "strategy": "json_ld",
                "total": 1,
                "titles": 1,
                "jobs": [{"url": url, "title": name, "description": "large content" * 100}],
                "sample": [{"raw_html": "discard after persistence" * 100}],
            }
        finally:
            with active_lock:
                active -= 1

    def fake_store_jobs(
        _conn: Any,
        jobs: list[dict[str, Any]],
        site: str,
        strategy: str,
        _accept: list[str],
        _reject: list[str],
    ) -> tuple[int, int]:
        stored.append((site, len(jobs)))
        assert strategy == "json_ld"
        return len(jobs), 0

    monkeypatch.setattr(smartextract, "init_db", lambda: object())
    monkeypatch.setattr(smartextract, "get_stats", lambda _conn: {"total": 0, "pending_detail": 0})
    monkeypatch.setattr(smartextract, "_run_one_site", fake_run_one_site)
    monkeypatch.setattr(smartextract, "_store_jobs_filtered", fake_store_jobs)

    targets = [{"name": f"site-{index}", "url": f"https://site-{index}.example.test/jobs"} for index in range(12)]
    summary = smartextract._run_all(targets, [], [], workers=1_000)

    assert summary == {"total_new": 12, "total_existing": 0, "passed": 12, "total": 12}
    assert peak == smartextract._MAX_SCRAPE_WORKERS
    assert len(stored) == 12
    assert all(count == 1 for _, count in stored)
