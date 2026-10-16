"""Pooled multi-provider LLM client with sync/async APIs and strict schemas.

Providers are selected from environment variables in this order: local endpoint,
Gemini, Anthropic, OpenAI, OpenRouter, Groq, and DeepSeek. The first available
provider is primary; configured providers after it form the failover chain.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from applypilot.network import RetryPolicy, build_async_client, request_with_retry

log = logging.getLogger(__name__)

_GEMINI_COMPAT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"
_ANTHROPIC_BASE = "https://api.anthropic.com/v1"
_TIMEOUT = httpx.Timeout(120.0, connect=10.0, pool=10.0)
_HTTP_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=30.0)
_MAX_RETRIES = 5
_RATE_LIMIT_BASE_WAIT = 1.5
_TRANSIENT_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_CACHE_MAXSIZE = 512
_CACHE_MAX_BYTES = 8 * 1024 * 1024

# Approximate USD cost per 1M tokens (input, output) for telemetry estimation.
# Keys are matched as substrings of the requested model name, most specific first
# is not required because names are matched in insertion order.
_MODEL_COSTS_PER_1M: dict[str, tuple[float, float]] = {
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.0-flash-lite": (0.075, 0.30),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-1.5-flash": (0.075, 0.30),
    "gemini-1.5-pro": (1.25, 5.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "o1": (15.00, 60.00),
    "o1-mini": (1.10, 4.40),
    "o3-mini": (1.10, 4.40),
    "claude-3-haiku": (0.25, 1.25),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-7-sonnet": (3.00, 15.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-opus-4": (15.00, 75.00),
    "claude-3-opus": (15.00, 75.00),
    "llama-3.3-70b": (0.59, 0.79),
    "llama-3.1-70b": (0.59, 0.79),
    "llama-3.1-8b": (0.05, 0.08),
    "mixtral-8x7b": (0.24, 0.24),
    "gemma-2-9b": (0.20, 0.20),
    "deepseek-chat": (0.14, 0.28),
    "deepseek-reasoner": (0.55, 2.19),
}

# Operators can pin an unknown model's price so telemetry stays truthful:
#   LLM_COST_PER_1M="my-finetune=3.00,15.00;some-other-model=1,4"
_COST_OVERRIDE_ENV = "LLM_COST_PER_1M"

_telemetry_lock = threading.Lock()
_TELEMETRY: dict[str, float | int] = {
    "calls": 0,
    "prompt_tokens_est": 0,
    "completion_tokens_est": 0,
    "estimated_cost_usd": 0.0,
    "failovers": 0,
    "cache_hits": 0,
    "total_latency_ms": 0,
    # Calls whose model had no known price. Their cost contributes 0.0 rather than
    # a guessed rate, so `estimated_cost_usd` is never silently wrong.
    "unpriced_calls": 0,
}
_cache_lock = threading.Lock()
_RESPONSE_CACHE: OrderedDict[str, tuple[str, int]] = OrderedDict()
_RESPONSE_CACHE_BYTES = 0

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)
Provider = tuple[str, str, str]
Message = dict[str, Any]


def get_telemetry() -> dict[str, float | int]:
    """Return a snapshot of LLM token, cost, failover, and latency telemetry."""
    with _telemetry_lock:
        return dict(_TELEMETRY)


def reset_telemetry() -> None:
    """Reset LLM telemetry counters."""
    with _telemetry_lock:
        for key in _TELEMETRY:
            _TELEMETRY[key] = 0.0 if key == "estimated_cost_usd" else 0


def clear_response_cache() -> None:
    """Clear the bounded process-local response cache (useful for tests)."""
    global _RESPONSE_CACHE_BYTES
    with _cache_lock:
        _RESPONSE_CACHE.clear()
        _RESPONSE_CACHE_BYTES = 0


def response_cache_info() -> dict[str, int]:
    """Report the number and UTF-8 size of cached deterministic responses."""
    with _cache_lock:
        return {
            "size": len(_RESPONSE_CACHE),
            "maxsize": _CACHE_MAXSIZE,
            "bytes": _RESPONSE_CACHE_BYTES,
            "max_bytes": _CACHE_MAX_BYTES,
        }


def _cost_override_table() -> dict[str, tuple[float, float]]:
    """Parse `LLM_COST_PER_1M` into {model: (input, output)} rates."""
    raw = os.environ.get(_COST_OVERRIDE_ENV, "").strip()
    table: dict[str, tuple[float, float]] = {}
    if not raw:
        return table
    for entry in raw.split(";"):
        if "=" not in entry:
            continue
        name, _, rates = entry.partition("=")
        parts = [part.strip() for part in rates.split(",")]
        if len(parts) != 2:
            continue
        try:
            table[name.strip().lower()] = (float(parts[0]), float(parts[1]))
        except ValueError:
            log.warning("Ignoring malformed %s entry for model %r", _COST_OVERRIDE_ENV, name)
    return table


def _model_rates(model: str) -> tuple[float, float, bool]:
    """Return (input_rate, output_rate, priced) for a model name per 1M tokens.

    An unknown model prices at 0.0 and is reported as unpriced rather than being
    billed at an unrelated model's rate.
    """
    lowered = (model or "").lower()
    if "local" in lowered:
        return (0.0, 0.0, True)
    overrides = _cost_override_table()
    for name, rates in overrides.items():
        if name and name in lowered:
            return (rates[0], rates[1], True)
    for model_prefix, rates in _MODEL_COSTS_PER_1M.items():
        if model_prefix in lowered:
            return (rates[0], rates[1], True)
    return (0.0, 0.0, False)


def _record_telemetry(
    model: str,
    prompt_chars: int,
    completion_chars: int,
    latency_ms: int,
    failover: bool = False,
) -> None:
    prompt_toks = max(1, prompt_chars // 4)
    completion_toks = max(1, completion_chars // 4)
    input_rate, output_rate, priced = _model_rates(model)
    cost = (prompt_toks * input_rate + completion_toks * output_rate) / 1_000_000.0
    with _telemetry_lock:
        _TELEMETRY["calls"] = int(_TELEMETRY["calls"]) + 1
        _TELEMETRY["prompt_tokens_est"] = int(_TELEMETRY["prompt_tokens_est"]) + prompt_toks
        _TELEMETRY["completion_tokens_est"] = int(_TELEMETRY["completion_tokens_est"]) + completion_toks
        _TELEMETRY["estimated_cost_usd"] = round(float(_TELEMETRY["estimated_cost_usd"]) + cost, 6)
        if not priced:
            _TELEMETRY["unpriced_calls"] = int(_TELEMETRY["unpriced_calls"]) + 1
        if failover:
            _TELEMETRY["failovers"] = int(_TELEMETRY["failovers"]) + 1
        _TELEMETRY["total_latency_ms"] = int(_TELEMETRY["total_latency_ms"]) + latency_ms


def _record_cache_hit() -> None:
    with _telemetry_lock:
        _TELEMETRY["cache_hits"] = int(_TELEMETRY["cache_hits"]) + 1


def _infer_model_family(model_name: str) -> str | None:
    """Classify a model id by its vendor prefix, or None when it is unknown.

    Unknown ids (custom/self-hosted names) return None so an explicit
    `LLM_MODEL` override is still honoured for them.
    """
    name = model_name.strip().lower()
    if not name:
        return None
    if name.startswith("claude"):
        return "anthropic"
    if name.startswith(("gpt-", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    if name.startswith("gemini"):
        return "google"
    if name.startswith(("llama", "mixtral", "gemma", "qwen", "kimi", "grok")):
        return "groq"
    if name.startswith("deepseek"):
        return "deepseek"
    return None


def _provider_accepts_family(base_url: str, family: str | None) -> bool:
    """Whether a hosted provider recognises model ids from ``family``.

    An unknown family (a custom model name) is always accepted: the operator asked
    for that model explicitly. A *known* family belonging to another vendor is
    rejected so we never send a model name an endpoint will 404 on.
    """
    if family is None:
        return True
    if not base_url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        return True
    host = base_url.split("://", 1)[1].split("/", 1)[0]
    if host.startswith("127.0.0.1") or host.startswith("localhost") or ".local" in host or ":11434" in base_url:
        return True  # self-hosted endpoint, any model id is plausible
    if "anthropic" in host:
        return family == "anthropic"
    if "openai.com" in host:
        return family == "openai"
    if "googleapis.com" in host:
        return family == "google"
    if "deepseek.com" in host:
        return family == "deepseek"
    if "openrouter.ai" in host:
        return True  # aggregator: hosts other vendors' models
    if "groq.com" in host:
        return family == "groq"
    return True  # unknown provider, do not second-guess the operator


def _detect_all_providers() -> list[Provider]:
    """Return configured providers in their deterministic failover order.

    `LLM_MODEL` overrides the model of the primary (first configured) endpoint and
    is forwarded to a hosted provider only when that provider recognises the model
    family, so one override can never 404 across an entire failover chain.
    """
    model_override = os.environ.get("LLM_MODEL", "").strip()
    local_url = os.environ.get("LLM_URL", "").strip()
    providers: list[Provider] = []

    if local_url:
        providers.append((local_url.rstrip("/"), model_override or "local-model", os.environ.get("LLM_API_KEY", "")))

    provider_specs: tuple[tuple[str, str, str], ...] = (
        ("GEMINI_API_KEY", _GEMINI_COMPAT_BASE, "gemini-2.0-flash"),
        ("ANTHROPIC_API_KEY", _ANTHROPIC_BASE, "claude-3-5-haiku-latest"),
        ("OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-4o-mini"),
        ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "google/gemini-2.0-flash-001"),
        ("GROQ_API_KEY", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
        ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1", "deepseek-chat"),
    )
    for key_name, base_url, default_model in provider_specs:
        api_key = os.environ.get(key_name, "").strip()
        if not api_key:
            continue
        model = default_model
        if model_override and not providers:
            family = _infer_model_family(model_override)
            if _provider_accepts_family(base_url, family):
                model = model_override
            else:
                log.warning(
                    "Ignoring LLM_MODEL=%s for %s: this endpoint only serves its own model ids. "
                    "Using %s instead.",
                    model_override,
                    base_url,
                    default_model,
                )
        providers.append((base_url, model, api_key))
    return providers


def _detect_provider() -> Provider:
    """Return primary `(base_url, model, api_key)` or raise a configuration error."""
    providers = _detect_all_providers()
    if providers:
        return providers[0]
    raise RuntimeError(
        "No LLM provider configured. Set GEMINI_API_KEY, ANTHROPIC_API_KEY, "
        "OPENAI_API_KEY, OPENROUTER_API_KEY, GROQ_API_KEY, DEEPSEEK_API_KEY, or LLM_URL."
    )


def _cache_key(
    base_url: str,
    model: str,
    messages: Sequence[Message],
    temperature: float,
    max_tokens: int,
    response_model: type[BaseModel] | None,
) -> str:
    schema = response_model.model_json_schema() if response_model else None
    cache_payload = {
        "provider": base_url,
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "schema": schema,
    }
    serialized = json.dumps(cache_payload, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> str | None:
    with _cache_lock:
        entry = _RESPONSE_CACHE.get(key)
        if entry is not None:
            _RESPONSE_CACHE.move_to_end(key)
            return entry[0]
        return None


def _cache_put(key: str, response: str) -> None:
    global _RESPONSE_CACHE_BYTES
    response_bytes = len(response.encode("utf-8", errors="replace"))
    if response_bytes > _CACHE_MAX_BYTES:
        return

    with _cache_lock:
        previous = _RESPONSE_CACHE.pop(key, None)
        if previous is not None:
            _RESPONSE_CACHE_BYTES -= previous[1]
        _RESPONSE_CACHE[key] = (response, response_bytes)
        _RESPONSE_CACHE_BYTES += response_bytes
        while len(_RESPONSE_CACHE) > _CACHE_MAXSIZE or _RESPONSE_CACHE_BYTES > _CACHE_MAX_BYTES:
            _, (_, evicted_bytes) = _RESPONSE_CACHE.popitem(last=False)
            _RESPONSE_CACHE_BYTES -= evicted_bytes


class _UnsupportedOpenAISchema(ValueError):
    """A Pydantic schema cannot be represented by OpenAI's strict schema subset."""


