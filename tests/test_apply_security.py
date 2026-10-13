"""Security regressions for the autonomous apply agent (audit doc section 2.3).

Covers:
- the Claude Code built-in tools the unattended agent must never hold;
- the CapSolver credential no longer being interpolated into page JavaScript;
- the CAPTCHA prompt JavaScript actually being valid (it shipped doubled braces);
- the server-side CapSolver client, validated through the official MCP client.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent

from applypilot import config
from applypilot.apply import capsolver
from applypilot.apply import prompt as prompt_mod
from applypilot.apply.launcher import (
    _DISALLOWED_BUILTIN_TOOLS,
    _build_claude_command,
    _make_mcp_config,
)

STUB_KEY = "capsolver-secret-key"


class _StubHandler(BaseHTTPRequestHandler):
    """Minimal CapSolver stand-in used to keep the tests fully offline."""

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        server: _StubCapSolver = self.server  # type: ignore[assignment]
        server.requests.append((self.path, body))

        if server.error_mode:
            payload: dict[str, Any] = {
                "errorId": 1,
                "errorCode": "ERROR_KEY_DENIED",
                "errorDescription": "Invalid key",
            }
        elif self.path.endswith("/createTask"):
            payload = {"errorId": 0, "taskId": "task-123"}
        else:
            payload = {"errorId": 0, "status": "ready", "solution": {"gRecaptchaResponse": "tok-abc"}}

        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return


class _StubCapSolver(ThreadingHTTPServer):
    requests: list[tuple[str, dict[str, Any]]]
    error_mode: bool

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


def _tool_text(result: CallToolResult) -> str:
    """Return the text of a single-block MCP tool result."""
    block = result.content[0]
    assert isinstance(block, TextContent), f"unexpected content block: {type(block)!r}"
    return block.text


@pytest.fixture()
def capsolver_stub() -> Iterator[_StubCapSolver]:
    server = _StubCapSolver(("127.0.0.1", 0), _StubHandler)
    server.requests = []
    server.error_mode = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point ApplyPilot at a throwaway workspace and skip ambient .env files."""
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    monkeypatch.setattr(config, "load_env", lambda: None)
    try:
        yield
    finally:
        config.set_active_profile(None)


# ---------------------------------------------------------------------------
# Claude Code tool restrictions (audit 2.3.1)
# ---------------------------------------------------------------------------


def test_claude_command_blocks_shell_file_and_web_tools() -> None:
    cmd = _build_claude_command("sonnet", Path("mcp.json"))

    assert cmd[0] == "claude"
    assert cmd[cmd.index("--mcp-config") + 1] == "mcp.json"
    # Unattended operation still requires bypassPermissions; the safety boundary
    # is the disallowed-tool list.
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"

    disallowed = set(cmd[cmd.index("--disallowedTools") + 1].split(","))
    assert set(_DISALLOWED_BUILTIN_TOOLS) <= disallowed
    for tool in ("Bash", "Write", "Edit", "Read", "WebFetch", "WebSearch"):
        assert tool in disallowed, f"{tool} must not be available to the apply agent"


def test_mcp_config_registers_capsolver_only_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "load_env", lambda: None)

    monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
    without_key = _make_mcp_config(9222)
    assert set(without_key["mcpServers"]) == {"playwright", "gmail"}

    monkeypatch.setenv("CAPSOLVER_API_KEY", STUB_KEY)
    with_key = _make_mcp_config(9222)
    assert "capsolver" in with_key["mcpServers"]
    server = with_key["mcpServers"]["capsolver"]
    assert server["args"] == ["-m", "applypilot.apply.capsolver_mcp"]
    # The credential must never be written into a per-worker config file.
    assert STUB_KEY not in json.dumps(with_key)


# ---------------------------------------------------------------------------
# CAPTCHA prompt: no credential, and valid JavaScript (audit 2.3.2)
# ---------------------------------------------------------------------------


