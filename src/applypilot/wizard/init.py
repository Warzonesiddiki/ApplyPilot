"""ApplyPilot first-time setup wizard.

Interactive flow that creates the active profile workspace (`~/.applypilot/` or
`~/.applypilot/profiles/<slug>/`) with:
  - resume.txt (and optionally resume.pdf)
  - profile.json (restricted 0600 permissions)
  - searches.yaml
  - .env (LLM API key, restricted 0600 permissions)
"""

from __future__ import annotations

import json
import shutil
from typing import Any
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from applypilot import config

console = Console()


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------

def _setup_resume() -> None:
    """Prompt for resume file and copy into APP_DIR."""
    console.print(Panel("[bold]Step 1: Resume[/bold]\nPoint to your master resume file (.txt or .pdf)."))

    while True:
        path_str = Prompt.ask("Resume file path")
        src = Path(path_str.strip().strip('"').strip("'")).expanduser().resolve()

        if not src.exists():
            console.print(f"[red]File not found:[/red] {src}")
            continue

        suffix = src.suffix.lower()
        if suffix not in (".txt", ".docx", ".md", ".pdf"):
            console.print("[red]Unsupported format.[/red] Provide a .txt, .docx, .md, or .pdf file.")
            continue

        if suffix == ".txt":
            shutil.copy2(src, config.RESUME_PATH)
            console.print(f"[green]Copied to {config.RESUME_PATH}[/green]")
        elif suffix in (".docx", ".md"):
            from applypilot.wizard.importer import read_resume_file
            extracted = read_resume_file(src)
            config.RESUME_PATH.write_text(extracted, encoding="utf-8")
            console.print(f"[green]Extracted text to {config.RESUME_PATH}[/green]")
        elif suffix == ".pdf":
            shutil.copy2(src, config.RESUME_PDF_PATH)
            console.print(f"[green]Copied to {config.RESUME_PDF_PATH}[/green]")

            # Also ask for a plain-text version for LLM consumption
            txt_path_str = Prompt.ask(
                "Plain-text version of your resume (.txt)",
                default="",
            )
            if txt_path_str.strip():
                txt_src = Path(txt_path_str.strip().strip('"').strip("'")).expanduser().resolve()
                if txt_src.exists():
                    shutil.copy2(txt_src, config.RESUME_PATH)
                    console.print(f"[green]Copied to {config.RESUME_PATH}[/green]")
                else:
                    console.print("[yellow]File not found, skipping plain-text copy.[/yellow]")
        break


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