def _openai_strict_schema(response_model: type[BaseModel]) -> dict[str, Any]:
    """Prepare a Pydantic schema for OpenAI strict Structured Outputs.

    OpenAI strict schemas require closed objects and every declared property to
    be required. Arbitrary-key mappings cannot be represented without changing
    their meaning, so callers can fall back to JSON-object mode for those.
    """
    schema = response_model.model_json_schema()

    def enforce_strict(node: Any) -> None:
        if isinstance(node, dict):
            # JSON Schema defaults do not affect validation and are rejected by
            # some strict-output implementations, so omit them from requests.
            node.pop("default", None)
            if node.get("type") == "object":
                properties = node.get("properties", {})
                additional_properties = node.get("additionalProperties")
                if additional_properties is not None and additional_properties is not False:
                    raise _UnsupportedOpenAISchema("arbitrary object keys are unsupported")
                node["additionalProperties"] = False
                node["required"] = list(properties)
            for value in node.values():
                enforce_strict(value)
        elif isinstance(node, list):
            for value in node:
                enforce_strict(value)

    enforce_strict(schema)
    return schema


def _schema_messages(messages: Sequence[Message], response_model: type[BaseModel] | None) -> list[Message]:
    """Copy messages and include the JSON Schema in the system instruction."""
    copied = [dict(message) for message in messages]
    if response_model is None:
        return copied
    schema = json.dumps(response_model.model_json_schema(), ensure_ascii=False, sort_keys=True)
    instruction = (
        "Return only one valid JSON object matching this JSON Schema. Do not use Markdown fences, "
        "preamble, or additional keys. Validate every required field.\nJSON Schema:\n" + schema
    )
    for message in copied:
        if message.get("role") == "system":
            message["content"] = f"{message.get('content', '')}\n\n{instruction}"
            break
    else:
        copied.insert(0, {"role": "system", "content": instruction})
    return copied


