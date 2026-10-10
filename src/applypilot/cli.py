"""ApplyPilot CLI — Typer-based command-line interface."""

import logging
import shutil
import subprocess
import sys

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from applypilot import __version__, config
from applypilot.config import TIER_COMMANDS, TIER_LABELS

app = typer.Typer(
    name="applypilot",
    help="AI-powered job discovery, resume tailoring, and autonomous application agent.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"ApplyPilot v{__version__}")
        raise typer.Exit()


def _activate_profile_if_set(profile: str | None) -> None:
    """Activate a named profile if provided via CLI option."""
    if profile is not None and profile.strip():
        from applypilot.database import close_connection
        close_connection()
        config.set_active_profile(profile.strip())


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_callback, is_eager=True,
        help="Show version and exit.",
    ),
    profile: str = typer.Option(
        None, "--profile", "-p",
        help="Active candidate profile slug (e.g. 'tahir' or 'neha').",
    ),
) -> None:
    """ApplyPilot — AI-powered job search and application agent."""
    _activate_profile_if_set(profile)


@app.command()
def init(
    profile: str = typer.Option(
        None, "--profile", "-p",
        help="Candidate profile slug to initialize (e.g. 'tahir' or 'neha').",
    ),
    template: str = typer.Option(
        None, "--template", "-t",
        help="Bootstrap profile & EU/UK search config from bundled template ('tahir' or 'neha').",
    ),
) -> None:
    """Interactive setup wizard — create your profile, resume, and search config."""
    _activate_profile_if_set(profile or template)
    if template:
        from pathlib import Path
        tpl = template.strip().lower()
        repo_docs = Path(__file__).resolve().parents[2] / "docs" / "profiles"
        tpl_map = {
            "tahir": (
                "tahir_siddique.template.json",
                "searches_tahir_eu_uk.yaml",
                "tahir_siddique_resume.txt",
            ),
            "neha": (
                "neha_tariq_aca.template.json",
                "searches_neha_aca_eu_uk.yaml",
                "neha_tariq_aca_resume.txt",
            ),
        }
        if tpl not in tpl_map:
            console.print(f"[red]Unknown template '{template}'. Choose 'tahir' or 'neha'.[/red]")
            raise typer.Exit(1)
        prof_file, search_file, resume_file = tpl_map[tpl]
        config.ensure_dirs()
        config.secure_write_text(
            config.PROFILE_PATH,
            (repo_docs / prof_file).read_text(encoding="utf-8"),
        )
        config.SEARCH_CONFIG_PATH.write_text(
            (repo_docs / search_file).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        if (repo_docs / resume_file).exists():
            config.RESUME_PATH.write_text(
                (repo_docs / resume_file).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        elif not config.RESUME_PATH.exists():
            config.RESUME_PATH.write_text(
                f"Paste {tpl.title()}'s full CV text here ({config.RESUME_PATH}).\n",
                encoding="utf-8",
            )
        console.print(
            f"[green]Bootstrapped profile '{config.get_active_profile()}' at {config.APP_DIR}[/green]\n"
            f"  - Profile:  {config.PROFILE_PATH}\n"
            f"  - Searches: {config.SEARCH_CONFIG_PATH}\n"
            f"  - Resume:   {config.RESUME_PATH}"
        )
        return
    from applypilot.wizard.init import run_wizard
    run_wizard()


@app.command()
def profiles() -> None:
    """List all configured candidate profiles and their workspace status."""
    items = config.list_profiles()
    table = Table(
        title="ApplyPilot Candidate Profiles",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Profile", style="bold")
    table.add_column("Active", justify="center")
    table.add_column("Profile JSON", justify="center")
    table.add_column("Resume", justify="center")
    table.add_column("Searches YAML", justify="center")
    table.add_column("Workspace Directory", style="dim")

    for item in items:
        table.add_row(
            item["name"],
            "[green]●[/green]" if item["active"] else "",
            "[green]✓[/green]" if item["has_profile"] else "[yellow]✗[/yellow]",
            "[green]✓[/green]" if item["has_resume"] else "[yellow]✗[/yellow]",
            "[green]✓[/green]" if item["has_searches"] else "[yellow]✗[/yellow]",
            item["app_dir"],
        )

    console.print(table)
    console.print(
        "\n[dim]Switch profile on any command with [bold]--profile <slug>[/bold] "
        "(e.g. [bold]applypilot run --profile tahir[/bold] or [bold]applypilot run --profile neha[/bold]).[/dim]"
    )


@app.command()
def run(
    stage: list[str] = typer.Option(
        None, "--stage", "-s",
        help="Stages to run: discover, enrich, score, tailor, cover, pdf. Default: all.",
    ),
    source: list[str] = typer.Option(
        None, "--source",
        help="Discovery sources: jobspy, workday, smartextract. Default: all.",
    ),
    min_score: int = typer.Option(7, "--min-score", help="Min fit score for tailor/cover."),
    limit: int = typer.Option(50, "--limit", "-l", help="Max jobs per stage."),
    skip_enrich: bool = typer.Option(False, "--skip-enrich", help="Skip description enrichment."),
    skip_tailor: bool = typer.Option(False, "--skip-tailor", help="Skip resume tailoring."),
    skip_cover: bool = typer.Option(False, "--skip-cover", help="Skip cover letter generation."),
    skip_pdf: bool = typer.Option(False, "--skip-pdf", help="Skip PDF conversion."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently in streaming mode."),
    batch_size: int = typer.Option(5, "--batch-size", help="Items per stage cycle in stream mode."),
    validation: str = typer.Option("normal", "--validation", help="Validation mode: strict, normal, lenient."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
    profile: str = typer.Option(
        None, "--profile", "-p",
        help="Candidate profile slug (e.g. 'tahir' or 'neha').",
    ),
) -> None:
    """Run the discovery -> enrich -> score -> tailor -> PDF pipeline."""
    _activate_profile_if_set(profile)
    _setup_logging(verbose)
    config.require_init()
    config.load_env()

    from applypilot.pipeline import AI_STAGES, ALL_STAGES, run_pipeline

    stages = stage if stage else list(ALL_STAGES)
    sources = source if source else ["jobspy", "workday", "smartextract"]

    # Auto-skip AI stages if no LLM is configured (Tier 1 user running default `applypilot run`)
    if not stage and not config.has_llm_configured():
        skipped = [s for s in stages if s in AI_STAGES]
        stages = [s for s in stages if s not in AI_STAGES]
        if skipped:
            console.print(
                f"[dim]Skipping AI stages ({', '.join(skipped)}) — no LLM configured. "
                f"Run [bold]applypilot init[/bold] to set up AI scoring and tailoring.[/dim]\n"
            )

    run_pipeline(
        stages=stages,
        sources=sources,
        min_score=min_score,
        limit=limit,
        skip_enrich=skip_enrich,
        skip_tailor=skip_tailor,
        skip_cover=skip_cover,
        skip_pdf=skip_pdf,
        stream=stream,
        batch_size=batch_size,
        validation=validation,
    )


@app.command()
def apply(
    limit: int = typer.Option(1, "--limit", "-l", help="Max jobs to apply to."),
    url: str = typer.Option(None, "--url", "-u", help="Apply to a specific job URL."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score threshold."),
    headless: bool = typer.Option(False, "--headless", help="Run Chrome in headless mode."),
    model: str = typer.Option("sonnet", "--model", "-m", help="Claude model: sonnet, opus, haiku."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fill forms but don't click Submit."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run continuously, polling for new jobs."),
    poll_interval: int = typer.Option(60, "--poll-interval", help="Seconds between polls in continuous mode."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    gen: str = typer.Option(None, "--gen", help="Generate prompt file for a URL (debug mode)."),
    mark_applied: str = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: str = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed."),
    reason: str = typer.Option("manual", "--reason", help="Failure reason (with --mark-failed)."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs to retry."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
    profile: str = typer.Option(
        None, "--profile", "-p",
        help="Candidate profile slug (e.g. 'tahir' or 'neha').",
    ),
) -> None:
    """Launch the autonomous job application agent (requires Claude Code CLI)."""
    _activate_profile_if_set(profile)
    _setup_logging(verbose)
    config.require_init()
    config.load_env()

    from applypilot.apply.launcher import (
        gen_prompt,
        mark_job,
    )
    from applypilot.apply.launcher import (
        main as apply_main,
    )
    from applypilot.apply.launcher import (
        reset_failed as do_reset_failed,
    )

    # Utility modes don't need Claude Code
    if gen:
        result = gen_prompt(target_url=gen, min_score=min_score, model=model)
        if result:
            console.print(f"[green]Prompt written to:[/green] {result}")
        else:
            console.print("[red]No matching job found.[/red]")
        return

    if mark_applied:
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        mark_job(mark_failed, "failed", reason)
        console.print(f"[yellow]Marked as failed ({reason}):[/yellow] {mark_failed}")
        return

    if reset_failed:
        count = do_reset_failed()
        console.print(f"[green]Reset {count} failed jobs.[/green]")
        return

    # Full apply mode requires Tier 3 (Claude Code CLI)
    config.require_tier(3)

    apply_main(
        limit=limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        poll_interval=poll_interval,
        workers=workers,
    )


@app.command()
def status(
    profile: str = typer.Option(
        None, "--profile", "-p",
        help="Candidate profile slug (e.g. 'tahir' or 'neha').",
    ),
) -> None:
    """Show pipeline statistics and database summary."""
    _activate_profile_if_set(profile)
    config.require_init()
    from applypilot.database import close_connection, get_connection, get_stats

    if not config.DB_PATH.exists():
        console.print("[yellow]No database found. Run 'applypilot run' first.[/yellow]")
        raise typer.Exit()

    conn = get_connection()
    stats = get_stats(conn)

    active_prof = config.get_active_profile() or "default"
    table = Table(title=f"ApplyPilot Pipeline Status ({active_prof})", show_header=True, header_style="bold cyan")
    table.add_column("Stage", style="bold")
    table.add_column("Count", justify="right")

    table.add_row("Total Discovered", str(stats.get("total", 0)))
    table.add_row("Enriched (has description)", str(stats.get("with_description", stats.get("enriched", 0))))
    table.add_row("Scored", str(stats.get("scored", 0)))
    table.add_row("Tailored Resumes", str(stats.get("tailored", 0)))
    table.add_row("Cover Letters", str(stats.get("with_cover_letter", stats.get("cover_letters", 0))))
    table.add_row("Applied", f"[green]{stats.get('applied', 0)}[/green]")
    table.add_row("Failed", f"[red]{stats.get('apply_errors', stats.get('failed', 0))}[/red]")
    table.add_row("Ready to Apply", f"[yellow]{stats.get('ready_to_apply', 0)}[/yellow]")

    console.print(table)

    dist = stats.get("score_distribution", [])
    if dist:
        dist_dict = dict(dist) if isinstance(dist, list) else dist
        dist_table = Table(title="Score Distribution", show_header=True, header_style="bold")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Jobs", justify="right")
        dist_table.add_column("Bar")
        max_count = max(dist_dict.values()) if dist_dict else 1
        for score in sorted(dist_dict.keys(), reverse=True):
            count = dist_dict[score]
            bar_len = int((count / max_count) * 30)
            color = "green" if score >= 7 else ("yellow" if score >= 5 else "dim")
            dist_table.add_row(str(score), str(count), f"[{color}]{'█' * bar_len}[/{color}]")
        console.print(dist_table)

    close_connection()


@app.command()
def dashboard(
    output: str = typer.Option(None, "--output", "-o", help="Output HTML file path."),
    no_open: bool = typer.Option(False, "--no-open", help="Generate HTML without opening browser."),
    profile: str = typer.Option(
        None, "--profile", "-p",
        help="Candidate profile slug (e.g. 'tahir' or 'neha').",
    ),
) -> None:
    """Generate and open the interactive HTML dashboard."""
    _activate_profile_if_set(profile)
    config.require_init()
    from applypilot.view import generate_dashboard, open_dashboard

    if no_open or output:
        path = generate_dashboard(output)
        console.print(f"[green]Dashboard generated:[/green] {path}")
    else:
        open_dashboard()


@app.command()
def doctor(
    profile: str = typer.Option(
        None, "--profile", "-p",
        help="Candidate profile slug (e.g. 'tahir' or 'neha').",
    ),
) -> None:
    """Check system dependencies, configuration, and feature tier status."""
    _activate_profile_if_set(profile)
    checks: list[tuple[str, str, bool, str]] = []

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 11)
    checks.append(("Tier 1", f"Python {py_ver}", py_ok, "Requires 3.11+" if not py_ok else ""))

    pw_ok = False
    try:
        import playwright  # noqa: F401
        pw_ok = True
    except ImportError:
        pass
    checks.append(("Tier 1", "Playwright SDK", pw_ok, "pip install playwright" if not pw_ok else ""))

    pw_browsers = False
    if pw_ok:
        try:
            result = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "--dry-run"],
                capture_output=True, text=True, timeout=10,
            )
            pw_browsers = result.returncode == 0
        except Exception:
            pw_browsers = True
    checks.append(("Tier 1", "Playwright Chromium", pw_browsers, "playwright install chromium" if not pw_browsers else ""))

    jobspy_ok = False
    try:
        import jobspy  # noqa: F401
        jobspy_ok = True
    except ImportError:
        pass
    checks.append(("Tier 1", "python-jobspy", jobspy_ok, "pip install python-jobspy (optional)" if not jobspy_ok else ""))

    profile_ok = config.PROFILE_PATH.exists()
    checks.append(("Tier 1", "Profile config", profile_ok, f"{config.PROFILE_PATH}" if profile_ok else "Run: applypilot init"))

    resume_ok = config.RESUME_PATH.exists()
    checks.append(("Tier 1", "Resume file", resume_ok, f"{config.RESUME_PATH}" if resume_ok else "Run: applypilot init"))

    search_ok = config.SEARCH_CONFIG_PATH.exists()
    checks.append(("Tier 1", "Search config", search_ok, f"{config.SEARCH_CONFIG_PATH}" if search_ok else "Run: applypilot init"))

    config.load_env()
    llm_ok = config.has_llm_configured()
    llm_detail = ""
    if llm_ok:
        import os
        if os.environ.get("GEMINI_API_KEY"):
            llm_detail = "Gemini API key configured"
        elif os.environ.get("OPENAI_API_KEY"):
            llm_detail = "OpenAI API key configured"
        elif os.environ.get("LLM_URL"):
            llm_detail = f"Local LLM: {os.environ['LLM_URL']}"
    else:
        llm_detail = "Optional — run 'applypilot init' to enable AI scoring/tailoring"
    checks.append(("Tier 2", "LLM API key", llm_ok, llm_detail))

    claude_path = shutil.which("claude")
    checks.append(("Tier 3", "Claude Code CLI", claude_path is not None,
                    claude_path or "Optional — https://claude.ai/code (needed for auto-apply)"))

    npx_path = shutil.which("npx")
    checks.append(("Tier 3", "npx (Node.js)", npx_path is not None,
                    npx_path or "Optional — install Node.js 18+ (needed for auto-apply)"))

    chrome_path = None
    try:
        chrome_path = config.get_chrome_path()
    except FileNotFoundError:
        pass
    checks.append(("Tier 3", "Google Chrome", chrome_path is not None,
                    str(chrome_path) if chrome_path else "Optional — https://google.com/chrome (needed for auto-apply)"))

    db_ok = config.DB_PATH.exists()
    db_info = ""
    if db_ok:
        from applypilot.database import close_connection, get_connection
        conn = get_connection()
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        close_connection()
        db_info = f"{count} jobs in database"
    else:
        db_info = "Will be created on first run"
    checks.append(("", "Database", db_ok, db_info))

    active_prof = config.get_active_profile() or "default"
    table = Table(title=f"ApplyPilot System Check ({active_prof})", show_header=True, header_style="bold cyan")
    table.add_column("Tier", style="dim", width=6)
    table.add_column("Component", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Details")

    for tier_col, name, ok, detail in checks:
        status_str = "[green]OK[/green]" if ok else "[yellow]--[/yellow]"
        if tier_col == "Tier 1" and not ok and name not in ("python-jobspy",):
            status_str = "[red]MISSING[/red]"
        table.add_row(tier_col, name, status_str, detail)

    console.print(table)

    tier = config.get_tier() if config.is_initialized() else 0

    tier_lines: list[str] = []
    for t in range(1, 4):
        label = TIER_LABELS[t]
        cmds = ", ".join(c for c in TIER_COMMANDS[t])
        if t <= tier:
            tier_lines.append(f"  [green]✓ Tier {t} — {label}[/green]  ({cmds})")
        else:
            tier_lines.append(f"  [dim]✗ Tier {t} — {label}  ({cmds})[/dim]")

    if tier == 0:
        summary_title = "[yellow]Not initialized — run 'applypilot init' to get started[/yellow]"
    else:
        summary_title = f"[bold]Active: Tier {tier} — {TIER_LABELS[tier]}[/bold]"

    console.print()
    console.print(Panel.fit(
        summary_title + "\n\n" + "\n".join(tier_lines),
        border_style="cyan",
    ))


if __name__ == "__main__":
    app()
