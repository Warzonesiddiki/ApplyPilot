"""CLI surface, legacy-console safety, and multi-provider LLM configuration tests.

Covers the roadmap Phase 10 deliverable `tests/test_cli_and_llm.py`. The CLI is
the primary user interface, so every command is exercised through `CliRunner`,
including the regression where Rich wrote non-ASCII glyphs straight to a legacy
Windows code page (cp1252/cp437) and aborted the command.
"""

from __future__ import annotations

import io
import json
import sys
import tomllib
from pathlib import Path

import httpx
import pytest
from rich.console import Console
from typer.testing import CliRunner

import applypilot.cli as cli
import applypilot.llm as llm
from applypilot import config, database, term
from applypilot.cli import app
from applypilot.llm import (
    LLMClient,
    _detect_all_providers,
    _detect_provider,
    clear_response_cache,
    get_telemetry,
    reset_telemetry,
)
from applypilot.schemas import ScoreResponse

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[1]

ALL_COMMANDS = (
    "init",
    "profiles",
    "run",
    "apply",
    "status",
    "dashboard",
    "serve",
    "prep",
    "outreach",
    "export",
    "memory",
    "dedup",
    "sync-email",
    "doctor",
)

LLM_ENV_KEYS = (
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "DEEPSEEK_API_KEY",
    "LLM_URL",
    "LLM_MODEL",
    "LLM_API_KEY",
)


def _seed_job(fit_score: int | None = None, url: str = "https://jobs.example.test/1") -> None:
    """Insert one tracked job into the active profile's database."""
    conn = database.init_db()
    database.store_jobs(
        conn,
        jobs=[
            {
                "url": url,
                "title": "Senior Financial Analyst",
                "company": "Monzo Bank",
                "location": "London, United Kingdom",
                "full_description": "IFRS 15 reporting, FP&A, and ERP consolidation.",
            }
        ],
        site="Monzo Bank",
        strategy="greenhouse_api",
    )
    if fit_score is not None:
        conn.execute("UPDATE jobs SET fit_score = ? WHERE url = ?", (fit_score, url))
    conn.commit()


def _score_payload() -> dict[str, object]:
    return {
        "score": 9,
        "skills_score": 9,
        "qualifications_score": 9,
        "domain_score": 9,
        "location_score": 9,
        "keywords": ["IFRS"],
        "missing_keywords": [],
        "reasoning": "Strong domain alignment and directly relevant experience.",
    }


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_help_lists_every_command_and_version_short_circuits() -> None:
    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0
    for command in ALL_COMMANDS:
        assert command in help_result.stdout

    version_result = runner.invoke(app, ["--version"])
    assert version_result.exit_code == 0
    assert "ApplyPilot v" in version_result.stdout


def test_cli_init_templates_rejects_unknown_and_profiles_lists_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    try:
        bootstrapped = runner.invoke(app, ["init", "-p", "tahir", "-t", "tahir"])
        assert bootstrapped.exit_code == 0
        assert (tmp_path / "profiles" / "tahir" / "profile.json").exists()
        assert (tmp_path / "profiles" / "tahir" / "searches.yaml").exists()

        unknown = runner.invoke(app, ["init", "-p", "ghost", "-t", "bogus"])
        assert unknown.exit_code == 1
        assert "Unknown template" in unknown.stdout

        listing = runner.invoke(app, ["profiles"])
        assert listing.exit_code == 0
        assert "tahir" in listing.stdout
    finally:
        config.set_active_profile(None)


def test_cli_template_packaging_ships_every_bundled_profile() -> None:
    """`init -t <name>` must resolve its templates from an installed wheel.

    The templates live in ``docs/profiles`` for contributors, which means a
    source checkout hides packaging mistakes: pyproject has to force-include
    that directory as ``applypilot/templates/profiles`` or every installed user
    hits FileNotFoundError. Guard the declared mapping and confirm each
    template the CLI references is actually inside it.
    """
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force_include["docs/profiles"] == "applypilot/templates/profiles"

    source_dir = REPO_ROOT / "docs" / "profiles"
    assert source_dir.is_dir()
    for name, filenames in cli.PROFILE_TEMPLATES.items():
        for filename in filenames:
            assert (source_dir / filename).is_file(), f"'{name}' template {filename} is not shipped"


def test_cli_profile_templates_dir_resolves_with_all_templates() -> None:
    resolved = cli._profile_templates_dir()
    assert resolved is not None
    assert resolved.is_dir()
    for profile_file, search_file, _resume_file in cli.PROFILE_TEMPLATES.values():
        assert (resolved / profile_file).is_file()
        assert (resolved / search_file).is_file()


