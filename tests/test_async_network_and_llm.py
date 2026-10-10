"""Async HTTP, structured LLM output, and cross-worker SQLite regressions."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest

from applypilot import database
from applypilot.browser import should_block_dom_request
from applypilot.discovery.smartextract import (
    _MAX_API_RESPONSE_CHARS,
    _api_response_body_within_limit,
    _should_capture_api_response,
)
import applypilot.llm as llm
from applypilot.llm import (
    LLMClient,
    clear_response_cache,
    response_cache_info,
)
from applypilot.network import RetryPolicy, build_async_client, parse_retry_after, request_with_retry
from applypilot.schemas import JobDescriptionResponse, ScoreResponse, StrategyResponse, TailoredResumeResponse


def _score_payload() -> dict[str, Any]:
    return {
        "score": 9,
        "skills_score": 8,
        "qualifications_score": 10,
        "domain_score": 9,
        "location_score": 8,
        "keywords": ["Python", "SQLite"],
        "missing_keywords": ["Kubernetes"],
        "reasoning": "The candidate has closely aligned delivery experience and required qualifications.",
    }


@pytest.mark.asyncio
async def test_async_retry_honors_retry_after_without_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0
    wait_calls: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0.25"}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    async def fake_sleep(seconds: float) -> None:
        wait_calls.append(seconds)

    monkeypatch.setattr("applypilot.network.asyncio.sleep", fake_sleep)
    async with build_async_client(transport=httpx.MockTransport(handler)) as client:
        response = await request_with_retry(
            client,
            "GET",
            "https://example.test/jobs",
            policy=RetryPolicy(max_attempts=2, base_delay=0.01),
        )

    assert attempts == 2
    assert response.status_code == 200
    assert wait_calls and wait_calls[0] >= 0.25
    assert parse_retry_after({"Retry-After": "1.5"}) == 1.5
    assert parse_retry_after({"retry-after": "1.5"}) == 1.5
    assert parse_retry_after({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}) == 0.0


@pytest.mark.asyncio
async def test_async_retry_keeps_the_event_loop_responsive() -> None:
    attempts = 0
    ticks = 0
    finished = asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0.05"}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    async def heartbeat() -> None:
        nonlocal ticks
        while not finished.is_set():
            ticks += 1
            await asyncio.sleep(0.001)

    ticker = asyncio.create_task(heartbeat())
    try:
        async with build_async_client(transport=httpx.MockTransport(handler)) as client:
            response = await request_with_retry(
                client,
                "GET",
                "https://example.test/heartbeat",
                policy=RetryPolicy(max_attempts=2),
            )
    finally:
        finished.set()
        await ticker

    assert response.status_code == 200
    assert attempts == 2
    assert ticks >= 5


def test_sync_structured_llm_validates_and_caches_identical_calls() -> None:
    clear_response_cache()
    calls = 0
    payload = _score_payload()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        request_data = json.loads(request.content)
        assert request_data["response_format"]["type"] == "json_schema"
        json_schema = request_data["response_format"]["json_schema"]
        assert json_schema["strict"] is True
        assert json_schema["schema"]["additionalProperties"] is False
        assert set(json_schema["schema"]["required"]) == set(payload)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload)}}]},
            request=request,
        )

    client = LLMClient(
        "https://api.openai.com/v1",
        "gpt-4o-mini",
        "test-key",
        http_transport=httpx.MockTransport(handler),
    )
    messages = [{"role": "user", "content": "Score the candidate."}]
    try:
        result = client.structured_chat(messages, ScoreResponse, max_tokens=256)
        repeated = client.structured_chat(messages, ScoreResponse, max_tokens=256)
    finally:
        client.close()

    assert result.score == 9
    assert repeated == result
    assert calls == 1
    assert response_cache_info()["size"] == 1


def test_response_cache_is_bounded_by_entry_count_and_utf8_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_response_cache()
    monkeypatch.setattr(llm, "_CACHE_MAXSIZE", 2)
    monkeypatch.setattr(llm, "_CACHE_MAX_BYTES", 8)

    llm._cache_put("first", "1234")
    llm._cache_put("unicode", "éé")
    assert response_cache_info() == {"size": 2, "maxsize": 2, "bytes": 8, "max_bytes": 8}
    assert llm._cache_get("first") == "1234"

    llm._cache_put("oversized", "x" * 9)
    assert llm._cache_get("oversized") is None
    llm._cache_put("third", "abc")
    assert llm._cache_get("unicode") is None
    assert llm._cache_get("third") == "abc"
    llm._cache_get("first")
    llm._cache_put("fourth", "d")

    assert llm._cache_get("first") == "1234"
    assert llm._cache_get("third") is None
    assert llm._cache_get("fourth") == "d"
    assert response_cache_info()["bytes"] == 5
    clear_response_cache()


def test_openai_strict_schema_closes_nested_pydantic_models() -> None:
    clear_response_cache()
    payload: dict[str, Any] = {
        "strategy": "json_ld",
        "reasoning": "The page exposes standard JobPosting JSON-LD data.",
        "extraction": {
            "url_pattern": None,
            "items_path": None,
            "title": None,
            "salary": None,
            "description": None,
            "location": None,
            "url": None,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        request_data = json.loads(request.content)
        schema = request_data["response_format"]["json_schema"]["schema"]
        nested = schema["$defs"]["StrategyExtractionResponse"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert nested["additionalProperties"] is False
        assert set(nested["required"]) == set(nested["properties"])
        assert all("default" not in field for field in nested["properties"].values())
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload)}}]},
            request=request,
        )

    client = LLMClient(
        "https://api.openai.com/v1",
        "gpt-4o-mini",
        "test-key",
        http_transport=httpx.MockTransport(handler),
    )
    try:
        result = client.ask_structured("Choose the extraction strategy.", StrategyResponse)
    finally:
        client.close()

    assert result.strategy == "json_ld"
    assert result.extraction.url_pattern is None


def test_openai_optional_fields_are_explicit_in_strict_schema() -> None:
    clear_response_cache()
    payload = {"full_description": None, "application_url": "https://jobs.example.test/apply"}

    def handler(request: httpx.Request) -> httpx.Response:
        request_data = json.loads(request.content)
        schema = request_data["response_format"]["json_schema"]["schema"]
        assert set(schema["required"]) == {"full_description", "application_url"}
        assert all("default" not in field for field in schema["properties"].values())
        assert any(
            choice.get("type") == "null"
            for choice in schema["properties"]["full_description"]["anyOf"]
        )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload)}}]},
            request=request,
        )

    client = LLMClient(
        "https://api.openai.com/v1",
        "gpt-4o-mini",
        "test-key",
        http_transport=httpx.MockTransport(handler),
    )
    try:
        result = client.ask_structured("Extract the job details.", JobDescriptionResponse)
    finally:
        client.close()

    assert result.full_description is None
    assert result.application_url == payload["application_url"]


def test_openai_strict_schema_rejection_retries_in_json_mode() -> None:
    clear_response_cache()
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_data = json.loads(request.content)
        requests.append(request_data)
        if len(requests) == 1:
            return httpx.Response(
                400,
                json={"error": {"message": "response_format json_schema is unsupported"}},
                request=request,
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_score_payload())}}]},
            request=request,
        )

    client = LLMClient(
        "https://api.openai.com/v1",
        "gpt-4o-mini",
        "test-key",
        http_transport=httpx.MockTransport(handler),
    )
    try:
        result = client.ask_structured("Use compatible structured output.", ScoreResponse)
    finally:
        client.close()

    assert result.score == 9
    assert requests[0]["response_format"]["type"] == "json_schema"
    assert "response_format" not in requests[1]


@pytest.mark.asyncio
async def test_async_openai_strict_schema_rejection_retries_in_json_mode() -> None:
    clear_response_cache()
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_data = json.loads(request.content)
        requests.append(request_data)
        if len(requests) == 1:
            return httpx.Response(
                400,
                json={"error": {"message": "response_format json_schema is unsupported"}},
                request=request,
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_score_payload())}}]},
            request=request,
        )

    client = LLMClient(
        "https://api.openai.com/v1",
        "gpt-4o-mini",
        "test-key",
        async_transport=httpx.MockTransport(handler),
    )
    async with client:
        result = await client.aask_structured("Use compatible async structured output.", ScoreResponse)

    assert result.score == 9
    assert requests[0]["response_format"]["type"] == "json_schema"
    assert "response_format" not in requests[1]


def test_openai_arbitrary_mapping_schema_uses_json_object_mode() -> None:
    clear_response_cache()
    payload: dict[str, Any] = {
        "title": "Software Engineer",
        "summary": "Build reliable backend services.",
        "skills": {"Python": "Advanced"},
        "experience": [{"header": "Engineer", "subtitle": "Example Co", "bullets": ["Built services."]}],
        "projects": [],
        "certifications": "",
        "education": "",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        request_data = json.loads(request.content)
        assert request_data["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload)}}]},
            request=request,
        )

    client = LLMClient(
        "https://api.openai.com/v1",
        "gpt-4o-mini",
        "test-key",
        http_transport=httpx.MockTransport(handler),
    )
    try:
        result = client.ask_structured("Tailor this resume.", TailoredResumeResponse)
    finally:
        client.close()

    assert result.skills == {"Python": "Advanced"}


def test_llm_falls_back_after_transient_primary_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_response_cache()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "primary.test":
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_score_payload())}}]},
            request=request,
        )

    monkeypatch.setattr("applypilot.llm.time.sleep", lambda _seconds: None)
    client = LLMClient(
        "https://primary.test/v1",
        "primary-model",
        "primary-key",
        fallbacks=[("https://backup.test/v1", "backup-model", "backup-key")],
        http_transport=httpx.MockTransport(handler),
    )
    try:
        result = client.structured_chat(
            [{"role": "user", "content": "Use the backup if primary is down."}],
            ScoreResponse,
            max_tokens=256,
        )
    finally:
        client.close()

    assert result.score == 9
    assert calls == ["primary.test", "backup.test"]


@pytest.mark.asyncio
async def test_async_only_llm_context_does_not_allocate_a_sync_pool() -> None:
    client = LLMClient(
        "https://api.openai.com/v1",
        "gpt-4o-mini",
        "test-key",
        http_transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)),
    )
    assert client._client is None

    async with client:
        assert client._client is None
        assert client._async_client is not None

    assert client._async_client is None
    client.close()
    client.close()


@pytest.mark.asyncio
async def test_async_llm_uses_pooled_client_and_pydantic_schema() -> None:
    clear_response_cache()
    calls = 0
    payload = _score_payload()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        request_data = json.loads(request.content)
        assert request_data["response_format"]["type"] == "json_schema"
        json_schema = request_data["response_format"]["json_schema"]
        assert json_schema["strict"] is True
        assert json_schema["schema"]["additionalProperties"] is False
        assert set(json_schema["schema"]["required"]) == set(payload)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload)}}]},
            request=request,
        )

    client = LLMClient(
        "https://api.openai.com/v1",
        "gpt-4o-mini",
        "test-key",
        async_transport=httpx.MockTransport(handler),
    )
    async with client:
        result = await client.aask_structured("Score this candidate.", ScoreResponse, max_tokens=256)
        repeated = await client.aask_structured("Score this candidate.", ScoreResponse, max_tokens=256)

    assert result.score == 9
    assert repeated == result
    assert calls == 1


def _insert_jobs_in_worker(db_path: str, worker_id: int) -> int:
    conn = database.init_db(db_path)
    jobs = [
        {
            "url": f"https://jobs.example.test/{worker_id}/{index}",
            "title": f"Engineer {worker_id}-{index}",
            "company": f"Company {worker_id}",
            "location": "Remote",
        }
        for index in range(10)
    ]
    try:
        inserted, _duplicates = database.store_jobs(conn, jobs, "test", "parallel")
        return int(inserted)
    finally:
        database.close_connection(db_path)



def test_api_response_capture_is_bounded_for_scraping_memory() -> None:
    assert _api_response_body_within_limit("x" * _MAX_API_RESPONSE_CHARS)
    assert not _api_response_body_within_limit("x" * (_MAX_API_RESPONSE_CHARS + 1))
    json_headers = {"content-type": "application/json", "content-length": "64"}
    assert _should_capture_api_response("https://jobs.example.test/api/jobs", json_headers, 0)
    assert _should_capture_api_response("https://jobs.example.test/api/jobs?asset=.png", json_headers, 0)
    assert not _should_capture_api_response("https://cdn.example.test/app.js", json_headers, 0)
    assert not _should_capture_api_response("http://[invalid", json_headers, 0)
    assert not _should_capture_api_response("https://jobs.example.test/api/jobs", {"content-type": "application/json"}, 0)
    assert not _should_capture_api_response(
        "https://jobs.example.test/api/jobs",
        {"content-type": "application/json", "content-length": "unknown"},
        0,
    )
    assert not _should_capture_api_response(
        "https://jobs.example.test/api/jobs",
        {"content-type": "application/json", "content-length": "1000001"},
        0,
    )
    assert not _should_capture_api_response("https://jobs.example.test/api/jobs", json_headers, 24)


def test_sqlite_pragmas_indexes_and_parallel_single_writer(tmp_path: Path) -> None:
    db_path = tmp_path / "applypilot.sqlite3"
    conn = database.init_db(db_path)

    assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
    assert conn.execute("PRAGMA cache_size").fetchone()[0] == -64_000
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(jobs)")}
    assert {
        "idx_jobs_fingerprint",
        "idx_jobs_apply_status",
        "idx_jobs_fit_score_discovered",
        "idx_jobs_pending_detail",
        "idx_jobs_crm_score_date",
    } <= indexes
    database.close_connection(db_path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        inserted = list(pool.map(lambda worker: _insert_jobs_in_worker(str(db_path), worker), range(8)))
    assert sum(inserted) == 80

    with ProcessPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(_insert_jobs_in_worker, str(db_path), 100 + worker)
            for worker in range(4)
        ]
        process_inserted = [future.result() for future in futures]
    assert sum(process_inserted) == 40

    conn = database.init_db(db_path)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 120
    database.close_connection(db_path)


def test_browser_dom_route_blocks_visual_assets_and_telemetry() -> None:
    assert should_block_dom_request("https://cdn.example.test/logo.webp", "other")
    assert should_block_dom_request("https://cdn.example.test/app.woff2", "other")
    assert should_block_dom_request("https://www.googletagmanager.com/gtm.js", "script")
    assert should_block_dom_request("https://example.test/site.css", "stylesheet")
    assert not should_block_dom_request("https://example.test/jobs/api", "xhr")
    assert not should_block_dom_request("https://example.test/app.js", "script")
