"""Webhook & Telegram Notification Dispatcher for ApplyPilot.

Sends optional alerts to Slack, Discord, generic JSON webhooks (`WEBHOOK_URL`),
or Telegram (`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`) when high-fit jobs
(score >= 9) are found or when a pipeline run completes.
"""

from __future__ import annotations

import logging
import os

import httpx

from applypilot import config

log = logging.getLogger(__name__)


def has_notification_channel() -> bool:
    """Return True if a Webhook or Telegram notification channel is configured in env."""
    config.load_env()
    has_webhook = bool(os.environ.get("WEBHOOK_URL", "").strip())
    has_tg = bool(
        os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        and os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    )
    return has_webhook or has_tg


def send_notification(message: str, title: str = "ApplyPilot Update") -> dict:
    """Dispatch a notification message to configured Webhook and/or Telegram channels."""
    config.load_env()
    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    sent_channels: list[str] = []
    errors: list[str] = []

    if webhook_url:
        try:
            payload: dict = {"text": f"*{title}*\n{message}", "content": f"**{title}**\n{message}"}
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(webhook_url, json=payload)
                resp.raise_for_status()
            sent_channels.append("webhook")
        except Exception as exc:
            log.debug("Webhook notification error: %s", exc)
            errors.append(f"webhook: {exc}")

    if tg_token and tg_chat:
        try:
            tg_url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
            payload = {"chat_id": tg_chat, "text": f"{title}\n\n{message}"}
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(tg_url, json=payload)
                resp.raise_for_status()
            sent_channels.append("telegram")
        except Exception as exc:
            log.debug("Telegram notification error: %s", exc)
            errors.append(f"telegram: {exc}")

    return {"sent": sent_channels, "errors": errors}


def notify_pipeline_summary(stats: dict, profile_name: str | None = None) -> dict:
    """Send a pipeline summary notification if a channel is configured."""
    if not has_notification_channel():
        return {"sent": [], "errors": []}

    prof = profile_name or config.get_active_profile() or "default"
    msg = (
        f"Profile: {prof}\n"
        f"Total Jobs: {stats.get('total', 0)} | Scored: {stats.get('scored', 0)} | "
        f"Tailored: {stats.get('tailored', 0)} | Ready to Apply: {stats.get('ready_to_apply', 0)}\n"
        f"Verified UK/EU Sponsors: {stats.get('verified_sponsors', 0)} | "
        f"Avg ATS Coverage: {stats.get('avg_ats_coverage', 0.0)}%"
    )
    return send_notification(msg, title=f"ApplyPilot Run Complete ({prof})")


def build_daily_digest(min_score: int = 8, limit: int = 5) -> str:
    """Build a human-readable Daily Digest of top matches and CRM funnel counts."""
    from applypilot.database import ensure_columns, get_connection, get_stats

    conn = get_connection()
    ensure_columns(conn)
    stats = get_stats(conn)
    prof = config.get_active_profile() or "default"

    top_rows = conn.execute(
        """
        SELECT title, company, location, fit_score, ats_coverage_pct, sponsor_status, url
        FROM jobs
        WHERE COALESCE(fit_score, 0) >= ?
        ORDER BY fit_score DESC, ats_coverage_pct DESC NULLS LAST
        LIMIT ?
        """,
        (min_score, limit),
    ).fetchall()

    lines = [
        f"Profile: {prof}",
        f"Total: {stats.get('total', 0)} | Scored: {stats.get('scored', 0)} | "
        f"Tailored: {stats.get('tailored', 0)} | Applied: {stats.get('applied', 0)}",
        f"UK/EU Sponsors: {stats.get('verified_sponsors', 0)} | Avg ATS Coverage: {stats.get('avg_ats_coverage', 0.0)}%",
        "",
        f"Top Matches (Score >= {min_score}):",
    ]
    if not top_rows:
        lines.append("  (No matches at or above threshold yet)")
    else:
        for r in top_rows:
            sp = " [Sponsor]" if r["sponsor_status"] in ("sponsorship_offered", "licensed_sponsor") else ""
            ats_str = f", ATS {r['ats_coverage_pct']}%" if r["ats_coverage_pct"] is not None else ""
            lines.append(
                f"  - [{r['fit_score']}/10{ats_str}{sp}] {r['title']} @ {r['company'] or 'Employer'} ({r['location'] or 'UK/EU'})"
            )
    return "\n".join(lines)


def send_daily_digest(min_score: int = 8, limit: int = 5) -> dict:
    """Format and dispatch the Daily Digest to configured webhook/Telegram channels."""
    digest = build_daily_digest(min_score=min_score, limit=limit)
    if not has_notification_channel():
        return {"sent": [], "errors": [], "digest": digest}
    res = send_notification(digest, title="ApplyPilot Daily Digest")
    res["digest"] = digest
    return res

