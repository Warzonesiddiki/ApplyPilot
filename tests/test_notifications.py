"""Notification dispatcher tests: channel detection, digest building, and delivery."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

import applypilot.config as config
import applypilot.database as database
from applypilot.notifications import (
    build_daily_digest,
    has_notification_channel,
    notify_pipeline_summary,
    send_daily_digest,
    send_notification,
)


@pytest.fixture()
def digest_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    config.set_active_profile("digest-tests")
    conn = database.init_db()
    database.ensure_columns(conn)
    conn.executemany(
        "INSERT INTO jobs (url, title, company, location, fit_score, ats_coverage_pct, "
        "sponsor_status, site, strategy, discovered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "https://jobs.example.com/audit-1",
                "Audit Manager",
                "PwC UK",
                "Leeds, UK",
                9,
                84,
                "licensed_sponsor",
                "ICAEW Jobs",
                "ats",
                "2026-10-10T10:00:00Z",
            ),
            (
                "https://jobs.example.com/eng-1",
                "Senior AI Engineer",
                "DeepTech London",
                "London, UK",
                9,
                None,
                None,
                "indeed",
                "jobspy",
                "2026-10-10T11:00:00Z",
            ),
            (
                "https://jobs.example.com/low-1",
                "Clerk",
                "Small Co",
                "Bristol, UK",
                4,
                None,
                None,
                "indeed",
                "jobspy",
                "2026-10-10T12:00:00Z",
            ),
        ],
    )
    conn.commit()
    yield None
    database.close_connection()
    config.set_active_profile(None)


class _FakeResponse:
    def __init__(self, fail: bool) -> None:
        self._fail = fail

    def raise_for_status(self) -> None:
        if self._fail:
            raise httpx.HTTPError("post failed")


class _FakeHttpClient:
    """Records posts made by send_notification; optionally fails every call."""

    posts: list[tuple[str, dict[str, Any]]] = []
    fail: bool = False

    def __init__(self, timeout: float = 0.0) -> None:
        self.timeout = timeout

    def __enter__(self) -> _FakeHttpClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def post(self, url: str, json: dict[str, Any]) -> _FakeResponse:
        _FakeHttpClient.posts.append((url, json))
        return _FakeResponse(_FakeHttpClient.fail)


@pytest.fixture()
def fake_http(monkeypatch: pytest.MonkeyPatch) -> type[_FakeHttpClient]:
    _FakeHttpClient.posts = []
    _FakeHttpClient.fail = False
    monkeypatch.setattr("applypilot.notifications.httpx.Client", _FakeHttpClient)
    return _FakeHttpClient


def test_channel_detection_requires_complete_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert has_notification_channel() is False

    monkeypatch.setenv("WEBHOOK_URL", "https://hooks.example.test/x")
    assert has_notification_channel() is True

    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    assert has_notification_channel() is False  # chat id missing

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    assert has_notification_channel() is True


def test_build_daily_digest_lists_top_matches_with_sponsor_and_ats(
    digest_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    digest = build_daily_digest(min_score=8, limit=5)

    assert "Profile: digest-tests" in digest
    assert "Top Matches (Score >= 8)" in digest
    assert "[9/10, ATS 84% [Sponsor]] Audit Manager @ PwC UK (Leeds, UK)" in digest
    assert "Senior AI Engineer" in digest
    assert "Clerk" not in digest  # below threshold


def test_build_daily_digest_reports_when_nothing_matches(digest_db: None) -> None:
    digest = build_daily_digest(min_score=10)
    assert "No matches at or above threshold yet" in digest


def test_send_notification_dispatches_to_webhook_and_telegram(
    monkeypatch: pytest.MonkeyPatch,
    fake_http: type[_FakeHttpClient],
) -> None:
    monkeypatch.setenv("WEBHOOK_URL", "https://hooks.example.test/x")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    result = send_notification("hello", title="Test Title")

    assert result["sent"] == ["webhook", "telegram"]
    assert result["errors"] == []
    urls = [url for url, _payload in fake_http.posts]
    assert "https://hooks.example.test/x" in urls
    assert "https://api.telegram.org/botbot-token/sendMessage" in urls
    telegram_payload = dict(fake_http.posts[1][1])
    assert telegram_payload["chat_id"] == "42"
    assert "hello" in telegram_payload["text"]


def test_send_notification_collects_delivery_errors(
    monkeypatch: pytest.MonkeyPatch,
    fake_http: type[_FakeHttpClient],
) -> None:
    monkeypatch.setenv("WEBHOOK_URL", "https://hooks.example.test/x")
    fake_http.fail = True

    result = send_notification("hello")

    assert result["sent"] == []
    assert len(result["errors"]) == 1
    assert result["errors"][0].startswith("webhook: ")


def test_notify_pipeline_summary_is_silent_without_channels(
    digest_db: None,
    fake_http: type[_FakeHttpClient],
) -> None:
    result = notify_pipeline_summary({"total": 10, "scored": 4}, profile_name="digest-tests")
    assert result == {"sent": [], "errors": []}
    assert fake_http.posts == []


def test_send_daily_digest_returns_digest_even_without_channels(
    digest_db: None,
    fake_http: type[_FakeHttpClient],
) -> None:
    result = send_daily_digest(min_score=8)
    assert result["sent"] == []
    assert "Top Matches (Score >= 8)" in result["digest"]
    assert fake_http.posts == []
