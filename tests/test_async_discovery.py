"""Async discovery integration tests with mocked transport boundaries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from applypilot import config, database
from applypilot.discovery.ats_direct import run_ats_direct_discovery_async
from applypilot.discovery.workday import (
    _fetch_details_async,
    _scrape_employers_with_client,
    workday_detail_async,
    workday_search_async,
)
from applypilot.network import build_async_client


@pytest.mark.asyncio
async def test_async_ats_discovery_keeps_sync_mock_getter_compatible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile("async-tests")
    config.ensure_dirs()
    monkeypatch.setattr(config, "load_search_config", lambda: {})
    monkeypatch.setattr(
        config,
        "load_ats_employers",
        lambda: {
            "greenhouse": [
                {"name": f"Example {index}", "token": f"example-{index}"}
                for index in range(18)
            ]
        },
    )

    def fake_get(url: str) -> httpx.Response:
        assert "boards-api.greenhouse.io" in url
        token = url.rsplit("/boards/", 1)[1].split("/", 1)[0]
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "title": "Senior Python Engineer",
                        "absolute_url": f"https://boards.greenhouse.io/{token}/jobs/42",
                        "location": {"name": "London, United Kingdom"},
                        "content": "Build robust Python services with async HTTP clients and SQLite storage. " * 5,
                    }
                ]
            },
        )

    try:
        summary = await run_ats_direct_discovery_async(
            workers=2,
            platforms=["greenhouse"],
            http_get=fake_get,
        )
        assert summary["boards_checked"] == 18
        assert summary["new_jobs"] == 18
        assert database.init_db().execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 18
    finally:
        database.close_connection()
        config.set_active_profile(None)


@pytest.mark.asyncio
async def test_workday_detail_fanout_keeps_only_a_small_worker_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    peak = 0

    async def fake_fetch(
        client: httpx.AsyncClient,
        employer: dict[str, Any],
        job: dict[str, Any],
    ) -> dict[str, Any]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.005)
        active -= 1
        return job

    monkeypatch.setattr("applypilot.discovery.workday._fetch_one_detail_async", fake_fetch)
    jobs = [{"external_path": f"/job/{index}"} for index in range(40)]
    employer: dict[str, Any] = {}

    async with build_async_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request))) as client:
        fetched = await _fetch_details_async(client, employer, jobs, asyncio.Semaphore(12))

    assert peak == 4
    assert [job["external_path"] for job in fetched] == [job["external_path"] for job in jobs]


@pytest.mark.asyncio
async def test_workday_async_search_and_detail_share_client() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(200, json={"total": 0, "jobPostings": []}, request=request)
        return httpx.Response(200, json={"jobPostingInfo": {"jobDescription": "Full role description"}}, request=request)

    employer = {
        "base_url": "https://example.wd5.myworkdayjobs.com",
        "tenant": "Example",
        "site_id": "Careers",
    }
    async with build_async_client(transport=httpx.MockTransport(handler)) as client:
        search = await workday_search_async(client, employer, "engineer")
        detail = await workday_detail_async(client, employer, "/job/42")

    assert search["total"] == 0
    assert detail["jobPostingInfo"]["jobDescription"] == "Full role description"
    assert requests == [
        ("POST", "/wday/cxs/Example/Careers/jobs"),
        ("GET", "/wday/cxs/Example/Careers/job/42"),
    ]


@pytest.mark.asyncio
async def test_workday_employer_task_fanout_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_counts: list[int] = []

    async def fake_pages(
        _client: httpx.AsyncClient,
        employer_key: str,
        _employer: dict[str, Any],
        _search_text: str,
        _location_filter: bool,
        _max_results: int,
        _accept_locs: list[str],
        _reject_locs: list[str],
    ) -> AsyncIterator[list[dict[str, Any]]]:
        task_counts.append(len(asyncio.all_tasks()))
        await asyncio.sleep(0)
        yield [{"external_path": f"/job/{employer_key}"}]

    async def fake_details(
        _client: httpx.AsyncClient,
        _employer: dict[str, Any],
        jobs: list[dict[str, Any]],
        _semaphore: asyncio.Semaphore,
    ) -> list[dict[str, Any]]:
        return jobs

    def fake_store(
        _employer: dict[str, Any],
        jobs: list[dict[str, Any]],
        _employers: dict[str, Any],
    ) -> tuple[int, int]:
        return len(jobs), 0

    monkeypatch.setattr("applypilot.discovery.workday._iter_search_employer_pages", fake_pages)
    monkeypatch.setattr("applypilot.discovery.workday._fetch_details_async", fake_details)
    monkeypatch.setattr("applypilot.discovery.workday._store_workday_employer_results", fake_store)
    employers = {f"employer-{index}": {"name": str(index)} for index in range(20)}

    async with build_async_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request))
    ) as client:
        summary = await _scrape_employers_with_client(
            client,
            "engineer",
            employers,
            list(employers),
            False,
            0,
            [],
            [],
            2,
        )

    assert summary == {"found": 20, "new": 20, "existing": 0, "errors": 0}
    assert task_counts
    assert max(task_counts) <= 10


@pytest.mark.asyncio
async def test_workday_paginates_details_and_persists_one_page_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    offsets: list[int] = []
    batch_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.content)
            offset = int(body["offset"])
            offsets.append(offset)
            postings = [
                {
                    "title": f"Engineer {index}",
                    "locationsText": "Remote",
                    "externalPath": f"/job/{index}",
                }
                for index in range(offset, min(offset + 20, 45))
            ]
            return httpx.Response(200, json={"total": 45, "jobPostings": postings}, request=request)
        return httpx.Response(
            200,
            json={"jobPostingInfo": {"jobDescription": "Build reliable async services."}},
            request=request,
        )

    def fake_store(
        _employer: dict[str, Any],
        jobs: list[dict[str, Any]],
        _employers: dict[str, Any],
    ) -> tuple[int, int]:
        batch_sizes.append(len(jobs))
        return len(jobs), 0

    monkeypatch.setattr("applypilot.discovery.workday._store_workday_employer_results", fake_store)
    employers = {
        "example": {
            "name": "Example",
            "base_url": "https://example.wd5.myworkdayjobs.com",
            "tenant": "Example",
            "site_id": "Careers",
        }
    }
    async with build_async_client(transport=httpx.MockTransport(handler)) as client:
        unlimited = await _scrape_employers_with_client(
            client, "engineer", employers, ["example"], False, 0, [], [], 1
        )
        assert unlimited["found"] == 45
        assert batch_sizes == [20, 20, 5]

        offsets.clear()
        batch_sizes.clear()
        limited = await _scrape_employers_with_client(
            client, "engineer", employers, ["example"], False, 23, [], [], 1
        )

    assert limited["found"] == 23
    assert batch_sizes == [20, 3]
    assert offsets == [0, 20]