def test_captcha_prompt_excludes_key_and_ships_valid_javascript(
    isolated_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAPSOLVER_API_KEY", STUB_KEY)
    section = prompt_mod._build_captcha_section()

    # No credential anywhere in the agent instructions.
    assert STUB_KEY not in section
    assert "clientKey" not in section
    assert "fetch('https://api.capsolver.com" not in section

    # The page JavaScript must be valid: doubled braces used to render into the
    # prompt because the section is an f-string.
    assert "{{" not in section
    assert "() => {" in section
    assert "() => {{" not in section

    # Solving is delegated to the server-side MCP tools.
    assert "mcp__capsolver__solve_captcha" in section
    assert "Status: configured" in section


def test_captcha_prompt_reports_missing_key(
    isolated_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
    section = prompt_mod._build_captcha_section()
    assert "NOT CONFIGURED" in section
    assert "mcp__capsolver__solve_captcha" in section


# ---------------------------------------------------------------------------
# Server-side CapSolver client
# ---------------------------------------------------------------------------


def test_capsolver_client_sends_key_server_side_only(
    capsolver_stub: _StubCapSolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAPSOLVER_API_BASE", capsolver_stub.base_url)
    monkeypatch.setenv("CAPSOLVER_API_KEY", STUB_KEY)

    assert capsolver.is_configured()
    task_id = capsolver.create_task("hcaptcha", "https://jobs.test/1", "sitekey-1")
    assert task_id == "task-123"

    solved = capsolver.solve("recaptchav2", "https://jobs.test/1", "sitekey-1", poll_interval=0.0)
    assert solved["token"] == "tok-abc"
    assert solved["status"] == "ready"

    # The key travels in the server-side request body.
    create_body = next(body for path, body in capsolver_stub.requests if path.endswith("/createTask"))
    assert create_body["clientKey"] == STUB_KEY


def test_capsolver_client_raises_on_api_error(
    capsolver_stub: _StubCapSolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAPSOLVER_API_BASE", capsolver_stub.base_url)
    monkeypatch.setenv("CAPSOLVER_API_KEY", STUB_KEY)
    capsolver_stub.error_mode = True

    with pytest.raises(capsolver.CapSolverError, match="Invalid key"):
        capsolver.create_task("hcaptcha", "https://jobs.test/1", "sitekey-1")


def test_capsolver_requires_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
    assert not capsolver.is_configured()
    with pytest.raises(capsolver.CapSolverError, match="not configured"):
        capsolver.create_task("hcaptcha", "https://jobs.test/1", "sitekey-1")


def test_capsolver_maps_captcha_types() -> None:
    assert capsolver.resolve_task_type("hcaptcha") == "HCaptchaTaskProxyLess"
    assert capsolver.resolve_task_type("turnstile") == "AntiTurnstileTaskProxyLess"
    # Already-resolved task types pass through unchanged.
    assert capsolver.resolve_task_type("ReCaptchaV2TaskProxyLess") == "ReCaptchaV2TaskProxyLess"
    with pytest.raises(capsolver.CapSolverError):
        capsolver.resolve_task_type("")


# ---------------------------------------------------------------------------
# MCP server protocol, validated with the official client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capsolver_mcp_server_serves_tools_without_leaking_key(
    capsolver_stub: _StubCapSolver,
) -> None:
    env = {
        **os.environ,
        "CAPSOLVER_API_BASE": capsolver_stub.base_url,
        "CAPSOLVER_API_KEY": STUB_KEY,
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "applypilot.apply.capsolver_mcp"],
        env=env,
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {"solve_captcha", "create_task", "get_task_result", "status"} <= names

            solved = await session.call_tool(
                "solve_captcha",
                {
                    "captcha_type": "hcaptcha",
                    "website_url": "https://jobs.test/1",
                    "website_key": "sitekey-1",
                },
            )
            solved_text = _tool_text(solved)
            assert solved.is_error is False
            assert json.loads(solved_text)["token"] == "tok-abc"
            # Only the solved token crosses the boundary, never the API key.
            assert STUB_KEY not in solved_text

            status_text = _tool_text(await session.call_tool("status", {}))
            assert json.loads(status_text)["configured"] is True
            assert STUB_KEY not in status_text
