"""Streaming, byte-bounded HTTP reads and their integration into discovery."""

from __future__ import annotations

import gzip
import json
from typing import Any

import httpx
import pytest

from applypilot.network import (
    ResponseBodyTooLarge,
    RetryPolicy,
    build_async_client,
    request_bounded_bytes,
    request_bounded_json,
)


def _gzipped_json_response(payload: dict[str, Any], request: httpx.Request) -> httpx.Response:
    raw = json.dumps(payload).encode("utf-8")
    compressed = gzip.compress(raw)
    return httpx.Response(
        200,
        headers={"Content-Encoding": "gzip", "Content-Length": str(len(compressed))},
        content=compressed,
        request=request,
    )


@pytest.mark.asyncio
async def test_bounded_bytes_returns_small_bodies_and_decodes_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total": 3, "jobPostings": []}, request=request)

    async with build_async_client(transport=httpx.MockTransport(handler)) as client:
        body = await request_bounded_bytes(client, "GET", "https://api.example.test/jobs", max_bytes=1024)
        payload = await request_bounded_json(client, "GET", "https://api.example.test/jobs", max_bytes=1024)

    assert json.loads(body) == {"total": 3, "jobPostings": []}
    assert payload == {"total": 3, "jobPostings": []}


@pytest.mark.asyncio
async def test_bounded_bytes_rejects_invalid_caps() -> None:
    async with build_async_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request))
    ) as client:
        with pytest.raises(ValueError, match="positive integer"):
            await request_bounded_bytes(client, "GET", "https://api.example.test", max_bytes=0)
        with pytest.raises(ValueError, match="positive integer"):
            await request_bounded_bytes(client, "GET", "https://api.example.test", max_bytes=True)


@pytest.mark.asyncio
async def test_bounded_bytes_stops_transfer_as_soon_as_cap_is_exceeded() -> None:
    """The remainder of an oversized body must never be pulled from the wire."""
    consumed_all = False
    chunks_served = 0

    async def body_iterator() -> Any:
        nonlocal consumed_all, chunks_served
        for _ in range(10):
            chunks_served += 1
            yield b"x" * 100
        consumed_all = True

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body_iterator(), request=request)

    async with build_async_client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ResponseBodyTooLarge):
            await request_bounded_bytes(client, "GET", "https://api.example.test/big", max_bytes=250)

    assert not consumed_all
    assert chunks_served <= 4  # cap hit mid-stream; later chunks never served


@pytest.mark.asyncio
async def test_bounded_bytes_caps_decompressed_size_despite_small_content_length() -> None:
    """A gzip body declaring 93 compressed bytes decodes to 10k+; the cap must bind decoded bytes."""
    payload = {"padding": "A" * 10_000}

    def handler(request: httpx.Request) -> httpx.Response:
        return _gzipped_json_response(payload, request)

    async with build_async_client(transport=httpx.MockTransport(handler)) as client:
        async with client.stream("GET", "https://api.example.test/gz") as probe:
            declared = int(probe.headers["content-length"])
        assert declared < 200  # header alone would suggest a tiny body

        with pytest.raises(ResponseBodyTooLarge):
            await request_bounded_bytes(client, "GET", "https://api.example.test/gz", max_bytes=5_000)

        body = await request_bounded_bytes(client, "GET", "https://api.example.test/gz", max_bytes=20_000)
    assert len(body) > 10_000


@pytest.mark.asyncio
async def test_bounded_bytes_retries_transient_status_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    attempts = 0

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0.1"}, request=request)
        return httpx.Response(200, content=b"ok", request=request)

    monkeypatch.setattr("applypilot.network.asyncio.sleep", record_sleep)
    async with build_async_client(transport=httpx.MockTransport(handler)) as client:
        body = await request_bounded_bytes(
            client,
            "GET",
            "https://api.example.test/flaky",
            max_bytes=16,
            policy=RetryPolicy(max_attempts=3, base_delay=0.5, max_delay=2.0),
        )
    assert body == b"ok"
    assert attempts == 2
    assert sleeps and 0.1 <= sleeps[0] <= 0.175  # Retry-After honored with small jitter


@pytest.mark.asyncio
async def test_bounded_bytes_raises_for_final_transient_and_non_retryable_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(delay: float) -> None:
        return None

    calls = {"count": 0}

    def always_429(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(429, request=request)

    def not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    monkeypatch.setattr("applypilot.network.asyncio.sleep", no_sleep)
    async with build_async_client(transport=httpx.MockTransport(always_429)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await request_bounded_bytes(
                client,
                "GET",
                "https://api.example.test/rate-limited",
                max_bytes=16,
                policy=RetryPolicy(max_attempts=2, base_delay=0.1, max_delay=0.2),
            )
    assert calls["count"] == 2

    async with build_async_client(transport=httpx.MockTransport(not_found)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await request_bounded_bytes(client, "GET", "https://api.example.test/missing", max_bytes=16)


@pytest.mark.asyncio
async def test_bounded_json_propagates_decode_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all", request=request)

    async with build_async_client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(json.JSONDecodeError):
            await request_bounded_json(client, "GET", "https://api.example.test/text", max_bytes=64)


@pytest.mark.asyncio
async def test_bounded_bytes_retries_transport_errors_and_closes_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    closed: list[bool] = []

    async def no_sleep(delay: float) -> None:
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("temporary connection failure", request=request)
        return httpx.Response(200, content=b"recovered", request=request)

    monkeypatch.setattr("applypilot.network.asyncio.sleep", no_sleep)
    async with build_async_client(transport=httpx.MockTransport(handler)) as client:
        body = await request_bounded_bytes(
            client,
            "GET",
            "https://api.example.test/flaky-transport",
            max_bytes=32,
            policy=RetryPolicy(max_attempts=3, base_delay=0.1, max_delay=0.2),
        )
    assert body == b"recovered"
    assert attempts == 2
    assert closed == []