def test_cli_doctor_reports_feature_tiers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile("tahir")
    try:
        result = runner.invoke(app, ["doctor", "-p", "tahir"])
        assert result.exit_code == 0
        assert "Tier 1" in result.stdout
        assert "Playwright" in result.stdout
    finally:
        config.set_active_profile(None)


def test_cli_run_dry_run_and_unknown_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile("tahir")
    try:
        dry = runner.invoke(app, ["run", "--dry-run", "-p", "tahir"])
        assert dry.exit_code == 0
        assert "DRY RUN" in dry.stdout

        unknown = runner.invoke(app, ["run", "bogus-stage", "--dry-run", "-p", "tahir"])
        assert unknown.exit_code == 1
        assert "Unknown stage" in unknown.stdout
    finally:
        config.set_active_profile(None)


def test_cli_export_supports_all_formats_and_rejects_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile("tahir")
    try:
        _seed_job()
        for fmt, suffix in (("csv", ".csv"), ("json", ".json"), ("markdown", ".md")):
            result = runner.invoke(app, ["export", "-f", fmt, "-p", "tahir"])
            assert result.exit_code == 0, result.stdout
            assert list(tmp_path.glob(f"**/*{suffix}")), f"expected a {suffix} export"

        bad = runner.invoke(app, ["export", "-f", "xml", "-p", "tahir"])
        assert bad.exit_code == 1
        assert "Unsupported export format" in bad.stdout
        assert "Traceback" not in bad.stdout
    finally:
        database.close_connection()
        config.set_active_profile(None)


def test_cli_memory_bank_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile("tahir")
    try:
        database.init_db()
        stored = runner.invoke(
            app, ["memory", "--set", "notice_period", "-a", "1 month", "-p", "tahir"]
        )
        assert stored.exit_code == 0
        assert "notice_period" in stored.stdout
        assert "1 month" in stored.stdout
    finally:
        database.close_connection()
        config.set_active_profile(None)


def test_cli_maintenance_commands_run_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile("tahir")
    try:
        _seed_job()
        for args in (
            ["dedup", "-p", "tahir"],
            ["status", "-p", "tahir"],
            ["status", "--all-profiles"],
            ["sync-email", "-p", "tahir"],
            ["dashboard", "--no-open", "-p", "tahir"],
        ):
            result = runner.invoke(app, args)
            assert result.exit_code == 0, f"{args} failed: {result.stdout}"
    finally:
        database.close_connection()
        config.set_active_profile(None)


def test_cli_prep_and_outreach_report_missing_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile("tahir")
    try:
        database.init_db()
        for args in (
            ["prep", "no-such-employer-anywhere", "--no-llm", "-p", "tahir"],
            ["outreach", "no-such-employer-anywhere", "-p", "tahir"],
        ):
            result = runner.invoke(app, args)
            assert result.exit_code == 1
            assert "No matching job" in result.stdout
    finally:
        database.close_connection()
        config.set_active_profile(None)


def test_cli_apply_utility_modes_and_native_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile("tahir")
    url = "https://jobs.example.test/1"
    try:
        assert runner.invoke(app, ["init", "-p", "tahir", "-t", "tahir"]).exit_code == 0
        _seed_job(fit_score=9)

        marked = runner.invoke(app, ["apply", "--mark-applied", url, "-p", "tahir"])
        assert marked.exit_code == 0
        assert "Marked as applied" in marked.stdout

        failed = runner.invoke(
            app, ["apply", "--mark-failed", url, "--reason", "timeout", "-p", "tahir"]
        )
        assert failed.exit_code == 0
        assert "Marked as failed" in failed.stdout

        retry = runner.invoke(app, ["apply", "--retry-transient", "-p", "tahir"])
        assert retry.exit_code == 0
        assert "transient" in retry.stdout

        reset = runner.invoke(app, ["apply", "--reset-failed", "-p", "tahir"])
        assert reset.exit_code == 0

        native = runner.invoke(app, ["apply", "--engine", "native", "-p", "tahir"])
        assert native.exit_code == 0
        assert "proof packets" in native.stdout
        assert list(config.PROOFS_DIR.glob("*_PROOF.json"))
    finally:
        database.close_connection()
        config.set_active_profile(None)


# ---------------------------------------------------------------------------
# Legacy Windows code page regression (cp1252 cannot encode ● / ✓ / ✗)
# ---------------------------------------------------------------------------