def _setup_profile() -> dict[str, Any]:
    """Walk through profile questions and return a nested profile dict."""
    console.print(Panel("[bold]Step 2: Profile[/bold]\nTell ApplyPilot about yourself. This powers scoring, tailoring, and auto-fill."))

    profile: dict[str, Any] = {}

    domain = Prompt.ask(
        "Professional domain",
        choices=["software_engineering", "fpa_tech_hybrid", "finance_accounting"],
        default="software_engineering",
    )
    profile["domain"] = domain

    # -- Personal --
    console.print("\n[bold cyan]Personal Information[/bold cyan]")
    full_name = Prompt.ask("Full name")
    profile["personal"] = {
        "full_name": full_name,
        "preferred_name": Prompt.ask("Preferred/nickname (leave blank to use first name)", default=""),
        "email": Prompt.ask("Email address"),
        "phone": Prompt.ask("Phone number", default=""),
        "city": Prompt.ask("City"),
        "province_state": Prompt.ask("Province/State/County (e.g. Greater London, North Holland)", default=""),
        "country": Prompt.ask("Country", default="United Kingdom"),
        "postal_code": Prompt.ask("Postal/ZIP code", default=""),
        "address": Prompt.ask("Street address (optional, used for form auto-fill)", default=""),
        "linkedin_url": Prompt.ask("LinkedIn URL", default=""),
        "github_url": Prompt.ask("GitHub URL (optional)", default=""),
        "portfolio_url": Prompt.ask("Portfolio URL (optional)", default=""),
        "website_url": Prompt.ask("Personal website URL (optional)", default=""),
        "password": Prompt.ask("Job site password (used for login walls during auto-apply)", password=True, default=""),
    }

    # -- Work Authorization --
    console.print("\n[bold cyan]Work Authorization & Relocation[/bold cyan]")
    profile["work_authorization"] = {
        "legally_authorized_to_work": "Yes" if Confirm.ask("Are you legally authorized to work in your current country?", default=True) else "No",
        "require_sponsorship": "Yes" if Confirm.ask("Will you need UK/EU visa sponsorship for onsite/hybrid roles?", default=True) else "No",
        "willing_to_relocate": "Yes" if Confirm.ask("Are you willing to relocate to UK / EU hubs?", default=True) else "No",
        "work_permit_type": Prompt.ask("Work permit / visa route (e.g. UK Skilled Worker / EU Blue Card / Citizen)", default="UK Skilled Worker / EU Blue Card / Remote"),
    }

    # -- Compensation --
    console.print("\n[bold cyan]Compensation[/bold cyan]")
    salary = Prompt.ask("Expected annual salary (number)", default="65000")
    salary_currency = Prompt.ask("Primary currency (GBP, EUR, CHF, USD)", default="GBP")
    salary_range = Prompt.ask("Acceptable range (e.g. 55000-85000)", default="55000-85000")
    range_parts = salary_range.split("-") if "-" in salary_range else [salary, salary]
    profile["compensation"] = {
        "salary_expectation": salary,
        "salary_currency": salary_currency,
        "salary_range_min": range_parts[0].strip(),
        "salary_range_max": range_parts[1].strip() if len(range_parts) > 1 else range_parts[0].strip(),
    }

    # -- Experience --
    console.print("\n[bold cyan]Experience & Qualifications[/bold cyan]")
    current_title = Prompt.ask("Current/most recent job title", default="")
    target_role = Prompt.ask("Target role (e.g. 'Senior AI Engineer' or 'Chartered Accountant (ACA)')", default=current_title)
    profile["experience"] = {
        "years_of_experience_total": Prompt.ask("Years of professional experience", default=""),
        "education_level": Prompt.ask("Highest education / qualification (e.g. ACA Chartered Accountant, Master's, Bachelor's)", default=""),
        "current_job_title": current_title,
        "current_title": current_title,
        "target_role": target_role,
    }

    # -- Skills Boundary --
    console.print("\n[bold cyan]Skills & Core Competencies[/bold cyan] (comma-separated)")
    if domain == "finance_accounting":
        stds = Prompt.ask("Accounting & Reporting Standards (e.g. IFRS, UK GAAP, FRS 102)", default="IFRS, UK GAAP, FRS 102, IAS")
        audit = Prompt.ask("Audit & Internal Controls (e.g. ISA, External Audit, SOX)", default="External Audit, ISA, Internal Controls, SOX")
        fpa = Prompt.ask("FP&A & Commercial Finance (e.g. Budgeting, Forecasting, Financial Modelling)", default="FP&A, Budgeting, Forecasting, Variance Analysis, Financial Modelling")
        erp = Prompt.ask("Systems & ERP Tools (e.g. SAP, NetSuite, Excel, Power BI)", default="Advanced Excel, Power BI, SAP, NetSuite, Xero")
        profile["skills_boundary"] = {
            "Accounting & Reporting Standards": [s.strip() for s in stds.split(",") if s.strip()],
            "Audit, Assurance & Internal Controls": [s.strip() for s in audit.split(",") if s.strip()],
            "FP&A & Commercial Finance": [s.strip() for s in fpa.split(",") if s.strip()],
            "Systems, ERP & Data Tools": [s.strip() for s in erp.split(",") if s.strip()],
        }
    else:
        langs = Prompt.ask("Programming languages", default="")
        frameworks = Prompt.ask("Frameworks & libraries", default="")
        tools = Prompt.ask("Tools & platforms (e.g. Docker, AWS, Git, SAP, Power BI)", default="")
        profile["skills_boundary"] = {
            "programming_languages": [s.strip() for s in langs.split(",") if s.strip()],
            "frameworks": [s.strip() for s in frameworks.split(",") if s.strip()],
            "tools": [s.strip() for s in tools.split(",") if s.strip()],
        }

    # -- Resume Facts (preserved truths for tailoring) --
    console.print("\n[bold cyan]Resume Facts[/bold cyan]")
    console.print("[dim]These are preserved exactly during resume tailoring — the AI will never change them.[/dim]")
    companies = Prompt.ask("Companies to always keep (comma-separated)", default="")
    projects = Prompt.ask("Projects to always keep (comma-separated, leave blank if N/A)", default="")
    certs = Prompt.ask("Professional certifications to keep (e.g. ACA, ICAEW, ACCA, AWS)", default="")
    school = Prompt.ask("School / institute name(s) to preserve", default="")
    metrics = Prompt.ask("Real metrics to preserve (e.g. '99.9% uptime, £50M audit scope')", default="")
    profile["resume_facts"] = {
        "preserved_companies": [s.strip() for s in companies.split(",") if s.strip()],
        "preserved_projects": [s.strip() for s in projects.split(",") if s.strip()],
        "certifications": [s.strip() for s in certs.split(",") if s.strip()],
        "preserved_school": school.strip(),
        "real_metrics": [s.strip() for s in metrics.split(",") if s.strip()],
    }

    # -- EEO Voluntary (defaults) --
    profile["eeo_voluntary"] = {
        "gender": "Decline to self-identify",
        "race_ethnicity": "Decline to self-identify",
        "veteran_status": "Decline to self-identify",
        "disability_status": "Decline to self-identify",
    }

    # -- Availability & PDF Theme --
    earliest = Prompt.ask("Earliest start date / notice period", default="Immediately")
    profile["availability"] = {
        "earliest_start_date": earliest,
        "notice_period": earliest,
    }
    profile["pdf_theme"] = Prompt.ask(
        "Preferred PDF resume layout theme",
        choices=["modern", "executive_uk_eu", "compact_ats"],
        default="executive_uk_eu" if domain == "finance_accounting" else "modern",
    )

    # Save with restricted permissions
    config.secure_write_text(
        config.PROFILE_PATH,
        json.dumps(profile, indent=2, ensure_ascii=False),
    )
    console.print(f"\n[green]Profile saved to {config.PROFILE_PATH}[/green]")
    return profile


