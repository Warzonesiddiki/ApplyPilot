"""Shared async HTTP primitives with connection pooling and resilient retries."""

from __future__ import annotations

import asyncio
import email.utils
import logging
import math
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

_TRANSIENT_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_TRANSIENT_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.WriteError,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential-backoff policy for idempotent HTTP requests."""

    max_attempts: int = 4
    base_delay: float = 0.4
    max_delay: float = 20.0
    jitter_ratio: float = 0.25
    retry_status_codes: frozenset[int] = field(default_factory=lambda: _TRANSIENT_STATUS_CODES)

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        for name, delay in (("base_delay", self.base_delay), ("max_delay", self.max_delay)):
            if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay < 0:
                raise ValueError(f"{name} must be a finite non-negative number")
        if (
            isinstance(self.jitter_ratio, bool)
            or not isinstance(self.jitter_ratio, (int, float))
            or not math.isfinite(self.jitter_ratio)
            or not 0 <= self.jitter_ratio <= 1
        ):
            raise ValueError("jitter_ratio must be finite and between 0 and 1")

    def delay(self, attempt: int, headers: Mapping[str, str] | None = None) -> float:
        """Calculate a retry delay, honoring a server-provided Retry-After value."""
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
            raise ValueError("attempt must be a non-negative integer")
        retry_after = parse_retry_after(headers or {})
        if retry_after is not None:
            # Respect ordinary server delays but cap pathological values so a
            # worker cannot remain parked indefinitely. Add jitter only inside
            # the configured maximum delay.
            bounded_delay = min(retry_after, float(self.max_delay))
            jitter_ceiling = min(0.25, bounded_delay * 0.05, float(self.max_delay) - bounded_delay)
            return bounded_delay + random.uniform(0.0, jitter_ceiling)

        try:
            ceiling = min(math.ldexp(float(self.base_delay), attempt), float(self.max_delay))
        except OverflowError:
            ceiling = float(self.max_delay)
        return random.uniform(ceiling * (1 - self.jitter_ratio), ceiling)


def parse_retry_after(headers: Mapping[str, str]) -> float | None:
    """Parse Retry-After as delta seconds or an HTTP date (RFC 7231)."""
    normalized_headers = {name.lower(): value for name, value in headers.items()}
    value = normalized_headers.get("retry-after")
    if not value:
        # Some APIs expose a reset-after duration under this non-standard name.
        value = normalized_headers.get("x-ratelimit-reset-requests")
    if not value:
        return None

    try:
        delay = float(value)
        return max(0.0, delay) if math.isfinite(delay) else None
    except (TypeError, ValueError):
        try:
            retry_at = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


def build_async_client(
    *,
    timeout: httpx.Timeout | float = httpx.Timeout(20.0, connect=5.0),
    max_connections: int = 100,
    max_keepalive_connections: int = 20,
    headers: Mapping[str, str] | None = None,
    proxy: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Create a pooled AsyncClient with explicit timeout/connection limits."""
    if max_connections < 1 or max_keepalive_connections < 0:
        raise ValueError("connection limits must be positive (keepalive may be zero)")
    limits = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=min(max_keepalive_connections, max_connections),
        keepalive_expiry=30.0,
    )
    return httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        headers=dict(headers or {}),
        follow_redirects=True,
        proxy=proxy,
        transport=transport,
    )


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    policy: RetryPolicy | None = None,
    logger: logging.Logger | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Issue an async request with bounded retries and non-blocking backoff.

    HTTP 429 and transient 5xx responses honor Retry-After. Requests whose
    response status is not retryable are returned unchanged so callers retain
    control over platform-specific handling (for example, an expected 404).
    """
    selected_policy = policy or RetryPolicy()
    request_logger = logger or log

    for attempt in range(selected_policy.max_attempts):
        try:
            response = await client.request(method, url, **kwargs)
        except _TRANSIENT_EXCEPTIONS as exc:
            if attempt + 1 >= selected_policy.max_attempts:
                raise
            wait = selected_policy.delay(attempt)
            request_logger.warning(
                "Transient HTTP transport error (%s), retry %d/%d in %.2fs: %s",
                type(exc).__name__,
                attempt + 1,
                selected_policy.max_attempts - 1,
                wait,
                url,
            )
            await asyncio.sleep(wait)
            continue

        if response.status_code not in selected_policy.retry_status_codes:
            return response
        if attempt + 1 >= selected_policy.max_attempts:
            return response

        wait = selected_policy.delay(attempt, response.headers)
        request_logger.warning(
            "Transient HTTP %d, retry %d/%d in %.2fs: %s",
            response.status_code,
            attempt + 1,
            selected_policy.max_attempts - 1,
            wait,
            url,
        )
        await response.aclose()
        await asyncio.sleep(wait)

    raise RuntimeError("HTTP retry loop exited unexpectedly")
