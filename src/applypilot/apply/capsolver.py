"""CapSolver REST client.

These calls run inside ApplyPilot (server-side) rather than inside the untrusted
job page. Previously the ``CAPSOLVER_API_KEY`` was interpolated into
``browser_evaluate`` JavaScript and used in an in-page ``fetch``, which exposed
the credential to any script on the employer's page (see
``docs/01_AUDIT_AND_GAP_ANALYSIS.md`` section 2.3.2).
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable

import httpx

DEFAULT_API_BASE = "https://api.capsolver.com"
POLL_INTERVAL_SECONDS = 3.0
MAX_POLLS = 10
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Maps the captcha type reported by the agent's page detector to CapSolver's
# task type identifiers.
TASK_TYPES: dict[str, str] = {
    "hcaptcha": "HCaptchaTaskProxyLess",
    "recaptchav2": "ReCaptchaV2TaskProxyLess",
    "recaptchav3": "ReCaptchaV3TaskProxyLess",
    "turnstile": "AntiTurnstileTaskProxyLess",
    "funcaptcha": "FunCaptchaTaskProxyLess",
}


class CapSolverError(RuntimeError):
    """Raised when CapSolver is unconfigured, errors, or returns no usable token."""


def api_base() -> str:
    """Return the CapSolver API base URL (overridable in tests)."""
    return os.environ.get("CAPSOLVER_API_BASE", DEFAULT_API_BASE).rstrip("/")


def get_api_key() -> str:
    """Return the configured CapSolver API key, or raise."""
    key = os.environ.get("CAPSOLVER_API_KEY", "").strip()
    if not key:
        raise CapSolverError("CAPSOLVER_API_KEY is not configured")
    return key


def is_configured() -> bool:
    """Return True when a CapSolver API key is configured."""
    return bool(os.environ.get("CAPSOLVER_API_KEY", "").strip())


def resolve_task_type(captcha_type: str) -> str:
    """Map a detected captcha type (or raw CapSolver task type) to a task type."""
    value = (captcha_type or "").strip()
    if not value:
        raise CapSolverError("captcha_type is required")
    mapped = TASK_TYPES.get(value.lower())
    return mapped if mapped else value


def _raise_for_error(data: dict[str, Any], action: str) -> None:
    try:
        error_id = int(data.get("errorId", 0))
    except (TypeError, ValueError):
        error_id = 0
    if error_id != 0:
        detail = data.get("errorDescription") or data.get("errorCode") or "unknown error"
        raise CapSolverError(f"{action} failed: {detail}")


def create_task(
    task_type: str,
    website_url: str,
    website_key: str,
    *,
    extra: dict[str, Any] | None = None,
) -> str:
    """Create a CapSolver task and return its ``taskId``."""
    if not website_url or not website_key:
        raise CapSolverError("website_url and website_key are required")
    task: dict[str, Any] = {
        "type": resolve_task_type(task_type),
        "websiteURL": website_url,
        "websiteKey": website_key,
    }
    if extra:
        task.update(extra)
    payload = {"clientKey": get_api_key(), "task": task}

    response = httpx.post(f"{api_base()}/createTask", json=payload, timeout=_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    _raise_for_error(data, "createTask")

    task_id = data.get("taskId")
    if not isinstance(task_id, str) or not task_id:
        raise CapSolverError("createTask returned no taskId")
    return task_id


def get_task_result(task_id: str) -> dict[str, Any]:
    """Fetch a CapSolver task result and return the raw payload."""
    if not task_id:
        raise CapSolverError("task_id is required")
    payload = {"clientKey": get_api_key(), "taskId": task_id}

    response = httpx.post(f"{api_base()}/getTaskResult", json=payload, timeout=_TIMEOUT)
    response.raise_for_status()
    raw: Any = response.json()
    if not isinstance(raw, dict):
        raise CapSolverError("getTaskResult returned a non-object payload")
    _raise_for_error(raw, "getTaskResult")
    return dict(raw)


def extract_token(data: dict[str, Any]) -> str | None:
    """Pull the solved token out of a ``getTaskResult`` payload."""
    solution = data.get("solution")
    if not isinstance(solution, dict):
        return None
    for key in ("gRecaptchaResponse", "token", "fingerprint"):
        value = solution.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def solve(
    task_type: str,
    website_url: str,
    website_key: str,
    *,
    extra: dict[str, Any] | None = None,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    max_polls: int = MAX_POLLS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Create a task, poll until it is ready, and return the result payload.

    Returns:
        ``{"task_id": str, "token": str, "status": "ready"}``.

    Raises:
        CapSolverError: On API errors, timeout, or a missing solution token.
    """
    task_id = create_task(task_type, website_url, website_key, extra=extra)

    for _ in range(max(1, max_polls)):
        data = get_task_result(task_id)
        status = str(data.get("status") or "").lower()
        if status == "ready":
            token = extract_token(data)
            if not token:
                raise CapSolverError("task ready but no solution token was returned")
            return {"task_id": task_id, "token": token, "status": "ready"}
        sleep(max(0.0, poll_interval))

    raise CapSolverError(f"task {task_id} did not become ready within {max_polls} polls")