def test_term_glyph_and_safe_text_degrade_on_legacy_code_pages() -> None:
    legacy = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    try:
        assert term.glyph("✓", "yes", legacy) == "yes"
        assert term.glyph("✗", "no", legacy) == "no"
        assert term.glyph("●", "*", legacy) == "*"
        assert term.glyph("✓", "yes", io.TextIOWrapper(io.BytesIO(), encoding="utf-8")) == "✓"
        assert term.supports_glyph("é", legacy)
        assert not term.supports_glyph("✓", legacy)
        # Encodable characters survive; only exotic ones degrade.
        assert term.safe_text("Café ● Engineering", legacy) == "Café * Engineering"
        assert term.safe_text("Résumé — 日本語", legacy) == "Résumé — ???"
        assert term.safe_text("ascii only", legacy) == "ascii only"
    finally:
        legacy.detach()


def test_configure_stdio_degrades_unencodable_output(monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = io.BytesIO()
    strict = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict")
    try:
        monkeypatch.setattr(sys, "stdout", strict)
        monkeypatch.setattr(sys, "stderr", strict)

        term.configure_stdio()
        sys.stdout.write("●✓✗ — £€")
        sys.stdout.flush()

        written = buffer.getvalue().decode("cp1252")
        assert "?" in written
        # em dash, pound, and euro are representable in cp1252 and kept intact
        assert "—" in written
        assert "£" in written
        assert "€" in written
    finally:
        strict.detach()


def test_profiles_command_renders_ascii_only_on_legacy_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: `applypilot profiles` aborted with UnicodeEncodeError on cp1252."""
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    config.set_active_profile(None)
    try:
        bootstrapped = runner.invoke(app, ["init", "-p", "tahir", "-t", "tahir"])
        assert bootstrapped.exit_code == 0

        buffer = io.BytesIO()
        legacy = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict")
        try:
            monkeypatch.setattr(cli, "console", Console(file=legacy, width=120, legacy_windows=False))
            cli.profiles()  # must not raise UnicodeEncodeError
            legacy.flush()
            rendered = buffer.getvalue().decode("cp1252")
        finally:
            legacy.detach()

        assert "yes" in rendered
        assert "no" in rendered
        # The decorative glyphs are replaced rather than aborting the command.
        assert "✓" not in rendered
        assert "✗" not in rendered
        assert "●" not in rendered
    finally:
        config.set_active_profile(None)


# ---------------------------------------------------------------------------
# Multi-provider LLM configuration, telemetry, and failover
# ---------------------------------------------------------------------------


def test_llm_provider_detection_order_and_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in LLM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="No LLM provider configured"):
        _detect_provider()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    providers = _detect_all_providers()
    assert [p[0] for p in providers] == [
        "https://api.anthropic.com/v1",
        "https://api.groq.com/openai/v1",
    ]
    assert _detect_provider() == providers[0]
    assert providers[0][2] == "anthropic-key"

    monkeypatch.setenv("LLM_MODEL", "custom-model")
    overridden = _detect_all_providers()
    assert overridden[0][1] == "custom-model"
    assert overridden[0][1] != overridden[1][1]

    monkeypatch.setenv("LLM_URL", "http://127.0.0.1:8080/v1")
    monkeypatch.setenv("LLM_API_KEY", "local-key")
    local_first = _detect_all_providers()[0]
    assert local_first[0] == "http://127.0.0.1:8080/v1"
    assert local_first[1] == "custom-model"
    assert local_first[2] == "local-key"


def test_llm_telemetry_tracks_tokens_cost_latency_and_failovers() -> None:
    reset_telemetry()
    llm._record_telemetry("gemini-2.0-flash", prompt_chars=400, completion_chars=80, latency_ms=120)
    llm._record_telemetry("local-model", prompt_chars=400, completion_chars=80, latency_ms=30, failover=True)

    telemetry = get_telemetry()
    assert telemetry["calls"] == 2
    assert telemetry["prompt_tokens_est"] == 200
    assert telemetry["completion_tokens_est"] == 40
    assert telemetry["failovers"] == 1
    assert telemetry["total_latency_ms"] == 150
    assert telemetry["estimated_cost_usd"] > 0

    reset_telemetry()
    assert get_telemetry()["calls"] == 0


def test_llm_client_failover_records_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_response_cache()
    reset_telemetry()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "primary.test":
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(_score_payload())}}]},
            request=request,
        )

    monkeypatch.setattr("applypilot.llm.time.sleep", lambda _seconds: None)
    client = LLMClient(
        "https://primary.test/v1",
        "primary-model",
        "primary-key",
        fallbacks=[("https://backup.test/v1", "backup-model", "backup-key")],
        http_transport=httpx.MockTransport(handler),
    )
    try:
        result = client.structured_chat(
            [{"role": "user", "content": "Score the candidate."}], ScoreResponse, max_tokens=128
        )
    finally:
        client.close()

    assert result.score == 9
    assert get_telemetry()["failovers"] >= 1
