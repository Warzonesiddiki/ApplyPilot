"""
Unified Multi-Provider LLM Client for ApplyPilot with Automatic Failover & Telemetry.

Auto-detects provider(s) from environment:
  GEMINI_API_KEY      -> Google Gemini (default: gemini-2.0-flash)
  ANTHROPIC_API_KEY   -> Anthropic Claude (default: claude-3-5-haiku-latest)
  OPENAI_API_KEY      -> OpenAI (default: gpt-4o-mini)
  OPENROUTER_API_KEY  -> OpenRouter (default: google/gemini-2.0-flash-001)
  GROQ_API_KEY        -> Groq (default: llama-3.3-70b-versatile)
  DEEPSEEK_API_KEY    -> DeepSeek (default: deepseek-chat)
  LLM_URL             -> Local llama.cpp / Ollama / vLLM compatible endpoint

LLM_MODEL env var overrides the model name for the primary provider.
When multiple API keys are configured in `.env`, LLMClient automatically fails
over to the next available provider on HTTP 429 / 502 / 503.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import httpx

log = logging.getLogger(__name__)

_GEMINI_COMPAT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"
_ANTHROPIC_BASE = "https://api.anthropic.com/v1"

# Approximate USD cost per 1M tokens (input, output) for telemetry estimation
_MODEL_COSTS_PER_1M: dict[str, tuple[float, float]] = {
    "gemini-2.0-flash": (0.10, 0.40),
    "gpt-4o-mini": (0.15, 0.60),
    "claude-3-5-haiku-latest": (0.80, 4.00),
    "claude-3-7-sonnet-latest": (3.00, 15.00),
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "deepseek-chat": (0.14, 0.28),
}

_telemetry_lock = threading.Lock()
_TELEMETRY: dict[str, float | int] = {
    "calls": 0,
    "prompt_tokens_est": 0,
    "completion_tokens_est": 0,
    "estimated_cost_usd": 0.0,
    "failovers": 0,
    "total_latency_ms": 0,
}


def get_telemetry() -> dict[str, float | int]:
    """Return a snapshot of LLM token, cost, failover, and latency telemetry."""
    with _telemetry_lock:
        return dict(_TELEMETRY)


def reset_telemetry() -> None:
    """Reset LLM telemetry counters."""
    with _telemetry_lock:
        for k in _TELEMETRY:
            _TELEMETRY[k] = 0.0 if k == "estimated_cost_usd" else 0


def _record_telemetry(
    model: str,
    prompt_chars: int,
    completion_chars: int,
    latency_ms: int,
    failover: bool = False,
) -> None:
    prompt_toks = max(1, prompt_chars // 4)
    comp_toks = max(1, completion_chars // 4)
    in_rate, out_rate = (0.15, 0.60)
    for key, rates in _MODEL_COSTS_PER_1M.items():
        if key in model.lower():
            in_rate, out_rate = rates
            break
    if "local" in model.lower():
        in_rate, out_rate = (0.0, 0.0)
    cost = (prompt_toks * in_rate + comp_toks * out_rate) / 1_000_000.0
    with _telemetry_lock:
        _TELEMETRY["calls"] = int(_TELEMETRY["calls"]) + 1
        _TELEMETRY["prompt_tokens_est"] = int(_TELEMETRY["prompt_tokens_est"]) + prompt_toks
        _TELEMETRY["completion_tokens_est"] = int(_TELEMETRY["completion_tokens_est"]) + comp_toks
        _TELEMETRY["estimated_cost_usd"] = round(float(_TELEMETRY["estimated_cost_usd"]) + cost, 6)
        if failover:
            _TELEMETRY["failovers"] = int(_TELEMETRY["failovers"]) + 1
        _TELEMETRY["total_latency_ms"] = int(_TELEMETRY["total_latency_ms"]) + latency_ms


# ---------------------------------------------------------------------------
# Provider detection & Fallback Chain
# ---------------------------------------------------------------------------

def _detect_all_providers() -> list[tuple[str, str, str]]:
    """Return ordered list of `(base_url, model, api_key)` for all configured providers."""
    model_override = os.environ.get("LLM_MODEL", "").strip()
    local_url = os.environ.get("LLM_URL", "").strip()
    providers: list[tuple[str, str, str]] = []

    if local_url:
        providers.append(
            (
                local_url.rstrip("/"),
                model_override or "local-model",
                os.environ.get("LLM_API_KEY", ""),
            )
        )

    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if gemini_key:
        providers.append(
            (
                _GEMINI_COMPAT_BASE,
                model_override if (model_override and not providers) else "gemini-2.0-flash",
                gemini_key,
            )
        )

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if anthropic_key:
        providers.append(
            (
                _ANTHROPIC_BASE,
                model_override if (model_override and not providers) else "claude-3-5-haiku-latest",
                anthropic_key,
            )
        )

    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if openai_key:
        providers.append(
            (
                "https://api.openai.com/v1",
                model_override if (model_override and not providers) else "gpt-4o-mini",
                openai_key,
            )
        )

    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if openrouter_key:
        providers.append(
            (
                "https://openrouter.ai/api/v1",
                model_override if (model_override and not providers) else "google/gemini-2.0-flash-001",
                openrouter_key,
            )
        )

    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    if groq_key:
        providers.append(
            (
                "https://api.groq.com/openai/v1",
                model_override if (model_override and not providers) else "llama-3.3-70b-versatile",
                groq_key,
            )
        )

    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if deepseek_key:
        providers.append(
            (
                "https://api.deepseek.com/v1",
                model_override if (model_override and not providers) else "deepseek-chat",
                deepseek_key,
            )
        )

    return providers


def _detect_provider() -> tuple[str, str, str]:
    """Return primary `(base_url, model, api_key)` based on environment variables."""
    providers = _detect_all_providers()
    if providers:
        return providers[0]

    raise RuntimeError(
        "No LLM provider configured. "
        "Set GEMINI_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY, GROQ_API_KEY, DEEPSEEK_API_KEY, or LLM_URL in your environment."
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_MAX_RETRIES = 5
_TIMEOUT = 120  # seconds
_RATE_LIMIT_BASE_WAIT = 10


class LLMClient:
    """Multi-provider LLM client supporting OpenAI-compatible, native Gemini, and native Anthropic APIs.

    Automatically fails over to secondary configured providers when the primary
    provider hits rate limits (429) or transient outages (502/503).
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        fallbacks: list[tuple[str, str, str]] | None = None,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.fallbacks = fallbacks or []
        self._client = httpx.Client(timeout=_TIMEOUT)
        self._use_native_gemini: bool = False
        self._is_gemini: bool = base_url.startswith(_GEMINI_COMPAT_BASE)
        self._is_anthropic: bool = base_url.startswith(_ANTHROPIC_BASE)

    # -- Native Gemini API --------------------------------------------------

    def _chat_native_gemini(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        model: str | None = None,
        api_key: str | None = None,
    ) -> str:
        """Call the native Gemini generateContent API."""
        contents: list[dict] = []
        system_parts: list[dict] = []

        for msg in messages:
            role = msg["role"]
            text = msg.get("content", "")
            if role == "system":
                system_parts.append({"text": text})
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": text}]})
            elif role == "assistant":
                contents.append({"role": "model", "parts": [{"text": text}]})

        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}

        use_model = model or self.model
        use_key = api_key or self.api_key
        url = f"{_GEMINI_NATIVE_BASE}/models/{use_model}:generateContent"
        resp = self._client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            params={"key": use_key},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    # -- Native Anthropic Claude API ----------------------------------------

    def _chat_anthropic(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> str:
        """Call Anthropic's `/v1/messages` API."""
        system_parts: list[str] = []
        chat_msgs: list[dict] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                system_parts.append(content)
            else:
                chat_msgs.append({"role": "assistant" if role == "assistant" else "user", "content": content})

        if not chat_msgs:
            chat_msgs.append({"role": "user", "content": "\n".join(system_parts)})
            system_parts = []

        payload: dict = {
            "model": model or self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": chat_msgs,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)

        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key or self.api_key,
            "anthropic-version": "2023-06-01",
        }
        url = f"{(base_url or self.base_url).rstrip('/')}/messages"
        resp = self._client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        blocks = data.get("content", [])
        if blocks and isinstance(blocks, list):
            return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        return ""

    # -- OpenAI-compat API --------------------------------------------------

    def _chat_compat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> str:
        """Call the OpenAI-compatible endpoint."""
        use_base = base_url or self.base_url
        use_model = model or self.model
        use_key = api_key if api_key is not None else self.api_key

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if use_key:
            headers["Authorization"] = f"Bearer {use_key}"

        payload = {
            "model": use_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        resp = self._client.post(
            f"{use_base}/chat/completions",
            json=payload,
            headers=headers,
        )

        if resp.status_code == 403 and use_base.startswith(_GEMINI_COMPAT_BASE):
            raise _GeminiCompatForbidden(resp)

        return self._handle_compat_response(resp)

    @staticmethod
    def _handle_compat_response(resp: httpx.Response) -> str:
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    def _try_fallbacks(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> tuple[str, str] | None:
        """Attempt secondary configured providers before sleeping on rate limit."""
        for fb_base, fb_model, fb_key in self.fallbacks:
            try:
                log.info("Failing over to backup LLM provider: %s (%s)", fb_base, fb_model)
                if fb_base.startswith(_ANTHROPIC_BASE):
                    out = self._chat_anthropic(messages, temperature, max_tokens, fb_base, fb_model, fb_key)
                elif fb_base.startswith(_GEMINI_COMPAT_BASE):
                    out = self._chat_native_gemini(messages, temperature, max_tokens, fb_model, fb_key)
                else:
                    out = self._chat_compat(messages, temperature, max_tokens, fb_base, fb_model, fb_key)
                return (out, fb_model)
            except Exception as fb_exc:
                log.warning("Fallback provider %s failed: %s", fb_model, fb_exc)
        return None

    # -- public API ---------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """Send a chat completion request and return the assistant message text."""
        if "qwen" in self.model.lower() and messages:
            first = messages[0]
            if first.get("role") == "user" and not first["content"].startswith("/no_think"):
                messages = [{"role": first["role"], "content": f"/no_think\n{first['content']}"}] + messages[1:]

        prompt_chars = sum(len(m.get("content", "")) for m in messages)

        for attempt in range(_MAX_RETRIES):
            t0 = time.time()
            try:
                if self._is_anthropic:
                    out = self._chat_anthropic(messages, temperature, max_tokens)
                elif self._use_native_gemini:
                    out = self._chat_native_gemini(messages, temperature, max_tokens)
                else:
                    out = self._chat_compat(messages, temperature, max_tokens)

                latency_ms = int((time.time() - t0) * 1000)
                _record_telemetry(self.model, prompt_chars, len(out), latency_ms, failover=False)
                return out

            except _GeminiCompatForbidden:
                log.warning(
                    "Gemini compat endpoint returned 403 for model '%s'. "
                    "Switching to native generateContent API.",
                    self.model,
                )
                self._use_native_gemini = True
                try:
                    out = self._chat_native_gemini(messages, temperature, max_tokens)
                    latency_ms = int((time.time() - t0) * 1000)
                    _record_telemetry(self.model, prompt_chars, len(out), latency_ms, failover=False)
                    return out
                except httpx.HTTPStatusError as native_exc:
                    raise RuntimeError(
                        f"Both Gemini endpoints failed. Compat: 403 Forbidden. "
                        f"Native: {native_exc.response.status_code} — "
                        f"{native_exc.response.text[:200]}"
                    ) from native_exc

            except httpx.HTTPStatusError as exc:
                resp = exc.response
                if resp.status_code in (429, 502, 503):
                    # Try configured fallback provider immediately before sleeping
                    if self.fallbacks:
                        fb_res = self._try_fallbacks(messages, temperature, max_tokens)
                        if fb_res is not None:
                            out, fb_model = fb_res
                            latency_ms = int((time.time() - t0) * 1000)
                            _record_telemetry(fb_model, prompt_chars, len(out), latency_ms, failover=True)
                            return out

                if resp.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
                    retry_after = (
                        resp.headers.get("Retry-After")
                        or resp.headers.get("X-RateLimit-Reset-Requests")
                    )
                    if retry_after:
                        try:
                            wait = float(retry_after)
                        except (ValueError, TypeError):
                            wait = _RATE_LIMIT_BASE_WAIT * (2 ** attempt)
                    else:
                        wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)

                    log.warning(
                        "LLM rate limited (HTTP %s). Waiting %ds before retry %d/%d.",
                        resp.status_code, wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

            except httpx.TimeoutException:
                if self.fallbacks:
                    fb_res = self._try_fallbacks(messages, temperature, max_tokens)
                    if fb_res is not None:
                        out, fb_model = fb_res
                        latency_ms = int((time.time() - t0) * 1000)
                        _record_telemetry(fb_model, prompt_chars, len(out), latency_ms, failover=True)
                        return out

                if attempt < _MAX_RETRIES - 1:
                    wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)
                    log.warning(
                        "LLM request timed out, retrying in %ds (attempt %d/%d)",
                        wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError("LLM request failed after all retries")

    def ask(self, prompt: str, **kwargs) -> str:
        """Convenience: single user prompt -> assistant response."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        self._client.close()


class _GeminiCompatForbidden(Exception):
    """Sentinel: Gemini OpenAI-compat returned 403. Switch to native API."""
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"Gemini compat 403: {response.text[:200]}")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: LLMClient | None = None


def get_client() -> LLMClient:
    """Return (or create) the module-level LLMClient singleton."""
    global _instance
    if _instance is None:
        providers = _detect_all_providers()
        if not providers:
            _detect_provider()  # raises clear RuntimeError
        base_url, model, api_key = providers[0]
        fallbacks = providers[1:]
        log.info("LLM provider: %s  model: %s (fallbacks: %d)", base_url, model, len(fallbacks))
        _instance = LLMClient(base_url, model, api_key, fallbacks=fallbacks)
    return _instance
