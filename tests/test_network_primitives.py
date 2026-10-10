"""Focused validation for retry, browser-routing, and structured-response boundaries."""

from __future__ import annotations

import email.utils
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import httpx
import pytest
from pydantic import ValidationError
from playwright.sync_api import BrowserContext, Route

from applypilot.browser import install_dom_scraping_routes, should_block_dom_request
from applypilot.network import RetryPolicy, build_async_client, parse_retry_after, request_with_retry
from applypilot.schemas import SelectorPlanResponse, StrategyExtractionResponse, StrategyResponse


def test_retry_policy_validates_bounds_and_caps_large_attempt_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="positive integer"):
        RetryPolicy(max_attempts=True)
    with pytest.raises(ValueError, match="finite non-negative"):
        RetryPolicy(base_delay=float("nan"))
    with pytest.raises(ValueError, match="finite and between"):
        RetryPolicy(jitter_ratio=float("inf"))
    with pytest.raises(ValueError, match="finite and between"):
        RetryPolicy(jitter_ratio=True)
    with pytest.raises(ValueError, match="non-negative integer"):
        RetryPolicy().delay(-1)

    monkeypatch.setattr("applypilot.network.random.uniform", lambda lower, upper: upper)
    policy = RetryPolicy(max_attempts=3, base_delay=1.0, max_delay=3.0, jitter_ratio=0.25)
    assert policy.delay(0) == 1.0
    assert policy.delay(1) == 2.0
    assert policy.delay(2) == 3.0
    assert policy.delay(10_000) == 3.0
    assert policy.delay(0, {"Retry-After": "300"}) == 3.0


def test_parse_retry_after_accepts_delta_http_date_and_reset_header() -> None:
    assert parse_retry_after({}) is None
    assert parse_retry_after({"x-ratelimit-reset-requests": "2.5"}) == 2.5
    assert parse_retry_after({"Retry-After": "NaN"}) is None
    assert parse_retry_after({"Retry-After": "Infinity"}) is None
    assert parse_retry_after({"Retry-After": "not-a-date"}) is None
    assert parse_retry_after({"Retry-After": "Wed, 21 Oct 2015 07:28:00"}) == 0.0

    future = email.utils.format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    delay = parse_retry_after({"Retry-After": future})
    assert delay is not None
    assert 0 < delay <= 30


def test_async_client_rejects_invalid_connection_limits() -> None:
    with pytest.raises(ValueError, match="connection limits"):
        build_async_client(max_connections=0)
    with pytest.raises(ValueError, match="connection limits"):
        build_async_client(max_keepalive_connections=-1)


@pytest.mark.asyncio
async def test_request_retry_retries_transport_errors_and_closes_retried_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    responses: list[httpx.Response] = []
    waits: list[float] = []

    async def no_wait(delay: float) -> None:
        waits.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("temporary connection failure", request=request)
        response = httpx.Response(503, request=request)
        responses.append(response)
        if attempts == 2:
            return response
        return httpx.Response(200, request=request)

    monkeypatch.setattr("applypilot.network.asyncio.sleep", no_wait)
    async with build_async_client(transport=httpx.MockTransport(handler)) as client:
        response = await request_with_retry(
            client,
            "GET",
            "https://example.test/retry",
            policy=RetryPolicy(max_attempts=3, base_delay=0.0, max_delay=0.0),
        )

    assert response.status_code == 200
    assert attempts == 3
    assert waits == [0.0, 0.0]
    assert responses[0].is_closed


@pytest.mark.asyncio
async def test_request_retry_returns_non_transient_and_final_transient_responses() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        status = 404 if request.url.path == "/missing" else 429
        return httpx.Response(status, request=request)

    async with build_async_client(transport=httpx.MockTransport(handler)) as client:
        missing = await request_with_retry(
            client,
            "GET",
            "https://example.test/missing",
            policy=RetryPolicy(max_attempts=3, base_delay=0.0, max_delay=0.0),
        )
        final_rate_limit = await request_with_retry(
            client,
            "GET",
            "https://example.test/limited",
            policy=RetryPolicy(max_attempts=2, base_delay=0.0, max_delay=0.0),
        )

    assert missing.status_code == 404
    assert final_rate_limit.status_code == 429
    assert requests == 3


@pytest.mark.asyncio
async def test_request_retry_propagates_exhausted_transport_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection unavailable", request=request)

    async with build_async_client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ConnectError, match="connection unavailable"):
            await request_with_retry(
                client,
                "GET",
                "https://example.test/unavailable",
                policy=RetryPolicy(max_attempts=1),
            )


def test_browser_routes_abort_heavy_assets_and_continue_dom_requests() -> None:
    context = MagicMock(spec=BrowserContext)
    install_dom_scraping_routes(context)
    context.route.assert_called_once()
    pattern, callback = context.route.call_args.args
    assert pattern == "**/*"

    image_route = MagicMock(spec=Route)
    image_route.request.url = "https://cdn.example.test/company-logo.png"
    image_route.request.resource_type = "image"
    callback(image_route)
    image_route.abort.assert_called_once()
    image_route.continue_.assert_not_called()

    script_route = MagicMock(spec=Route)
    script_route.request.url = "https://jobs.example.test/app.js"
    script_route.request.resource_type = "script"
    callback(script_route)
    script_route.continue_.assert_called_once()
    script_route.abort.assert_not_called()


def test_dom_request_filter_uses_hostname_boundaries_and_casefolding() -> None:
    assert should_block_dom_request("https://SUB.GOOGLE-ANALYTICS.COM./collect", "script")
    assert not should_block_dom_request("https://google-analytics.com.attacker.test/collect", "script")
    assert should_block_dom_request("https://cdn.example.test/LOGO.PNG?size=large", "other")
    assert not should_block_dom_request("data:text/plain,hello", "document")


def test_structured_schema_cross_field_invariants_and_extra_field_rejection() -> None:
    with pytest.raises(ValidationError, match="job_card is required"):
        SelectorPlanResponse()
    assert SelectorPlanResponse(error="No usable selectors were found").error

    with pytest.raises(ValidationError, match="requires url_pattern and items_path"):
        StrategyResponse(strategy="api_response", reasoning="Use API data.")
    valid = StrategyResponse(
        strategy="api_response",
        reasoning="The API response contains the job list.",
        extraction=StrategyExtractionResponse(url_pattern="/api/jobs", items_path="data.jobs"),
    )
    assert valid.extraction.items_path == "data.jobs"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        StrategyResponse.model_validate({"strategy": "json_ld", "reasoning": "Use JSON-LD.", "unknown": "field"})