# ---------------------------------------------------------------------------
# Search config
# ---------------------------------------------------------------------------

def _setup_searches() -> None:
    """Generate a searches.yaml from user input."""
    console.print(Panel("[bold]Step 3: Job Search Config[/bold]\nDefine what you're looking for across UK & EU."))

    location = Prompt.ask("Primary target location (e.g. 'London, United Kingdom', 'Amsterdam, Netherlands', 'Remote')", default="London, United Kingdom")
    distance_str = Prompt.ask("Search radius in miles (0 for remote-only)", default="25")
    try:
        distance = int(distance_str)
    except ValueError:
        distance = 25

    roles_raw = Prompt.ask(
        "Target job titles (comma-separated, e.g. 'AI Engineer, Chartered Accountant ACA')"
    )
    roles = [r.strip() for r in roles_raw.split(",") if r.strip()]

    if not roles:
        console.print("[yellow]No roles provided. Using a default set.[/yellow]")
        roles = ["Software Engineer"]

    lines = [
        "# ApplyPilot search configuration",
        "# Edit this file to refine your job search queries.",
        "",
        "defaults:",
        f'  location: "{location}"',
        f"  distance: {distance}",
        "  hours_old: 72",
        "  results_per_site: 50",
        "",
        "locations:",
        f'  - location: "{location}"',
        f"    remote: {str(distance == 0).lower()}",
        "",
        "queries:",
    ]
    for i, role in enumerate(roles):
        lines.append(f'  - query: "{role}"')
        lines.append(f"    tier: {min(i + 1, 3)}")

    config.SEARCH_CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.print(f"[green]Search config saved to {config.SEARCH_CONFIG_PATH}[/green]")


