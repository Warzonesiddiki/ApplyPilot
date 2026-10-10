"""Pipeline stage resolution, dispatch, and dry-run behaviour."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import applypilot.config as config
import applypilot.database as database
import applypilot.pipeline as pipeline
from applypilot.pipeline import (
    STAGE_ORDER,
    _StageTracker,
    _resolve_stages,
    _run_sequential,
    run_pipeline,
)


@pytest.fixture()
def pipeline_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    config.set_active_profile("pipeline-tests")
    yield None
    database.close_connection()
    config.set_active_profile(None)


def test_resolve_stages_expands_all_orders_and_deduplicates() -> None:
    assert _resolve_stages(["all"]) == list(STAGE_ORDER)
    assert _resolve_stages([]) == []
    # Canonical order is enforced and duplicates collapse.
    assert _resolve_stages(["pdf", "tailor", "discover", "tailor"]) == ["discover", "tailor", "pdf"]


def test_resolve_stages_rejects_unknown_names() -> None:
    with pytest.raises(SystemExit) as excinfo:
        _resolve_stages(["discover", "teleport"])
    assert excinfo.value.code == 1


def test_stage_tracker_is_thread_safe() -> None:
    tracker = _StageTracker()
    assert tracker.is_done("enrich") is False
    assert tracker.wait("score", timeout=0.01) is False

    def mark() -> None:
        tracker.mark_done("enrich", {"status": "ok"})

    thread = threading.Thread(target=mark)
    thread.start()
    thread.join()
    assert tracker.is_done("enrich") is True
    assert tracker.wait("enrich", timeout=1.0) is True
    assert tracker.get_results() == {"enrich": {"status": "ok"}}


def test_run_sequential_dispatches_stage_specific_kwargs(
    pipeline_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, dict[str, Any]] = {}

    def fake_discover(**kwargs: Any) -> dict[str, Any]:
        calls["discover"] = kwargs
        return {"status": "ok"}

    def fake_enrich(**kwargs: Any) -> dict[str, Any]:
        calls["enrich"] = kwargs
        return {"status": "ok"}

    def fake_tailor(**kwargs: Any) -> dict[str, Any]:
        calls["tailor"] = kwargs
        return {"status": "ok"}

    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "discover", fake_discover)
    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "enrich", fake_enrich)
    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "tailor", fake_tailor)

    result = _run_sequential(
        ["discover", "enrich", "tailor"],
        min_score=8,
        workers=3,
        validation_mode="strict",
        sources=["workday"],
        limit=12,
        deduplicate=False,
    )

    assert [entry["stage"] for entry in result["stages"]] == ["discover", "enrich", "tailor"]
    assert all(entry["status"] == "ok" for entry in result["stages"])
    assert result["errors"] == {}
    assert calls["discover"] == {"workers": 3, "sources": ["workday"], "deduplicate": False}
    assert calls["enrich"] == {"workers": 3, "limit": 12}
    assert calls["tailor"] == {"min_score": 8, "validation_mode": "strict", "limit": 12}


def test_run_sequential_isolates_stage_crashes(
    pipeline_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("stage exploded")

    def healthy(**kwargs: Any) -> dict[str, Any]:
        return {"status": "ok"}

    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "enrich", boom)
    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "score", healthy)

    result = _run_sequential(["enrich", "score"], min_score=7)

    assert [entry["status"] for entry in result["stages"]] == ["error: stage exploded", "ok"]
    assert result["errors"] == {"enrich": "error: stage exploded"}


def test_run_pipeline_dry_run_executes_nothing(
    pipeline_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("dry run must not execute stages")

    for stage in STAGE_ORDER:
        monkeypatch.setitem(pipeline._STAGE_RUNNERS, stage, refuse)

    result = run_pipeline(stages=["discover", "score"], dry_run=True)

    assert result == {"stages": [], "errors": {}, "elapsed": 0.0}


def test_run_pipeline_honors_skip_flags(
    pipeline_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[str] = []

    def record(name: str) -> Any:
        def runner(**kwargs: Any) -> dict[str, Any]:
            executed.append(name)
            return {"status": "ok"}

        return runner

    for stage in STAGE_ORDER:
        monkeypatch.setitem(pipeline._STAGE_RUNNERS, stage, record(stage))

    result = run_pipeline(
        stages=["all"],
        skip_enrich=True,
        skip_tailor=True,
        skip_cover=True,
        skip_pdf=True,
    )

    # "score" has no skip flag and remains; the four skippable stages are removed.
    assert executed == ["discover", "score"]
    assert [entry["stage"] for entry in result["stages"]] == ["discover", "score"]
    assert result["errors"] == {}


def test_run_pipeline_rejects_unknown_stage(
    pipeline_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("should not run")

    for stage in STAGE_ORDER:
        monkeypatch.setitem(pipeline._STAGE_RUNNERS, stage, refuse)

    with pytest.raises(SystemExit) as excinfo:
        run_pipeline(stages=["discover", "teleport"])
    assert excinfo.value.code == 1
