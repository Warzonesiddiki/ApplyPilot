"""Streaming and retry-snapshot regression tests for batch job scoring."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import applypilot.config as config
import applypilot.database as database
import applypilot.scoring.scorer as scorer


def test_scoring_uses_bounded_batches_and_continues_past_job_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "scoring.sqlite3"
    conn = database.init_db(db_path)
    conn.executemany(
        "INSERT INTO jobs (url, title, company, site, full_description, discovered_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                f"https://jobs.example.test/{index:03d}",
                "Expected failure" if index == 7 else f"Engineer {index}",
                "Example Corp",
                "test",
                "Build reliable Python services and maintain production systems.",
                f"2026-10-{(index % 28) + 1:02d}T00:00:00Z",
            )
            for index in range(40)
        ],
    )
    conn.commit()

    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("Base resume", encoding="utf-8")
    monkeypatch.setattr(config, "RESUME_PATH", resume_path)
    monkeypatch.setattr(config, "load_profile", lambda: {})
    monkeypatch.setattr(scorer, "get_connection", lambda: conn)
    monkeypatch.setattr(
        scorer,
        "select_best_base_resume",
        lambda _description, default_resume_path, profile: ("Base resume", "default"),
    )

    pending_failure = {"https://jobs.example.test/007"}

    def fake_score_job(
        _resume: str,
        job: dict[str, Any],
        profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if job["url"] in pending_failure:
            pending_failure.remove(job["url"])
            return {"score": 0, "keywords": "", "reasoning": "Transient scoring failure"}
        return {
            "score": 8,
            "keywords": "Python",
            "reasoning": "Relevant production experience.",
            "ats_coverage_pct": 75,
            "score_breakdown": "{}",
        }

    monkeypatch.setattr(scorer, "score_job", fake_score_job)
    batch_limits: list[int] = []

    def trace(statement: str) -> None:
        if "FROM jobs WHERE rowid >" in statement:
            match = re.search(r"ORDER BY rowid LIMIT (\d+)", statement)
            if match:
                batch_limits.append(int(match.group(1)))

    conn.set_trace_callback(trace)
    created_pools: list[int] = []
    real_executor = ThreadPoolExecutor

    def record_executor(
        max_workers: int,
        thread_name_prefix: str = "",
    ) -> ThreadPoolExecutor:
        created_pools.append(max_workers)
        return real_executor(max_workers=max_workers, thread_name_prefix=thread_name_prefix)

    monkeypatch.setattr(scorer, "ThreadPoolExecutor", record_executor)
    summary = scorer.run_scoring(workers=4)
    conn.set_trace_callback(None)

    assert summary["scored"] == 39
    assert summary["errors"] == 1
    assert summary["distribution"] == [(8, 39)]
    assert batch_limits == [16, 16, 8]
    assert created_pools == [4]
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE fit_score IS NULL").fetchone()[0] == 1

    rescore = scorer.run_scoring(limit=10, rescore=True, workers=2)
    assert rescore["scored"] == 10
    assert rescore["errors"] == 0
    assert created_pools == [4, 2]
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE fit_score = 8").fetchone()[0] == 40
    database.close_connection(db_path)
