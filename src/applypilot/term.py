"""Terminal helpers that keep ApplyPilot's console output safe on legacy code pages.

Windows consoles frequently default to a single-byte code page (``cp1252``,
``cp437``, ...) that cannot represent the glyphs ApplyPilot prints (``●``,
``✓``, ``✗``, emoji, CJK job titles). Rich's legacy-Windows writer sends those
characters straight to ``sys.stdout``, which raises ``UnicodeEncodeError`` and
aborts the command with a traceback.
"""

from __future__ import annotations

import sys
from typing import IO, Any

__all__ = ["configure_stdio", "glyph", "supports_glyph", "safe_text"]


def configure_stdio() -> None:
    """Make ``stdout``/``stderr`` degrade gracefully instead of raising.

    Unencodable characters are replaced with ``?`` rather than aborting the
    process. The stream's encoding is left untouched so real Windows consoles
    keep their native code page. This is a no-op on UTF-8 streams and on
    streams that do not support ``reconfigure`` (e.g. pytest's capture buffer).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):  # pragma: no cover - platform dependent
            continue


def _stream_encoding(stream: IO[str] | None) -> str:
    target: Any = stream if stream is not None else sys.stdout
    encoding = getattr(target, "encoding", None)
    return str(encoding) if encoding else "utf-8"


def supports_glyph(text: str, stream: IO[str] | None = None) -> bool:
    """Return True when the target stream's encoding can represent ``text``."""
    if not text:
        return True
    try:
        text.encode(_stream_encoding(stream), "strict")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def glyph(unicode_text: str, ascii_fallback: str, stream: IO[str] | None = None) -> str:
    """Return ``unicode_text`` when the stream can print it, else ``ascii_fallback``.

    Keeps output readable on legacy Windows consoles instead of emitting the
    ``?`` characters that ``errors="replace"`` would otherwise produce.
    """
    return unicode_text if supports_glyph(unicode_text, stream) else ascii_fallback


def safe_text(text: str, stream: IO[str] | None = None) -> str:
    """Return ``text`` if the stream can encode it, otherwise an ASCII rendering.

    Characters the stream can already represent are preserved; only
    non-encodable ones are transliterated where a common fallback exists and
    replaced with ``?`` afterwards, so a single exotic character (for example in
    a scraped job title) never leaves the rest of a line unreadable.
    """
    if supports_glyph(text, stream):
        return text
    replacements = {
        "\u25cf": "*",  # ●
        "\u2713": "v",  # ✓
        "\u2717": "x",  # ✗
        "\u2588": "#",  # █
        "\u2605": "*",  # ★
        "\u26a0": "!",  # ⚠
        "\u2192": "->",  # →
        "\u2014": "-",  # —
        "\u2013": "-",  # –
        "\u2022": "*",  # •
        "\u2026": "...",  # …
    }
    encoded = _stream_encoding(stream)
    out: list[str] = []
    for ch in text:
        candidate = ch
        try:
            candidate.encode(encoded, "strict")
        except (UnicodeEncodeError, LookupError):  # pragma: no cover - defensive
            candidate = replacements.get(ch, "?")
            try:
                candidate.encode(encoded, "strict")
            except (UnicodeEncodeError, LookupError):
                candidate = "?"
        out.append(candidate)
    return "".join(out)
