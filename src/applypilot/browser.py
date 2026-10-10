"""Shared, conservative Playwright resource controls for DOM-only scraping."""

from __future__ import annotations

from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Route

_BLOCKED_RESOURCE_TYPES = frozenset({"image", "font", "stylesheet", "media"})
_BLOCKED_SUFFIXES = (
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".webp",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
)
_TRACKER_DOMAINS = (
    "doubleclick.net",
    "facebook.net",
    "google-analytics.com",
    "googletagmanager.com",
    "hotjar.com",
    "mixpanel.com",
    "segment.io",
    "clarity.ms",
)


def should_block_dom_request(url: str, resource_type: str) -> bool:
    """Return true for heavy visual assets and common analytics/telemetry hosts."""
    if resource_type.lower() in _BLOCKED_RESOURCE_TYPES:
        return True
    parsed = urlsplit(url)
    if parsed.path.lower().endswith(_BLOCKED_SUFFIXES):
        return True
    hostname = (parsed.hostname or "").lower().rstrip(".")
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in _TRACKER_DOMAINS)


def _route_dom_request(route: Route) -> None:
    request = route.request
    if should_block_dom_request(request.url, request.resource_type):
        route.abort()
    else:
        route.continue_()


def install_dom_scraping_routes(context: BrowserContext) -> None:
    """Block assets not needed by text/DOM extractors while retaining JavaScript."""
    context.route("**/*", _route_dom_request)