# ---------------------------------------------------------------------------
# AI Features
# ---------------------------------------------------------------------------

def _setup_ai_features() -> None:
    """Ask about AI scoring/tailoring — optional LLM configuration."""
    console.print(Panel(
        "[bold]Step 4: AI Features (optional)[/bold]\n"
        "An LLM powers job scoring, resume tailoring, and cover letters.\n"
        "Without this, you can still discover and enrich jobs."
    ))

    if not Confirm.ask("Enable AI scoring and resume tailoring?", default=True):
        console.print("[dim]Discovery-only mode. You can configure AI later with [bold]applypilot init[/bold].[/dim]")
        return

    console.print(
        "Supported providers: [bold]Gemini[/bold] (recommended, free tier), "
        "Anthropic Claude, OpenAI, OpenRouter, Groq, DeepSeek, or Local (Ollama/llama.cpp)"
    )
    provider = Prompt.ask(
        "Provider",
        choices=["gemini", "anthropic", "openai", "openrouter", "groq", "deepseek", "local"],
        default="gemini",
    )

    env_lines = ["# ApplyPilot configuration", ""]

    if provider == "gemini":
        api_key = Prompt.ask("Gemini API key (from aistudio.google.com)", password=True)
        model = Prompt.ask("Model", default="gemini-2.0-flash")
        env_lines.append(f"GEMINI_API_KEY={api_key}")
        env_lines.append(f"LLM_MODEL={model}")
    elif provider == "anthropic":
        api_key = Prompt.ask("Anthropic Claude API key", password=True)
        model = Prompt.ask("Model", default="claude-3-5-haiku-latest")
        env_lines.append(f"ANTHROPIC_API_KEY={api_key}")
        env_lines.append(f"LLM_MODEL={model}")
    elif provider == "openai":
        api_key = Prompt.ask("OpenAI API key", password=True)
        model = Prompt.ask("Model", default="gpt-4o-mini")
        env_lines.append(f"OPENAI_API_KEY={api_key}")
        env_lines.append(f"LLM_MODEL={model}")
    elif provider == "openrouter":
        api_key = Prompt.ask("OpenRouter API key", password=True)
        model = Prompt.ask("Model", default="google/gemini-2.0-flash-001")
        env_lines.append(f"OPENROUTER_API_KEY={api_key}")
        env_lines.append(f"LLM_MODEL={model}")
    elif provider == "groq":
        api_key = Prompt.ask("Groq API key", password=True)
        model = Prompt.ask("Model", default="llama-3.3-70b-versatile")
        env_lines.append(f"GROQ_API_KEY={api_key}")
        env_lines.append(f"LLM_MODEL={model}")
    elif provider == "deepseek":
        api_key = Prompt.ask("DeepSeek API key", password=True)
        model = Prompt.ask("Model", default="deepseek-chat")
        env_lines.append(f"DEEPSEEK_API_KEY={api_key}")
        env_lines.append(f"LLM_MODEL={model}")
    elif provider == "local":
        url = Prompt.ask("Local LLM endpoint URL", default="http://localhost:8080/v1")
        model = Prompt.ask("Model name", default="local-model")
        env_lines.append(f"LLM_URL={url}")
        env_lines.append(f"LLM_MODEL={model}")

    env_lines.append("")
    config.secure_write_text(config.ENV_PATH, "\n".join(env_lines))
    console.print(f"[green]AI configuration saved to {config.ENV_PATH}[/green]")


# ---------------------------------------------------------------------------
# Auto-Apply
# ---------------------------------------------------------------------------