def _json_schema_response(raw: str, response_model: type[BaseModel] | None) -> None:
    if response_model is None:
        return
    response_model.model_validate_json(raw)


class StructuredOutputError(ValueError):
    """An LLM response could not be validated against its requested schema."""


class _GeminiCompatForbidden(Exception):
    """Sentinel for the Gemini OpenAI-compat endpoint returning HTTP 403."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"Gemini compat 403: {response.text[:200]}")


class LLMClient:
    """Pooled sync/async multi-provider chat client with resilient fallback.

    Existing CLI calls use `chat` / `ask`. Async services may use
    `async with client:` plus `achat`/`achat_structured` to reuse one pooled
    `httpx.AsyncClient` for the full operation.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        fallbacks: list[Provider] | None = None,
        *,
        http_transport: httpx.BaseTransport | None = None,
        async_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.fallbacks = list(fallbacks or [])
        self._http_transport = http_transport
        self._async_transport = async_transport
        self._client: httpx.Client | None = None
        self._sync_client_lock = threading.Lock()
        self._async_client: httpx.AsyncClient | None = None
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._use_native_gemini = False
        self._is_anthropic = self.base_url.startswith(_ANTHROPIC_BASE)
        self._async_session_owned = False

    def _native_gemini_payload(
        self,
        messages: Sequence[Message],
        temperature: float,
        max_tokens: int,
        model: str,
    ) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        system_parts: list[dict[str, str]] = []
        for message in messages:
            role = message.get("role", "user")
            content = str(message.get("content", ""))
            if role == "system":
                system_parts.append({"text": content})
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": content}]})
            elif role == "assistant":
                contents.append({"role": "model", "parts": [{"text": content}]})
        generation_config: dict[str, Any] = {"temperature": temperature, "maxOutputTokens": max_tokens}
        payload: dict[str, Any] = {"contents": contents, "generationConfig": generation_config}
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}
        return payload

    @staticmethod
    def _anthropic_messages(messages: Sequence[Message]) -> tuple[str, list[dict[str, str]]]:
        system_parts: list[str] = []
        chat_messages: list[dict[str, str]] = []
        for message in messages:
            role = message.get("role", "user")
            content = str(message.get("content", ""))
            if role == "system":
                system_parts.append(content)
            else:
                chat_messages.append({"role": "assistant" if role == "assistant" else "user", "content": content})
        if not chat_messages:
            chat_messages.append({"role": "user", "content": "\n".join(system_parts)})
            system_parts = []
        return "\n\n".join(system_parts), chat_messages

    def _provider_request(
        self,
        provider: Provider,
        messages: Sequence[Message],
        temperature: float,
        max_tokens: int,
        response_model: type[BaseModel] | None,
        *,
        native_gemini: bool = False,
    ) -> tuple[str, dict[str, str], dict[str, str], dict[str, Any], str]:
        base_url, model, api_key = provider
        if native_gemini:
            url = f"{_GEMINI_NATIVE_BASE}/models/{model}:generateContent"
            payload = self._native_gemini_payload(messages, temperature, max_tokens, model)
            if response_model is not None:
                payload["generationConfig"]["responseMimeType"] = "application/json"
            return url, {"Content-Type": "application/json"}, {"key": api_key}, payload, "gemini"

        if base_url.startswith(_ANTHROPIC_BASE):
            system, chat_messages = self._anthropic_messages(messages)
            payload = {"model": model, "max_tokens": max_tokens, "temperature": temperature, "messages": chat_messages}
            if system:
                payload["system"] = system
            if response_model is not None:
                payload["tools"] = [
                    {
                        "name": "return_structured_response",
                        "description": "Return the response using the required structured schema.",
                        "input_schema": response_model.model_json_schema(),
                    }
                ]
                payload["tool_choice"] = {"type": "tool", "name": "return_structured_response"}
            headers = {
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            }
            return f"{base_url.rstrip('/')}/messages", headers, {}, payload, "anthropic"

        payload = {
            "model": model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_model is not None:
            if base_url.rstrip("/") == "https://api.openai.com/v1":
                try:
                    strict_schema = _openai_strict_schema(response_model)
                except _UnsupportedOpenAISchema as exc:
                    log.debug(
                        "Using JSON-object output for %s: %s",
                        response_model.__name__,
                        exc,
                    )
                    payload["response_format"] = {"type": "json_object"}
                else:
                    payload["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": response_model.__name__.lower(),
                            "strict": True,
                            "schema": strict_schema,
                        },
                    }
            else:
                payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return f"{base_url.rstrip('/')}/chat/completions", headers, {}, payload, "openai"

    @staticmethod
    def _parse_provider_response(response: httpx.Response, response_type: str) -> str:
        data = response.json()
        if response_type == "gemini":
            candidates = data.get("candidates", [])
            if not candidates:
                raise ValueError("Gemini returned no candidates")
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))
            if not text:
                raise ValueError("Gemini returned no textual candidate content")
            return text
        if response_type == "anthropic":
            blocks = data.get("content", [])
            if isinstance(blocks, list):
                for block in blocks:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        return json.dumps(block.get("input", {}), ensure_ascii=False)
                return "".join(str(block.get("text", "")) for block in blocks if isinstance(block, dict))
            return ""
        choices = data.get("choices", [])
        if not choices:
            raise ValueError("LLM provider returned no completion choices")
        content = choices[0].get("message", {}).get("content")
        if not isinstance(content, str):
            raise ValueError("LLM provider returned an empty or non-text completion")
        return content

    def _get_sync_client(self) -> httpx.Client:
        """Lazily create the sync pool so async-only users own no unused client."""
        if self._client is None:
            with self._sync_client_lock:
                if self._client is None:
                    self._client = httpx.Client(
                        timeout=_TIMEOUT,
                        limits=_HTTP_LIMITS,
                        transport=self._http_transport,
                    )
        return self._client

    def _request_sync(
        self,
        provider: Provider,
        messages: Sequence[Message],
        temperature: float,
        max_tokens: int,
        response_model: type[BaseModel] | None,
        *,
        native_gemini: bool = False,
    ) -> str:
        url, headers, params, payload, response_type = self._provider_request(
            provider, messages, temperature, max_tokens, response_model, native_gemini=native_gemini
        )
        client = self._get_sync_client()
        response = client.post(url, json=payload, headers=headers, params=params)
        if response.status_code == 403 and provider[0].startswith(_GEMINI_COMPAT_BASE) and not native_gemini:
            raise _GeminiCompatForbidden(response)
        if (
            response_model is not None
            and response.status_code == 400
            and "response_format" in response.text.lower()
            and response_type == "openai"
        ):
            payload.pop("response_format", None)
            response = client.post(url, json=payload, headers=headers, params=params)
        response.raise_for_status()
        return self._parse_provider_response(response, response_type)

    async def _request_async(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        messages: Sequence[Message],
        temperature: float,
        max_tokens: int,
        response_model: type[BaseModel] | None,
        *,
        native_gemini: bool = False,
    ) -> str:
        url, headers, params, payload, response_type = self._provider_request(
            provider, messages, temperature, max_tokens, response_model, native_gemini=native_gemini
        )
        response = await request_with_retry(
            client,
            "POST",
            url,
            policy=RetryPolicy(max_attempts=1),
            json=payload,
            headers=headers,
            params=params,
            logger=log,
        )
        if response.status_code == 403 and provider[0].startswith(_GEMINI_COMPAT_BASE) and not native_gemini:
            raise _GeminiCompatForbidden(response)
        if (
            response_model is not None
            and response.status_code == 400
            and "response_format" in response.text.lower()
            and response_type == "openai"
        ):
            payload.pop("response_format", None)
            response = await request_with_retry(
                client,
                "POST",
                url,
                policy=RetryPolicy(max_attempts=1),
                json=payload,
                headers=headers,
                params=params,
                logger=log,
            )
        response.raise_for_status()
        return self._parse_provider_response(response, response_type)

    def _try_fallbacks(
        self,
        messages: Sequence[Message],
        temperature: float,
        max_tokens: int,
        response_model: type[BaseModel] | None,
    ) -> tuple[str, str, int] | None:
        """Return `(output, model, latency_ms)` from the first healthy fallback.

        The latency is the fallback attempt's own duration, not the primary
        attempt's, so failover telemetry never double-counts the failed call.
        """
        for provider in self.fallbacks:
            base_url, model, _ = provider
            log.warning("LLM provider downgrade: switching to %s (%s)", base_url, model)
            started = time.perf_counter()
            try:
                output = self._request_sync(
                    provider,
                    messages,
                    temperature,
                    max_tokens,
                    response_model,
                    native_gemini=base_url.startswith(_GEMINI_COMPAT_BASE),
                )
                return (output, model, int((time.perf_counter() - started) * 1000))
            except (httpx.HTTPError, ValueError, KeyError, _GeminiCompatForbidden) as exc:
                log.warning("Fallback LLM provider %s failed: %s", model, exc)
        return None

    async def _try_fallbacks_async(
        self,
        client: httpx.AsyncClient,
        messages: Sequence[Message],
        temperature: float,
        max_tokens: int,
        response_model: type[BaseModel] | None,
    ) -> tuple[str, str, int] | None:
        """Async counterpart of `_try_fallbacks`, with its own latency measurement."""
        for provider in self.fallbacks:
            base_url, model, _ = provider
            log.warning("LLM provider downgrade: switching to %s (%s)", base_url, model)
            started = time.perf_counter()
            try:
                output = await self._request_async(
                    client,
                    provider,
                    messages,
                    temperature,
                    max_tokens,
                    response_model,
                    native_gemini=base_url.startswith(_GEMINI_COMPAT_BASE),
                )
                return (output, model, int((time.perf_counter() - started) * 1000))
            except (httpx.HTTPError, ValueError, KeyError, _GeminiCompatForbidden) as exc:
                log.warning("Fallback LLM provider %s failed: %s", model, exc)
        return None

    def chat(
        self,
        messages: list[Message],
        temperature: float = 0.0,
        max_tokens: int = 4096,
        response_model: type[BaseModel] | None = None,
    ) -> str:
        """Send a chat completion request; optional Pydantic validation is strict."""
        if "qwen" in self.model.lower() and messages:
            first = messages[0]
            first_content = str(first.get("content", ""))
            if first.get("role") == "user" and not first_content.startswith("/no_think"):
                messages = [{**first, "content": f"/no_think\n{first_content}"}, *messages[1:]]

        request_messages = _schema_messages(messages, response_model)
        key = _cache_key(self.base_url, self.model, request_messages, temperature, max_tokens, response_model)
        cached = _cache_get(key)
        if cached is not None:
            _record_cache_hit()
            _json_schema_response(cached, response_model)
            return cached

        prompt_chars = sum(len(str(message.get("content", ""))) for message in request_messages)
        retry_policy = RetryPolicy(
            max_attempts=_MAX_RETRIES,
            base_delay=_RATE_LIMIT_BASE_WAIT,
            max_delay=60.0,
            jitter_ratio=0.25,
        )
        primary: Provider = (self.base_url, self.model, self.api_key)

        for attempt in range(_MAX_RETRIES):
            started = time.perf_counter()
            try:
                output = self._request_sync(
                    primary,
                    request_messages,
                    temperature,
                    max_tokens,
                    response_model,
                    native_gemini=self._use_native_gemini,
                )
                # Bill the call before validating: a response that burns tokens and
                # then fails schema validation is still spend (structured_chat retries it).
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                _record_telemetry(self.model, prompt_chars, len(output), elapsed_ms)
                _json_schema_response(output, response_model)
                _cache_put(key, output)
                return output
            except _GeminiCompatForbidden:
                log.warning(
                    "Gemini compat endpoint returned 403 for model '%s'; switching to native generateContent.",
                    self.model,
                )
                self._use_native_gemini = True
                try:
                    output = self._request_sync(
                        primary,
                        request_messages,
                        temperature,
                        max_tokens,
                        response_model,
                        native_gemini=True,
                    )
                    elapsed_ms = int((time.perf_counter() - started) * 1000)
                    _record_telemetry(self.model, prompt_chars, len(output), elapsed_ms)
                    _json_schema_response(output, response_model)
                    _cache_put(key, output)
                    return output
                except (httpx.HTTPError, ValueError, KeyError, _GeminiCompatForbidden) as native_exc:
                    log.warning("Gemini native endpoint failed: %s", native_exc)
                    fallback = self._try_fallbacks(request_messages, temperature, max_tokens, response_model)
                    if fallback is not None:
                        output, fallback_model, fallback_ms = fallback
                        _record_telemetry(
                            fallback_model,
                            prompt_chars,
                            len(output),
                            fallback_ms,
                            failover=True,
                        )
                        _json_schema_response(output, response_model)
                        _cache_put(key, output)
                        return output
                    raise RuntimeError(f"Gemini compat and native endpoints failed: {native_exc}") from native_exc
            except httpx.HTTPStatusError as exc:
                response = exc.response
                if response.status_code in _TRANSIENT_STATUS_CODES and self.fallbacks:
                    fallback = self._try_fallbacks(request_messages, temperature, max_tokens, response_model)
                    if fallback is not None:
                        output, fallback_model, fallback_ms = fallback
                        _json_schema_response(output, response_model)
                        _record_telemetry(
                            fallback_model,
                            prompt_chars,
                            len(output),
                            fallback_ms,
                            failover=True,
                        )
                        _cache_put(key, output)
                        return output
                if response.status_code not in _TRANSIENT_STATUS_CODES or attempt + 1 >= _MAX_RETRIES:
                    raise
                wait = retry_policy.delay(attempt, response.headers)
                log.warning(
                    "LLM HTTP %d; retry %d/%d in %.2fs.",
                    response.status_code,
                    attempt + 1,
                    _MAX_RETRIES - 1,
                    wait,
                )
                time.sleep(wait)
            except httpx.TransportError as exc:
                if self.fallbacks:
                    fallback = self._try_fallbacks(request_messages, temperature, max_tokens, response_model)
                    if fallback is not None:
                        output, fallback_model, fallback_ms = fallback
                        _json_schema_response(output, response_model)
                        _record_telemetry(
                            fallback_model,
                            prompt_chars,
                            len(output),
                            fallback_ms,
                            failover=True,
                        )
                        _cache_put(key, output)
                        return output
                if attempt + 1 >= _MAX_RETRIES:
                    raise
                wait = retry_policy.delay(attempt)
                log.warning("LLM transport failure (%s); retrying in %.2fs.", type(exc).__name__, wait)
                time.sleep(wait)
            except (ValueError, json.JSONDecodeError) as exc:
                # Response parsing produced no usable completion ("no completion
                # choices", "empty or non-text completion", undecodable JSON).
                # These are transient from the caller's perspective, so a configured
                # fallback runs first and only then an in-place retry.
                if isinstance(exc, ValidationError):
                    raise  # schema mismatch: structured_chat re-prompts with the errors
                if self.fallbacks:
                    fallback = self._try_fallbacks(request_messages, temperature, max_tokens, response_model)
                    if fallback is not None:
                        output, fallback_model, fallback_ms = fallback
                        _json_schema_response(output, response_model)
                        _record_telemetry(
                            fallback_model,
                            prompt_chars,
                            len(output),
                            fallback_ms,
                            failover=True,
                        )
                        _cache_put(key, output)
                        return output
                if attempt + 1 >= _MAX_RETRIES:
                    raise
                wait = retry_policy.delay(attempt)
                log.warning("LLM unusable response (%s); retrying in %.2fs.", exc, wait)
                time.sleep(wait)

        raise RuntimeError("LLM request failed after all retries")

    async def achat(
        self,
        messages: list[Message],
        temperature: float = 0.0,
        max_tokens: int = 4096,
        response_model: type[BaseModel] | None = None,
    ) -> str:
        """Async chat API; retry waits use `asyncio.sleep`, never block the event loop."""
        loop = asyncio.get_running_loop()
        if self._async_session_owned and self._async_loop is loop and self._async_client is not None:
            return await self._achat_with_client(
                self._async_client, messages, temperature, max_tokens, response_model
            )

        async with build_async_client(
            timeout=_TIMEOUT,
            max_connections=_HTTP_LIMITS.max_connections or 100,
            max_keepalive_connections=_HTTP_LIMITS.max_keepalive_connections or 20,
            headers={"User-Agent": "ApplyPilot/0.5"},
            transport=self._async_transport,
        ) as client:
            return await self._achat_with_client(client, messages, temperature, max_tokens, response_model)

    async def _achat_with_client(
        self,
        client: httpx.AsyncClient,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        response_model: type[BaseModel] | None,
    ) -> str:
        """Perform an async completion using a caller-owned pooled client."""
        if "qwen" in self.model.lower() and messages:
            first = messages[0]
            first_content = str(first.get("content", ""))
            if first.get("role") == "user" and not first_content.startswith("/no_think"):
                messages = [{**first, "content": f"/no_think\n{first_content}"}, *messages[1:]]

        request_messages = _schema_messages(messages, response_model)
        key = _cache_key(self.base_url, self.model, request_messages, temperature, max_tokens, response_model)
        cached = _cache_get(key)
        if cached is not None:
            _record_cache_hit()
            _json_schema_response(cached, response_model)
            return cached

        prompt_chars = sum(len(str(message.get("content", ""))) for message in request_messages)
        retry_policy = RetryPolicy(
            max_attempts=_MAX_RETRIES,
            base_delay=_RATE_LIMIT_BASE_WAIT,
            max_delay=60.0,
            jitter_ratio=0.25,
        )
        primary: Provider = (self.base_url, self.model, self.api_key)

        for attempt in range(_MAX_RETRIES):
            started = time.perf_counter()
            try:
                output = await self._request_async(
                    client,
                    primary,
                    request_messages,
                    temperature,
                    max_tokens,
                    response_model,
                    native_gemini=self._use_native_gemini,
                )
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                _record_telemetry(self.model, prompt_chars, len(output), elapsed_ms)
                _json_schema_response(output, response_model)
                _cache_put(key, output)
                return output
            except _GeminiCompatForbidden:
                log.warning(
                    "Gemini compat endpoint returned 403 for model '%s'; switching to native generateContent.",
                    self.model,
                )
                self._use_native_gemini = True
                try:
                    output = await self._request_async(
                        client,
                        primary,
                        request_messages,
                        temperature,
                        max_tokens,
                        response_model,
                        native_gemini=True,
                    )
                    elapsed_ms = int((time.perf_counter() - started) * 1000)
                    _record_telemetry(self.model, prompt_chars, len(output), elapsed_ms)
                    _json_schema_response(output, response_model)
                    _cache_put(key, output)
                    return output
                except (httpx.HTTPError, ValueError, KeyError, _GeminiCompatForbidden) as native_exc:
                    log.warning("Gemini native endpoint failed: %s", native_exc)
                    fallback = await self._try_fallbacks_async(
                        client, request_messages, temperature, max_tokens, response_model
                    )
                    if fallback is not None:
                        output, fallback_model, fallback_ms = fallback
                        _json_schema_response(output, response_model)
                        _record_telemetry(
                            fallback_model,
                            prompt_chars,
                            len(output),
                            fallback_ms,
                            failover=True,
                        )
                        _cache_put(key, output)
                        return output
                    raise RuntimeError(f"Gemini compat and native endpoints failed: {native_exc}") from native_exc
            except httpx.HTTPStatusError as exc:
                response = exc.response
                if response.status_code in _TRANSIENT_STATUS_CODES and self.fallbacks:
                    fallback = await self._try_fallbacks_async(
                        client, request_messages, temperature, max_tokens, response_model
                    )
                    if fallback is not None:
                        output, fallback_model, fallback_ms = fallback
                        _json_schema_response(output, response_model)
                        _record_telemetry(
                            fallback_model,
                            prompt_chars,
                            len(output),
                            fallback_ms,
                            failover=True,
                        )
                        _cache_put(key, output)
                        return output
                if response.status_code not in _TRANSIENT_STATUS_CODES or attempt + 1 >= _MAX_RETRIES:
                    raise
                wait = retry_policy.delay(attempt, response.headers)
                log.warning("LLM HTTP %d; async retry %d/%d in %.2fs.", response.status_code, attempt + 1, _MAX_RETRIES - 1, wait)
                await asyncio.sleep(wait)
            except httpx.TransportError as exc:
                if self.fallbacks:
                    fallback = await self._try_fallbacks_async(
                        client, request_messages, temperature, max_tokens, response_model
                    )
                    if fallback is not None:
                        output, fallback_model, fallback_ms = fallback
                        _json_schema_response(output, response_model)
                        _record_telemetry(
                            fallback_model,
                            prompt_chars,
                            len(output),
                            fallback_ms,
                            failover=True,
                        )
                        _cache_put(key, output)
                        return output
                if attempt + 1 >= _MAX_RETRIES:
                    raise
                wait = retry_policy.delay(attempt)
                log.warning("LLM async transport failure (%s); retrying in %.2fs.", type(exc).__name__, wait)
                await asyncio.sleep(wait)
            except (ValueError, json.JSONDecodeError) as exc:
                # See the sync path: unusable responses are transient, but a schema
                # mismatch belongs to astructured_chat's corrective retry.
                if isinstance(exc, ValidationError):
                    raise
                if self.fallbacks:
                    fallback = await self._try_fallbacks_async(
                        client, request_messages, temperature, max_tokens, response_model
                    )
                    if fallback is not None:
                        output, fallback_model, fallback_ms = fallback
                        _json_schema_response(output, response_model)
                        _record_telemetry(
                            fallback_model,
                            prompt_chars,
                            len(output),
                            fallback_ms,
                            failover=True,
                        )
                        _cache_put(key, output)
                        return output
                if attempt + 1 >= _MAX_RETRIES:
                    raise
                wait = retry_policy.delay(attempt)
                log.warning("LLM unusable async response (%s); retrying in %.2fs.", exc, wait)
                await asyncio.sleep(wait)

        raise RuntimeError("LLM request failed after all retries")

    async def __aenter__(self) -> LLMClient:
        """Open a reusable async HTTP connection pool for a batch of requests."""
        loop = asyncio.get_running_loop()
        if self._async_client is not None:
            if self._async_loop is loop and self._async_session_owned:
                raise RuntimeError("LLMClient async context is already open")
            raise RuntimeError("Close the existing LLMClient async context before reopening it")
        self._async_client = build_async_client(
            timeout=_TIMEOUT,
            max_connections=_HTTP_LIMITS.max_connections or 100,
            max_keepalive_connections=_HTTP_LIMITS.max_keepalive_connections or 20,
            headers={"User-Agent": "ApplyPilot/0.5"},
            transport=self._async_transport,
        )
        await self._async_client.__aenter__()
        self._async_loop = loop
        self._async_session_owned = True
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the async pool; call `close()` separately to release the sync client."""
        client, self._async_client = self._async_client, None
        self._async_loop = None
        self._async_session_owned = False
        if client is not None:
            await client.aclose()

    def structured_chat(
        self,
        messages: list[Message],
        response_model: type[ResponseModel],
        temperature: float = 0.0,
        max_tokens: int = 4096,
        validation_retries: int = 1,
    ) -> ResponseModel:
        """Return a Pydantic-validated response, retrying once with validation feedback."""
        prepared = [dict(message) for message in messages]
        for attempt in range(validation_retries + 1):
            try:
                raw = self.chat(prepared, temperature, max_tokens, response_model=response_model)
                return response_model.model_validate_json(raw)
            except ValidationError as exc:
                if attempt >= validation_retries:
                    raise StructuredOutputError(
                        f"LLM response failed {response_model.__name__} validation: {exc}"
                    ) from exc
                prepared = [
                    *prepared,
                    {
                        "role": "user",
                        "content": (
                            "Your previous response did not match the required schema. Return a corrected JSON object only. "
                            f"Validation errors: {exc}"
                        ),
                    },
                ]
        raise RuntimeError("Structured response retry loop exited unexpectedly")

    async def astructured_chat(
        self,
        messages: list[Message],
        response_model: type[ResponseModel],
        temperature: float = 0.0,
        max_tokens: int = 4096,
        validation_retries: int = 1,
    ) -> ResponseModel:
        """Async counterpart of `structured_chat`, with Pydantic validation."""
        prepared = [dict(message) for message in messages]
        for attempt in range(validation_retries + 1):
            try:
                raw = await self.achat(prepared, temperature, max_tokens, response_model=response_model)
                return response_model.model_validate_json(raw)
            except ValidationError as exc:
                if attempt >= validation_retries:
                    raise StructuredOutputError(
                        f"LLM response failed {response_model.__name__} validation: {exc}"
                    ) from exc
                prepared = [
                    *prepared,
                    {
                        "role": "user",
                        "content": (
                            "Your previous response did not match the required schema. Return a corrected JSON object only. "
                            f"Validation errors: {exc}"
                        ),
                    },
                ]
        raise RuntimeError("Async structured response retry loop exited unexpectedly")

    def ask(self, prompt: str, **kwargs: Any) -> str:
        """Convenience method for one user message."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def ask_structured(
        self,
        prompt: str,
        response_model: type[ResponseModel],
        **kwargs: Any,
    ) -> ResponseModel:
        """Synchronous convenience method for validated JSON responses."""
        return self.structured_chat(
            [{"role": "user", "content": prompt}], response_model=response_model, **kwargs
        )

    async def aask(self, prompt: str, **kwargs: Any) -> str:
        """Async convenience method for one user message."""
        return await self.achat([{"role": "user", "content": prompt}], **kwargs)

    async def aask_structured(
        self,
        prompt: str,
        response_model: type[ResponseModel],
        **kwargs: Any,
    ) -> ResponseModel:
        return await self.astructured_chat(
            [{"role": "user", "content": prompt}], response_model=response_model, **kwargs
        )

    def close(self) -> None:
        """Close the synchronous pool; use `await aclose()` for the async pool."""
        with self._sync_client_lock:
            client, self._client = self._client, None
        if client is not None:
            client.close()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: LLMClient | None = None
_instance_lock = threading.Lock()


def get_client() -> LLMClient:
    """Return (or create) the module-level pooled LLMClient singleton."""
    global _instance
    if _instance is not None:
        return _instance
    with _instance_lock:
        if _instance is None:
            providers = _detect_all_providers()
            if not providers:
                _detect_provider()
            base_url, model, api_key = providers[0]
            fallbacks = providers[1:]
            log.info("LLM provider: %s model: %s (fallbacks: %d)", base_url, model, len(fallbacks))
            _instance = LLMClient(base_url, model, api_key, fallbacks=fallbacks)
        return _instance
