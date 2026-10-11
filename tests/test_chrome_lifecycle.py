"""Chrome worker lifecycle helpers: profiles, preferences, and process cleanup."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import applypilot.apply.chrome as chrome
import applypilot.config as config

from applypilot.apply.chrome import (
    BASE_CDP_PORT,
    cleanup_on_exit,
    cleanup_worker,
    kill_all_chrome,
    reset_worker_dir,
    setup_worker_profile,
    _kill_on_port,
    _suppress_restore_nag,
)


@pytest.fixture()
def chrome_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile("chrome-tests")
    config.ensure_dirs()
    yield tmp_path
    with chrome._chrome_lock:
        chrome._chrome_procs.clear()
    config.set_active_profile(None)


class _FakeSourceProfile:
    """Builder for a realistic Chrome user-data directory."""

    @staticmethod
    def build(root: Path) -> Path:
        source = root / "chrome-user-data"
        (source / "Default").mkdir(parents=True)
        (source / "Default" / "Preferences").write_text('{"profile": {"exit_type": "Crashed"}}', encoding="utf-8")
        (source / "Default" / "History").write_text("history", encoding="utf-8")
        (source / "Local State").write_text("{}", encoding="utf-8")
        (source / "Cache").mkdir()
        (source / "Cache" / "big_blob.bin").write_text("cache" * 1000, encoding="utf-8")
        (source / "GrShaderCache").mkdir()
        return source


def test_setup_worker_profile_clones_from_user_profile_skipping_caches(
    chrome_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _FakeSourceProfile.build(chrome_env)
    monkeypatch.setattr(config, "get_chrome_user_data", lambda: source)
    profile = setup_worker_profile(0)

    assert profile == config.CHROME_WORKER_DIR / "worker-0"
    assert (profile / "Default" / "Preferences").exists()
    assert (profile / "Local State").exists()
    assert not (profile / "Cache").exists()
    assert not (profile / "GrShaderCache").exists()

    # A second setup for the same worker reuses the existing profile untouched.
    marker = profile / "keep-me"
    marker.write_text("stale", encoding="utf-8")
    assert setup_worker_profile(0) == profile
    assert marker.exists()


def test_setup_worker_profile_prefers_an_existing_worker_profile(
    chrome_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _FakeSourceProfile.build(chrome_env)
    worker0 = config.CHROME_WORKER_DIR / "worker-0"
    worker0.mkdir(parents=True)
    (worker0 / "Default").mkdir()
    (worker0 / "Default" / "Cookies").write_text("session", encoding="utf-8")

    monkeypatch.setattr(config, "get_chrome_user_data", lambda: source)
    profile1 = setup_worker_profile(1)

    # worker-1 clones worker-0 (which has session cookies), not the raw user profile.
    assert (profile1 / "Default" / "Cookies").exists()


def test_suppress_restore_nag_patches_chrome_preferences(chrome_env: Path) -> None:
    profile_dir = config.CHROME_WORKER_DIR / "worker-0"
    prefs_file = profile_dir / "Default" / "Preferences"
    prefs_file.parent.mkdir(parents=True)
    prefs_file.write_text(
        json.dumps({"profile": {"exit_type": "Crashed"}, "session": {"restore_on_startup": 1, "startup_urls": ["https://x.test"]}}),
        encoding="utf-8",
    )

    _suppress_restore_nag(profile_dir)

    prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
    assert prefs["profile"]["exit_type"] == "Normal"
    assert prefs["session"]["restore_on_startup"] == 4
    assert "startup_urls" not in prefs["session"]
    assert prefs["credentials_enable_service"] is False
    assert prefs["password_manager"]["saving_enabled"] is False
    assert prefs["autofill"]["profile_enabled"] is False

    # Missing preferences file is a no-op.
    _suppress_restore_nag(config.CHROME_WORKER_DIR / "worker-9")
    assert not (config.CHROME_WORKER_DIR / "worker-9" / "Default" / "Preferences").exists()


def test_reset_worker_dir_wipes_and_recreates(chrome_env: Path) -> None:
    worker_dir = config.APPLY_WORKER_DIR / "worker-2"
    worker_dir.mkdir(parents=True)
    (worker_dir / "mcp-config.json").write_text("{}", encoding="utf-8")

    fresh = reset_worker_dir(2)

    assert fresh == worker_dir
    assert fresh.is_dir()
    assert list(fresh.iterdir()) == []

    # Works when the directory does not exist yet.
    assert reset_worker_dir(3) == config.APPLY_WORKER_DIR / "worker-3"


class _FakeChromeProcess:
    """Popen stand-in: reports running until killed."""

    def __init__(self) -> None:
        self.pid = 4242
        self._alive = True

    def poll(self) -> int | None:
        return None if self._alive else 0

    def kill_called(self) -> None:
        self._alive = False


@pytest.fixture()
def record_kills(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    killed: list[int] = []
    monkeypatch.setattr(chrome, "_kill_process_tree", lambda pid: killed.append(pid))
    return killed


def test_cleanup_worker_kills_running_process_and_untracks_it(record_kills: list[int]) -> None:
    proc = _FakeChromeProcess()
    with chrome._chrome_lock:
        chrome._chrome_procs[5] = proc  # type: ignore[assignment]

    cleanup_worker(5, proc)  # type: ignore[arg-type]

    assert record_kills == [proc.pid]
    assert 5 not in chrome._chrome_procs

    # An already-exited process is not killed again.
    record_kills.clear()
    exited = _FakeChromeProcess()
    exited.kill_called()
    cleanup_worker(6, exited)  # type: ignore[arg-type]
    assert record_kills == []


def test_kill_all_chrome_sweeps_processes_and_ports(
    monkeypatch: pytest.MonkeyPatch,
    record_kills: list[int],
) -> None:
    swept_ports: list[int] = []
    monkeypatch.setattr(chrome, "_kill_on_port", lambda port: swept_ports.append(port))

    running = _FakeChromeProcess()
    exited = _FakeChromeProcess()
    exited.kill_called()
    with chrome._chrome_lock:
        chrome._chrome_procs[1] = running  # type: ignore[assignment]
        chrome._chrome_procs[2] = exited  # type: ignore[assignment]

    kill_all_chrome()

    assert record_kills == [running.pid]
    assert swept_ports == [BASE_CDP_PORT + 1, BASE_CDP_PORT + 2, BASE_CDP_PORT]
    assert chrome._chrome_procs == {}


def test_cleanup_on_exit_matches_kill_all_behaviour(
    monkeypatch: pytest.MonkeyPatch,
    record_kills: list[int],
) -> None:
    swept_ports: list[int] = []
    monkeypatch.setattr(chrome, "_kill_on_port", lambda port: swept_ports.append(port))
    proc = _FakeChromeProcess()
    with chrome._chrome_lock:
        chrome._chrome_procs[0] = proc  # type: ignore[assignment]

    cleanup_on_exit()

    assert record_kills == [proc.pid]
    assert swept_ports == [BASE_CDP_PORT, BASE_CDP_PORT]  # worker port + base sweep
    assert chrome._chrome_procs == {}


def test_kill_on_port_parses_lsof_output_and_kills_listeners(
    monkeypatch: pytest.MonkeyPatch,
    record_kills: list[int],
) -> None:
    """Verify the non-Windows port-kill path parses lsof -ti output and kills
    every listed PID.

    The production helper ``_kill_on_port`` imports ``subprocess`` only under
    the non-Windows branch, so this test wires a fake ``subprocess`` module
    into the *same* namespace ``applypilot.apply.chrome`` uses at runtime.
    """
    class _FakeCompleted:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    class _FakeSubprocess:
        CompletedProcess: Any = _FakeCompleted
        run: Any = staticmethod(
            lambda cmd, **kwargs: _FakeCompleted(
                "1111\nnot-a-pid\n2222\n"
                if cmd[:2] == ["lsof", "-ti"] and len(cmd) > 2 and cmd[2] == ":9310"
                else ""
            )
        )

    import applypilot.apply.chrome as _chrome_under_test
    monkeypatch.setattr(_chrome_under_test, "subprocess", _FakeSubprocess)
    monkeypatch.setattr(
        _chrome_under_test, "_resolve_port_scan_tool", lambda: "lsof"
    )
    _kill_on_port(BASE_CDP_PORT + 88)
    assert record_kills == [1111, 2222]


def _assert_lsof_tool_is_scripted_for_port_kill_tests() -> None:
    """Safety check used by the audit: the lsof-parsing path must be the one
    under test, not the Windows netstat path."""
    assert chrome._resolve_port_scan_tool() == "lsof"

def test_kill_on_port_tolerates_missing_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeSubprocess:
        CompletedProcess: Any = subprocess.CompletedProcess
        run: Any = staticmethod(
            lambda cmd, **kwargs: (_ for _ in ()).throw(FileNotFoundError("lsof not installed"))
        )

    import applypilot.apply.chrome as _chrome_under_test
    monkeypatch.setattr(_chrome_under_test, "subprocess", _FakeSubprocess)
    monkeypatch.setattr(
        _chrome_under_test, "_resolve_port_scan_tool", lambda: "lsof"
    )
    _kill_on_port(12345)  # must not raise


def test_kill_process_tree_ignores_missing_pids() -> None:
    # A PID that does not exist exercises the ProcessLookupError fallbacks.
    chrome._kill_process_tree(999_999_999)