def _setup_auto_apply() -> None:
    """Configure autonomous job application (requires Claude Code CLI)."""
    console.print(Panel(
        "[bold]Step 5: Auto-Apply (optional)[/bold]\n"
        "ApplyPilot can autonomously fill and submit job applications\n"
        "using Claude Code as the browser agent."
    ))

    if not Confirm.ask("Enable autonomous job applications?", default=True):
        console.print("[dim]You can apply manually using the tailored resumes ApplyPilot generates.[/dim]")
        return

    if shutil.which("claude"):
        console.print("[green]Claude Code CLI detected.[/green]")
    else:
        console.print(
            "[yellow]Claude Code CLI not found on PATH.[/yellow]\n"
            "Install it from: [bold]https://claude.ai/code[/bold]\n"
            "Auto-apply won't work until Claude Code is installed."
        )

    console.print("\n[dim]Some job sites use CAPTCHAs. CapSolver can handle them automatically.[/dim]")
    if Confirm.ask("Configure CapSolver API key? (optional)", default=False):
        capsolver_key = Prompt.ask("CapSolver API key", password=True)
        if config.ENV_PATH.exists():
            existing = config.ENV_PATH.read_text(encoding="utf-8")
            if "CAPSOLVER_API_KEY" not in existing:
                config.secure_write_text(
                    config.ENV_PATH,
                    existing.rstrip() + f"\nCAPSOLVER_API_KEY={capsolver_key}\n",
                )
        else:
            config.secure_write_text(
                config.ENV_PATH,
                f"# ApplyPilot configuration\nCAPSOLVER_API_KEY={capsolver_key}\n",
            )
        console.print("[green]CapSolver key saved.[/green]")
    else:
        console.print("[dim]Skipped. Add CAPSOLVER_API_KEY to .env later if needed.[/dim]")


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def run_wizard() -> None:
    """Run the full interactive setup wizard."""
    active_prof = config.get_active_profile()
    prof_label = f" (profile: [bold]{active_prof}[/bold])" if active_prof else ""

    console.print()
    console.print(
        Panel.fit(
            f"[bold green]ApplyPilot Setup Wizard{prof_label}[/bold green]\n\n"
            "This will create your configuration at:\n"
            f"  [cyan]{config.APP_DIR}[/cyan]\n\n"
            "You can re-run this anytime with [bold]applypilot init[/bold].",
            border_style="green",
        )
    )

    config.ensure_dirs()
    console.print(f"[dim]Created {config.APP_DIR}[/dim]\n")

    # Step 1: Resume
    _setup_resume()
    console.print()

    # Step 2: Profile
    _setup_profile()
    console.print()

    # Step 3: Search config
    _setup_searches()
    console.print()

    # Step 4: AI features (optional LLM)
    _setup_ai_features()
    console.print()

    # Step 5: Auto-apply (Claude Code detection)
    _setup_auto_apply()
    console.print()

    # Done — show tier status
    tier = config.get_tier()

    tier_lines: list[str] = []
    for t in range(1, 4):
        label = config.TIER_LABELS[t]
        cmds = ", ".join(f"[bold]{c}[/bold]" for c in config.TIER_COMMANDS[t])
        if t <= tier:
            tier_lines.append(f"  [green]✓ Tier {t} — {label}[/green]  ({cmds})")
        elif t == tier + 1:
            tier_lines.append(f"  [yellow]→ Tier {t} — {label}[/yellow]  ({cmds})")
        else:
            tier_lines.append(f"  [dim]✗ Tier {t} — {label}  ({cmds})[/dim]")

    unlock_hint = ""
    if tier == 1:
        unlock_hint = "\n[dim]To unlock Tier 2: configure an LLM API key (re-run [bold]applypilot init[/bold]).[/dim]"
    elif tier == 2:
        unlock_hint = "\n[dim]To unlock Tier 3: install Claude Code CLI + Chrome.[/dim]"

    console.print(
        Panel.fit(
            "[bold green]Setup complete![/bold green]\n\n"
            f"[bold]Your tier: Tier {tier} — {config.TIER_LABELS[tier]}[/bold]\n\n"
            + "\n".join(tier_lines)
            + unlock_hint,
            border_style="green",
        )
    )
