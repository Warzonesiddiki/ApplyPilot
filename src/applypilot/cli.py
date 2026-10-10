"""ApplyPilot CLI — Typer-based command-line interface."""

from __future__ import annotations

import logging
import os
from pathlib import Path
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
    help="AI-powered job discovery, resume tailoring, Career CRM, interview prep, and autonomous application agent.",
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
    """ApplyPilot — AI-powered job search, tailoring, CRM, and application agent."""
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
    from_resume: str = typer.Option(
        None, "--from-resume", "-r",
        help="Auto-generate profile.json, resume.txt, and searches.yaml from an existing .txt or .docx resume.",
    ),
) -> None:
    """Interactive setup wizard — create your profile, resume, and search config."""
    _activate_profile_if_set(profile or template)
    if from_resume:
        from applypilot.wizard.importer import import_from_resume_file
        info = import_from_resume_file(from_resume)
        console.print(
            f"[green]Imported profile '{config.get_active_profile() or 'default'}' from {from_resume}:[/green]\n"
            f"  - Candidate: {info['full_name']} ({info['target_role']} | {info['domain']})\n"
            f"  - Profile:   {info['profile_path']}\n"
            f"  - Searches:  {info['searches_path']}\n"
            f"  - Resume:    {info['resume_path']}"
        )
        return

    if template:
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
    stages_arg: list[str] = typer.Argument(
        None,
        help="Optional stage names: discover, enrich, score, tailor, cover, pdf, or 'all'.",
    ),
    stage: list[str] = typer.Option(
        None, "--stage", "-s",
        help="Stages to run: discover, enrich, score, tailor, cover, pdf. Default: all.",
    ),
    source: list[str] = typer.Option(
        None, "--source",
        help="Discovery sources: jobspy, workday, ats, smartextract. Default: all.",
    ),
    min_score: int = typer.Option(7, "--min-score", help="Min fit score for tailor/cover."),
    limit: int = typer.Option(0, "--limit", "-l", help="Max jobs per stage (0 = default/unlimited)."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel worker threads for discovery/enrichment."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    skip_enrich: bool = typer.Option(False, "--skip-enrich", help="Skip description enrichment."),
    skip_tailor: bool = typer.Option(False, "--skip-tailor", help="Skip resume tailoring."),
    skip_cover: bool = typer.Option(False, "--skip-cover", help="Skip cover letter generation."),
    skip_pdf: bool = typer.Option(False, "--skip-pdf", help="Skip PDF/DOCX conversion."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently in streaming mode."),
    batch_size: int = typer.Option(5, "--batch-size", help="Items per stage cycle in stream mode."),
    validation: str = typer.Option("normal", "--validation", help="Validation mode: strict, normal, lenient."),
    all_profiles: bool = typer.Option(False, "--all-profiles", help="Run pipeline across all initialized profiles."),
    daemon: bool = typer.Option(False, "--daemon", help="Run continuously on a scheduled interval."),
    interval: int = typer.Option(360, "--interval", help="Minutes between scheduled runs when --daemon is active."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
    profile: str = typer.Option(
        None, "--profile", "-p",
        help="Candidate profile slug (e.g. 'tahir' or 'neha').",
    ),
) -> None:
    """Run the discovery -> enrich -> score -> tailor -> cover -> PDF/DOCX pipeline."""
    _activate_profile_if_set(profile)
    _setup_logging(verbose)
    config.require_init()
    config.load_env()

    from applypilot.pipeline import AI_STAGES, ALL_STAGES, run_pipeline

    requested_stages = list(stage) if stage else (list(stages_arg) if stages_arg else [])
    stages = requested_stages if requested_stages else list(ALL_STAGES)
    sources = source if source else ["jobspy", "workday", "ats", "smartextract"]

    if not requested_stages and not config.has_llm_configured() and not dry_run:
        skipped = [s for s in stages if s in AI_STAGES]
        stages = [s for s in stages if s not in AI_STAGES]
        if skipped:
            console.print(
                f"[dim]Skipping AI stages ({', '.join(skipped)}) — no LLM configured. "
                f"Run [bold]applypilot init[/bold] to set up AI scoring and tailoring.[/dim]\n"
            )

    def _execute_once() -> None:
        if all_profiles:
            profs = [
                p["name"] for p in config.list_profiles()
                if p["name"] != "default" and (p["has_profile"] or p["has_resume"])
            ]
            if not profs:
                profs = [config.get_active_profile() or "default"]
            for prof_slug in profs:
                console.print(f"\n[bold magenta]>>> Running Pipeline for Profile: {prof_slug}[/bold magenta]")
                _activate_profile_if_set(None if prof_slug == "default" else prof_slug)
                run_pipeline(
                    stages=stages,
                    sources=sources,
                    min_score=min_score,
                    limit=limit,
                    workers=workers,
                    dry_run=dry_run,
                    skip_enrich=skip_enrich,
                    skip_tailor=skip_tailor,
                    skip_cover=skip_cover,
                    skip_pdf=skip_pdf,
                    stream=stream,
                    batch_size=batch_size,
                    validation_mode=validation,
                )
            return

        run_pipeline(
            stages=stages,
            sources=sources,
            min_score=min_score,
            limit=limit,
            workers=workers,
            dry_run=dry_run,
            skip_enrich=skip_enrich,
            skip_tailor=skip_tailor,
            skip_cover=skip_cover,
            skip_pdf=skip_pdf,
            stream=stream,
            batch_size=batch_size,
            validation_mode=validation,
        )

    if not daemon or dry_run:
        _execute_once()
        return

    import time
    from applypilot.notifications import send_daily_digest

    cycle = 0
    while True:
        cycle += 1
        console.print(f"\n[bold cyan]=== Scheduled Daemon Cycle #{cycle} (interval={interval}m) ===[/bold cyan]")
        _execute_once()
        try:
            send_daily_digest(min_score=max(7, min_score))
        except Exception:
            pass
        console.print(f"[dim]Sleeping {interval} minutes until next scheduled run (Ctrl+C to stop)...[/dim]")
        try:
            time.sleep(max(1, interval) * 60)
        except KeyboardInterrupt:
            console.print("\n[yellow]Daemon stopped by user.[/yellow]")
            break


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
    retry_transient: bool = typer.Option(
        False, "--retry-transient",
        help="Smart retry: reset only transient/timeout failures while keeping ineligible/expired jobs closed.",
    ),
    engine: str = typer.Option(
        "auto", "--engine",
        help="Application engine: 'auto' (Claude CLI if installed, else native proof engine), 'claude', or 'native'.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
    profile: str = typer.Option(
        None, "--profile", "-p",
        help="Candidate profile slug (e.g. 'tahir' or 'neha').",
    ),
) -> None:
    """Launch the autonomous job application agent or native proof-packet engine."""
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

    if retry_transient:
        from applypilot.apply.memory import reset_transient_failed_jobs
        res = reset_transient_failed_jobs()
        console.print(
            f"[green]Smart retry reset {res['reset_count']} transient failed jobs[/green] "
            f"(checked={res['checked']}, buckets={res['by_bucket']})."
        )
        return

    if reset_failed:
        count = do_reset_failed()
        console.print(f"[green]Reset {count} failed jobs.[/green]")
        return

    clean_engine = engine.strip().lower()
    if clean_engine == "native" or (clean_engine == "auto" and not shutil.which("claude")):
        from applypilot.apply.native_agent import prepare_native_applications
        res = prepare_native_applications(min_score=min_score, limit=limit)
        console.print(
            f"[green]Native Application Proof Engine prepared {res['prepared']} pre-submission proof packets[/green] "
            f"in {config.PROOFS_DIR}"
        )
        return

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
    all_profiles: bool = typer.Option(False, "--all-profiles", help="Show side-by-side status for all profiles."),
    profile: str = typer.Option(
        None, "--profile", "-p",
        help="Candidate profile slug (e.g. 'tahir' or 'neha').",
    ),
) -> None:
    """Show pipeline statistics, ATS coverage, UK/EU sponsor counts, and CRM summary."""
    _activate_profile_if_set(profile)
    config.require_init()
    from applypilot.database import close_connection, get_connection, get_stats

    if all_profiles:
        multi = Table(title="ApplyPilot Multi-Profile Status Summary", show_header=True, header_style="bold cyan")
        multi.add_column("Profile", style="bold")
        multi.add_column("Total", justify="right")
        multi.add_column("Enriched", justify="right")
        multi.add_column("Scored", justify="right")
        multi.add_column("Tailored", justify="right")
        multi.add_column("UK/EU Sponsors", justify="right")
        multi.add_column("Avg ATS %", justify="right")
        multi.add_column("Applied", justify="right")

        for item in config.list_profiles():
            db_file = Path(item["app_dir"]) / "applypilot.db"
            if not db_file.exists():
                continue
            c = get_connection(db_file)
            st = get_stats(c)
            multi.add_row(
                item["name"],
                str(st.get("total", 0)),
                str(st.get("with_description", 0)),
                str(st.get("scored", 0)),
                str(st.get("tailored", 0)),
                str(st.get("verified_sponsors", 0)),
                f"{st.get('avg_ats_coverage', 0.0)}%",
                str(st.get("applied", 0)),
            )
            close_connection(db_file)
        console.print(multi)
        return

    if not config.DB_PATH.exists():
        console.print("[yellow]No database found. Run 'applypilot run' first.[/yellow]")
        raise typer.Exit()

    conn = get_connection()
    stats = get_stats(conn)

    active_prof = config.get_active_profile() or "default"
    table = Table(title=f"ApplyPilot Pipeline Status ({active_prof})", show_header=True, header_style="bold cyan")
    table.add_column("Stage / Metric", style="bold")
    table.add_column("Value", justify="right")

    table.add_row("Total Discovered", str(stats.get("total", 0)))
    table.add_row("Enriched (has description)", str(stats.get("with_description", stats.get("enriched", 0))))
    table.add_row("Scored", str(stats.get("scored", 0)))
    table.add_row("Tailored Resumes (.txt / .pdf)", str(stats.get("tailored", 0)))
    table.add_row("Editable Word CVs (.docx)", str(stats.get("with_docx", 0)))
    table.add_row("Cover Letters", str(stats.get("with_cover_letter", stats.get("cover_letters", 0))))
    table.add_row("Verified UK/EU Sponsors", f"[cyan]{stats.get('verified_sponsors', 0)}[/cyan]")
    table.add_row("Avg ATS Keyword Coverage", f"[magenta]{stats.get('avg_ats_coverage', 0.0)}%[/magenta]")
    table.add_row("Interview Prep Packs", str(stats.get("with_prep_pack", 0)))
    table.add_row("Outreach Packs", str(stats.get("with_outreach", 0)))
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
def serve(
    host: str = typer.Option("0.0.0.0", "--host", help="Bind host for the Career CRM server."),
    port: int = typer.Option(8080, "--port", help="TCP port for the Career CRM server."),
    profile: str = typer.Option(
        None, "--profile", "-p",
        help="Candidate profile slug (e.g. 'tahir' or 'neha').",
    ),
) -> None:
    """Launch the local interactive Career CRM web application server."""
    _activate_profile_if_set(profile)
    config.require_init()
    from applypilot.crm import serve_crm
    serve_crm(host=host, port=port)


@app.command()
def prep(
    target: str = typer.Argument(
        None,
        help="Job URL, company name, or title substring (defaults to highest-scored job).",
    ),
    no_llm: bool = typer.Option(False, "--no-llm", help="Use deterministic STAR generator without calling LLM."),
    profile: str = typer.Option(
        None, "--profile", "-p",
        help="Candidate profile slug (e.g. 'tahir' or 'neha').",
    ),
) -> None:
    """Generate a tailored Interview Preparation Dossier (STAR answers + role brief)."""
    _activate_profile_if_set(profile)
    config.require_init()
    config.load_env()
    from applypilot.database import close_connection, init_db
    from applypilot.prep import find_target_job, generate_interview_prep

    conn = init_db()
    job = find_target_job(conn=conn, url_or_query=target)
    if not job:
        close_connection()
        console.print("[red]No matching job found in the database.[/red]")
        raise typer.Exit(1)

    _, out_path = generate_interview_prep(job, use_llm=not no_llm)
    close_connection()
    console.print(
        f"[green]Interview Prep Dossier generated for '{job.get('title')}' ({job.get('company')}):[/green]\n"
        f"  {out_path}"
    )


@app.command()
def outreach(
    target: str = typer.Argument(
        None,
        help="Job URL, company name, or title substring (defaults to highest-scored job).",
    ),
    profile: str = typer.Option(
        None, "--profile", "-p",
        help="Candidate profile slug (e.g. 'tahir' or 'neha').",
    ),
) -> None:
    """Generate LinkedIn Connection Note (<300 chars), Recruiter InMail, and Hiring Manager Cold Email."""
    _activate_profile_if_set(profile)
    config.require_init()
    config.load_env()
    from applypilot.database import close_connection, init_db
    from applypilot.prep import find_target_job, generate_outreach_pack

    conn = init_db()
    job = find_target_job(conn=conn, url_or_query=target)
    if not job:
        close_connection()
        console.print("[red]No matching job found in the database.[/red]")
        raise typer.Exit(1)

    _, out_path = generate_outreach_pack(job)
    close_connection()
    console.print(
        f"[green]Outreach Pack generated for '{job.get('title')}' ({job.get('company')}):[/green]\n"
        f"  {out_path}"
    )


@app.command()
def export(
    fmt: str = typer.Option("csv", "--format", "-f", help="Export format: csv, json, or markdown."),
    output: str = typer.Option(None, "--output", "-o", help="Output file path."),
    min_score: int = typer.Option(0, "--min-score", help="Minimum fit_score filter."),
    stage: str = typer.Option(None, "--stage", help="Filter by CRM stage (e.g. applied, interviewing, ready)."),
    profile: str = typer.Option(
        None, "--profile", "-p",
        help="Candidate profile slug (e.g. 'tahir' or 'neha').",
    ),
) -> None:
    """Export pipeline jobs to CSV, JSON, or Markdown."""
    _activate_profile_if_set(profile)
    config.require_init()
    from applypilot.database import close_connection, init_db
    from applypilot.export import export_jobs

    init_db()
    out_path = export_jobs(fmt=fmt, output_path=output, min_score=min_score, crm_stage=stage)
    close_connection()
    console.print(f"[green]Exported pipeline data ({fmt.upper()}):[/green] {out_path}")


@app.command()
def memory(
    set_key: str = typer.Option(None, "--set", help="Question key to add/update (e.g. 'notice_period')."),
    question: str = typer.Option(None, "--question", "-q", help="Question prompt text."),
    answer: str = typer.Option(None, "--answer", "-a", help="Canonical answer to store."),
    category: str = typer.Option("custom", "--category", "-c", help="Category label."),
    profile: str = typer.Option(
        None, "--profile", "-p",
        help="Candidate profile slug (e.g. 'tahir' or 'neha').",
    ),
) -> None:
    """View or update canonical screening question answers in the Auto-Apply Memory Bank."""
    _activate_profile_if_set(profile)
    config.require_init()
    from applypilot.apply.memory import list_questions, upsert_question
    from applypilot.database import close_connection

    if set_key and answer:
        q_text = question or set_key.replace("_", " ").capitalize() + "?"
        upsert_question(set_key, q_text, answer, category=category)
        console.print(f"[green]Updated screening memory '{set_key}' -> '{answer}'[/green]")

    items = list_questions()
    table = Table(title="Auto-Apply Screening Question Memory Bank", show_header=True, header_style="bold cyan")
    table.add_column("Key", style="bold")
    table.add_column("Category", style="dim")
    table.add_column("Question")
    table.add_column("Canonical Answer", style="green")

    for item in items:
        table.add_row(
            item["question_key"],
            item.get("category") or "general",
            item["question_text"],
            item["canonical_answer"],
        )
    console.print(table)
    close_connection()


@app.command()
def dedup(
    profile: str = typer.Option(
        None, "--profile", "-p",
        help="Candidate profile slug (e.g. 'tahir' or 'neha').",
    ),
) -> None:
    """Run cross-board fuzzy deduplication and prioritize direct employer ATS links."""
    _activate_profile_if_set(profile)
    config.require_init()
    from applypilot.database import close_connection, deduplicate_jobs, init_db

    conn = init_db()
    res = deduplicate_jobs(conn)
    close_connection()
    console.print(
        f"[green]Deduplication complete:[/green] checked {res['checked']} jobs, "
        f"merged {res['groups_merged']} duplicate groups, removed {res['duplicates_removed']} redundant rows."
    )


@app.command(name="sync-email")
def sync_email(
    inbox_file: str = typer.Option(
        None, "--inbox-file", "-i",
        help="Optional path to a JSON file of recruiting email messages (defaults to IMAP or ~/.applypilot/inbox_sync.json).",
    ),
    no_auto_prep: bool = typer.Option(
        False, "--no-auto-prep",
        help="Disable automatic Interview Prep Dossier generation when an interview invite is detected.",
    ),
    profile: str = typer.Option(
        None, "--profile", "-p",
        help="Candidate profile slug (e.g. 'tahir' or 'neha').",
    ),
) -> None:
    """Sync recruiting emails (confirmation, OA, interview invite, offer, rejection) into Career CRM stages."""
    _activate_profile_if_set(profile)
    config.require_init()
    from applypilot.email_sync import sync_application_emails

    summary = sync_application_emails(
        inbox_file=inbox_file,
        auto_prep_on_interview=not no_auto_prep,
    )
    console.print(
        f"[green]Email sync complete:[/green] scanned {summary['scanned']} messages, "
        f"matched {summary['matched']} tracked jobs, updated {summary['updated']} CRM stages, "
        f"auto-generated {summary['prep_generated']} interview prep dossiers."
    )
    if summary["events"]:
        ev_table = Table(title="Post-Application Email Lifecycle Events", show_header=True, header_style="bold cyan")
        ev_table.add_column("Company", style="bold")
        ev_table.add_column("Role")
        ev_table.add_column("Event", style="yellow")
        ev_table.add_column("New CRM Stage", style="green")
        ev_table.add_column("Subject")
        for ev in summary["events"]:
            ev_table.add_row(
                str(ev.get("company") or ""),
                str(ev.get("title") or ""),
                str(ev.get("event_type") or ""),
                str(ev.get("crm_stage") or "").upper(),
                str(ev.get("subject") or "")[:60],
            )
        console.print(ev_table)


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

    ats_reg = config.load_ats_employers()
    ats_board_count = sum(len(v) for v in ats_reg.values() if isinstance(v, list))
    checks.append(("Tier 1", "Direct ATS APIs", ats_board_count > 0, f"{ats_board_count} boards across {len(ats_reg)} platforms"))
    checks.append(("Tier 1", "DOCX & Proof Engine", True, "Native OOXML .docx exporter & Proof Packet engine ready"))

    config.load_env()
    llm_ok = config.has_llm_configured()
    llm_detail = ""
    if llm_ok:
        active_keys = [k for k in config.LLM_ENV_KEYS if os.environ.get(k)]
        llm_detail = f"Configured ({', '.join(active_keys)})"
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
