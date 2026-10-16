"""Chrome lifecycle management for apply workers.

Handles launching an isolated Chrome instance with remote debugging,
worker profile setup/cloning, and cross-platform process cleanup.
"""

import json
import logging
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

# Unix process-group primitives live behind a platform-conditional import so
# they do not appear as missing attributes on Windows builds.

if sys.platform == "win32":
    _OS_UNIX: Any = None
    _SIGNAL_UNIX: Any = None
    _HAS_UNIX_PROCESS_API = False
else:
    import os as _os_unix
    import signal as _signal_unix

    _OS_UNIX = _os_unix
    _SIGNAL_UNIX = _signal_unix
    _HAS_UNIX_PROCESS_API = True

from applypilot import config

logger = logging.getLogger(__name__)

# CDP port base — each worker uses BASE_CDP_PORT + worker_id
BASE_CDP_PORT = 9222

# Track Chrome processes per worker for cleanup
_chrome_procs: dict[int, subprocess.Popen[bytes]] = {}
_chrome_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Cross-platform process helpers
# ---------------------------------------------------------------------------

def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its children.

    On Windows, Chrome spawns 10+ child processes (GPU, renderer, etc.),
    so taskkill /T is needed to kill the entire tree. On Unix, os.killpg
    handles the process group.
    """
    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            # Unix: kill entire process group
            if not _HAS_UNIX_PROCESS_API:
                raise RuntimeError("Unix process-group kill is only supported on POSIX")

            try:
                _OS_UNIX.killpg(_OS_UNIX.getpgid(pid), _SIGNAL_UNIX.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # Process already gone or owned by another user
                try:
                    _OS_UNIX.kill(pid, _SIGNAL_UNIX.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    except Exception:
        logger.debug("Failed to kill process tree for PID %d", pid, exc_info=True)


def _netstat_listener_pid(line: str, port: int) -> str | None:
    """Return the PID when a ``netstat -ano -p TCP`` line listens on ``port``.

    The local address is compared on an exact port boundary: a substring test for
    ``":9222"`` also matches ``:92220`` and the *remote* address of an outbound
    connection, so ``kill_all_chrome`` could terminate an unrelated process tree.
    Only loopback listeners are accepted -- Chrome's CDP endpoint is always local.
    """
    fields = line.split()
    if len(fields) < 5:
        return None
    local_address, state, pid = fields[1], fields[3], fields[-1]
    if state.upper() != "LISTENING" or not pid.isdigit():
        return None
    if ":" not in local_address:
        return None
    host, _, local_port = local_address.rpartition(":")
    if local_port != str(port):
        return None
    if host not in ("127.0.0.1", "0.0.0.0", "::", "[::1]", "localhost", ""):
        return None
    return pid


def _kill_on_port(port: int) -> None:
    """Kill any process listening on a specific port (zombie cleanup).

    Uses netstat on Windows, lsof on macOS/Linux.
    """
    tool = _get_port_scan_tool()
    try:
        if tool == "Windows":
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                pid = _netstat_listener_pid(line, port)
                if pid is not None:
                    _kill_process_tree(int(pid))
            return

        # macOS / Linux: parse ``lsof -ti :port`` output.
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=10,
        )
        for pid_str in result.stdout.strip().splitlines():
            pid_str = pid_str.strip()
            if pid_str.isdigit():
                _kill_process_tree(int(pid_str))
    except FileNotFoundError:
        logger.debug("Port-kill tool not found (netstat/lsof) for port %d", port)
    except Exception:
        logger.debug("Failed to kill process on port %d", port, exc_info=True)


def _resolve_port_scan_tool() -> str:
    """Return which external port-scanning tool this platform attempts first.

    Windows uses ``netstat``; macOS and Linux use ``lsof``. The value is
    used by the liveliness probe in ``_kill_on_port`` and by tests that
    script the port-kill path.
    """
    return "netstat" if platform.system() == "Windows" else "lsof"


_PORT_SCAN_TOOL: str | None = None


def _get_port_scan_tool() -> str:
    """Return the cached port-scanning tool name, resolving once per process.

    On Windows this is ``netstat``; on macOS and Linux it is ``lsof``.
    ``_kill_on_port`` uses this helper so non-Windows tests can script the
    lsof-parsing path without racing ``platform.system()`` at runtime.
    """
    global _PORT_SCAN_TOOL
    if _PORT_SCAN_TOOL is None:
        _PORT_SCAN_TOOL = _resolve_port_scan_tool()
    return _PORT_SCAN_TOOL


# ---------------------------------------------------------------------------
# Worker profile management
# ---------------------------------------------------------------------------

def setup_worker_profile(worker_id: int) -> Path:
    """Create an isolated Chrome profile for a worker.

    On first run, clones from an existing worker profile (preferred, since
    it already has session cookies) or from the user's real Chrome profile.
    Subsequent runs reuse the existing worker profile.

    Args:
        worker_id: Numeric worker identifier.

    Returns:
        Path to the worker's Chrome user-data directory.
    """
    profile_dir = config.CHROME_WORKER_DIR / f"worker-{worker_id}"
    if (profile_dir / "Default").exists():
        return profile_dir  # Already initialized

    # Find a source: prefer existing worker (has session cookies), else user profile
    source: Path | None = None
    for wid in range(10):
        if wid == worker_id:
            continue
        candidate = config.CHROME_WORKER_DIR / f"worker-{wid}"
        if (candidate / "Default").exists():
            source = candidate
            break
    if source is None:
        source = config.get_chrome_user_data()

    logger.info("[worker-%d] Copying Chrome profile from %s (first time setup)...",
                worker_id, source.name)
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Copy essential profile dirs -- skip caches and heavy transient data
    skip = {
        "ShaderCache", "GrShaderCache", "Service Worker", "Cache",
        "Code Cache", "GPUCache", "CacheStorage", "Crashpad",
        "BrowserMetrics", "SafeBrowsing", "Crowd Deny",
        "MEIPreload", "SSLErrorAssistant", "recovery", "Temp",
        "SingletonLock", "SingletonSocket", "SingletonCookie",
    }

    for item in source.iterdir():
        if item.name in skip:
            continue
        dst = profile_dir / item.name
        try:
            if item.is_dir():
                shutil.copytree(
                    str(item), str(dst), dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(
                        "Cache", "Code Cache", "GPUCache", "Service Worker",
                    ),
                )
            else:
                shutil.copy2(str(item), str(dst))
        except (PermissionError, OSError):
            pass  # skip locked files

    return profile_dir


def _suppress_restore_nag(profile_dir: Path) -> None:
    """Clear Chrome's 'restore pages' nag by fixing Preferences.

    Chrome writes exit_type=Crashed when killed, which triggers a
    'Restore pages?' prompt on next launch. This patches it out.
    """
    prefs_file = profile_dir / "Default" / "Preferences"
    if not prefs_file.exists():
        return

    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        prefs.setdefault("profile", {})["exit_type"] = "Normal"
        prefs.setdefault("session", {})["restore_on_startup"] = 4  # 4 = open blank
        prefs.setdefault("session", {}).pop("startup_urls", None)
        prefs["credentials_enable_service"] = False
        prefs.setdefault("password_manager", {})["saving_enabled"] = False
        prefs.setdefault("autofill", {})["profile_enabled"] = False
        prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
    except Exception:
        logger.debug("Could not patch Chrome preferences", exc_info=True)


# ---------------------------------------------------------------------------
# Chrome launch / kill
# ---------------------------------------------------------------------------

def launch_chrome(worker_id: int, port: int | None = None,
                  headless: bool = False) -> subprocess.Popen[bytes]:
    """Launch a Chrome instance with remote debugging for a worker.

    Args:
        worker_id: Numeric worker identifier.
        port: CDP port. Defaults to BASE_CDP_PORT + worker_id.
        headless: Run Chrome in headless mode (no visible window).

    Returns:
        subprocess.Popen handle for the Chrome process.
    """
    if port is None:
        port = BASE_CDP_PORT + worker_id

    profile_dir = setup_worker_profile(worker_id)

    # Kill any zombie Chrome from a previous run on this port
    _kill_on_port(port)

    # Patch preferences to suppress restore nag
    _suppress_restore_nag(profile_dir)

    chrome_exe = config.get_chrome_path()

    cmd = [
        chrome_exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1024,768",
        "--disable-session-crashed-bubble",
        "--disable-features=InfiniteSessionRestore,PasswordManagerOnboarding",
        "--hide-crash-restore-bubble",
        "--noerrdialogs",
        "--password-store=basic",
        "--disable-save-password-bubble",
        "--disable-popup-blocking",
        # Block dangerous permissions at browser level
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream",
        "--deny-permission-prompts",
        "--disable-notifications",
    ]
    if headless:
        cmd.append("--headless=new")

    # On Unix, start in a new process group so we can kill the whole tree
    kwargs: dict[str, Any] = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if platform.system() != "Windows":
        if not _HAS_UNIX_PROCESS_API:
            raise RuntimeError("Unix process-group launch is only supported on POSIX")
        kwargs["preexec_fn"] = _OS_UNIX.setsid

    proc = subprocess.Popen[bytes](cmd, **kwargs)
    with _chrome_lock:
        _chrome_procs[worker_id] = proc

    # Give Chrome time to start and open the debug port
    time.sleep(3)
    logger.info("[worker-%d] Chrome started on port %d (pid %d)",
                worker_id, port, proc.pid)
    return proc


def cleanup_worker(worker_id: int, process: subprocess.Popen[bytes] | None) -> None:
    """Kill a worker's Chrome instance and remove it from tracking.

    Args:
        worker_id: Numeric worker identifier.
        process: The Popen handle returned by launch_chrome.
    """
    if process and process.poll() is None:
        _kill_process_tree(process.pid)
    with _chrome_lock:
        _chrome_procs.pop(worker_id, None)
    logger.info("[worker-%d] Chrome cleaned up", worker_id)


def kill_all_chrome() -> None:
    """Kill all Chrome instances and any port zombies.

    Called during graceful shutdown to ensure no orphan Chrome processes.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _kill_on_port(BASE_CDP_PORT + wid)

    # Sweep base port in case of zombies
    _kill_on_port(BASE_CDP_PORT)


def reset_worker_dir(worker_id: int) -> Path:
    """Wipe and recreate a worker's isolated working directory.

    Each job gets a fresh working directory so that file conflicts
    (resume PDFs, MCP configs) don't bleed between jobs.

    Args:
        worker_id: Numeric worker identifier.

    Returns:
        Path to the clean worker directory.
    """
    worker_dir = config.APPLY_WORKER_DIR / f"worker-{worker_id}"
    if worker_dir.exists():
        shutil.rmtree(str(worker_dir), ignore_errors=True)
    worker_dir.mkdir(parents=True, exist_ok=True)
    return worker_dir


def cleanup_on_exit() -> None:
    """Atexit handler: kill all Chrome processes and sweep CDP ports.

    Register this with atexit.register() at application startup.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _kill_on_port(BASE_CDP_PORT + wid)

    # Sweep base port for any orphan
    _kill_on_port(BASE_CDP_PORT)
