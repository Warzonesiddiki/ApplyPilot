"""Minimal stdio MCP server exposing the CapSolver tools to the apply agent.

Implemented against the MCP stdio transport using only the standard library, so
ApplyPilot gains no extra runtime dependency. Running the CapSolver calls here
(rather than inside ``browser_evaluate``) keeps ``CAPSOLVER_API_KEY`` out of the
untrusted job page: only the solved token is handed back to the browser.

Usage (registered automatically in the per-worker MCP config when a key is set)::

    python -m applypilot.apply.capsolver_mcp
"""

from __future__ import annotations

import json
import sys
from typing import Any

from applypilot import __version__
from applypilot.apply import capsolver

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "applypilot-capsolver"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "solve_captcha",
        "description": (
            "Create a CapSolver task and poll until it is solved, returning the token to inject "
            "into the page. The CapSolver API key stays server-side."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "captcha_type": {
                    "type": "string",
                    "description": "hcaptcha, recaptchav2, recaptchav3, turnstile, or funcaptcha",
                },
                "website_url": {"type": "string", "description": "URL of the page with the captcha"},
                "website_key": {"type": "string", "description": "sitekey reported by the page detector"},
                "extra": {
                    "type": "object",
                    "description": "Optional CapSolver task extras, e.g. {'pageAction': 'submit'}",
                },
            },
            "required": ["captcha_type", "website_url", "website_key"],
        },
    },
    {
        "name": "create_task",
        "description": "Create a CapSolver task only (advanced: manual polling). Returns the taskId.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "captcha_type": {"type": "string"},
                "website_url": {"type": "string"},
                "website_key": {"type": "string"},
                "extra": {"type": "object"},
            },
            "required": ["captcha_type", "website_url", "website_key"],
        },
    },
    {
        "name": "get_task_result",
        "description": "Fetch the raw CapSolver result for a taskId created by create_task.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "status",
        "description": "Report whether CapSolver is configured. Never returns the API key.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute one tool call and return an MCP tool result payload."""
    if name == "status":
        return _text_result({"configured": capsolver.is_configured(), "api_base": capsolver.api_base()})

    if name == "solve_captcha":
        result = capsolver.solve(
            str(arguments.get("captcha_type", "")),
            str(arguments.get("website_url", "")),
            str(arguments.get("website_key", "")),
            extra=_optional_mapping(arguments.get("extra")),
        )
        return _text_result(result)

    if name == "create_task":
        task_id = capsolver.create_task(
            str(arguments.get("captcha_type", "")),
            str(arguments.get("website_url", "")),
            str(arguments.get("website_key", "")),
            extra=_optional_mapping(arguments.get("extra")),
        )
        return _text_result({"task_id": task_id})

    if name == "get_task_result":
        return _text_result(capsolver.get_task_result(str(arguments.get("task_id", ""))))

    raise ValueError(f"Unknown tool: {name}")


def _optional_mapping(value: Any) -> dict[str, Any] | None:
    """Return ``value`` when it is a mapping, otherwise None."""
    return value if isinstance(value, dict) else None


def _text_result(payload: Any) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": False,
    }


def _error_result(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def handle_message(message: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC message; return a response, or None for notifications."""
    method = message.get("method")
    message_id = message.get("id")

    # Notifications carry no id and must never be answered.
    if message_id is None:
        return None

    if method == "initialize":
        params = message.get("params") or {}
        requested = params.get("protocolVersion")
        return _result(message_id, {
            "protocolVersion": requested if isinstance(requested, str) and requested else PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
        })

    if method == "ping":
        return _result(message_id, {})

    if method == "tools/list":
        return _result(message_id, {"tools": TOOLS})

    if method == "tools/call":
        params = message.get("params") or {}
        name = str(params.get("name", ""))
        arguments = params.get("arguments")
        try:
            result = _tool_call(name, arguments if isinstance(arguments, dict) else {})
        except capsolver.CapSolverError as exc:
            return _result(message_id, _error_result(str(exc)))
        except ValueError as exc:
            return _error(message_id, -32602, str(exc))
        except Exception as exc:  # pragma: no cover - unexpected transport/API failure
            return _result(message_id, _error_result(f"{type(exc).__name__}: {exc}"))
        return _result(message_id, result)

    return _error(message_id, -32601, f"Method not found: {method}")


def _result(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def main() -> None:
    """Serve the MCP stdio transport until stdin closes."""
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue

        response = handle_message(message)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
