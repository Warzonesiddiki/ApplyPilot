# Changelog

All notable changes to ApplyPilot will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - 2026-10-10

### Added
- **Direct Zero-Browser ATS API Connectors (`discovery/ats_direct.py`)** - Added direct public JSON/XML API scrapers for **7 ATS platforms** (**Greenhouse**, **Lever**, **Ashby**, **SmartRecruiters**, **Personio**, **Workable**, and **Recruitee**) across 50+ premier UK & EU employers (`ats_employers` in `config/employers.yaml`), pre-enriching full job descriptions and direct apply URLs with zero browser/LLM overhead.
- **Cross-Board Fuzzy Deduplication (`database.py:deduplicate_jobs` & `applypilot dedup`)** - Computes normalized `(company, title, location)` fingerprints (`compute_job_fingerprint`) and merges duplicate listings across aggregators and direct ATS boards while prioritizing direct employer ATS links.
- **UK Home Office Skilled Worker & EU Sponsor Matcher (`enrichment/sponsors.py`)** - Automatically tags jobs with `sponsor_status` (`sponsorship_offered`, `licensed_sponsor`, `no_sponsorship_stated`, `unknown`), `work_arrangement` (`remote`, `hybrid`, `onsite`), and expired-listing detection.
- **Explainable 4-Dimensional Scoring & Deterministic ATS Keyword Coverage (`scoring/scorer.py` & `scoring/ats.py`)** - Scores jobs across 4 sub-dimensions (`skills_score`, `qualifications_score`, `domain_score`, `location_score`) and computes deterministic pre- vs. post-tailor ATS keyword coverage (`ats_coverage_pct`), injecting missing JD keywords into the resume tailoring prompt (`scoring/tailor.py`).
- **Multi-Provider LLM Engine with Automatic Failover & Telemetry (`llm.py`)** - Added native support for **Anthropic Claude**, **OpenRouter**, **Groq**, and **DeepSeek** alongside **Gemini**, **OpenAI**, and **Local Ollama/llama.cpp**, with automatic provider failover on HTTP 429/502/503 and token/cost telemetry (`get_telemetry()`).
- **Selectable PDF Themes (`scoring/pdf.py`) & Pure-Python Word (`.docx`) Exporter (`scoring/docx_export.py`)** - Supports `modern`, `executive_uk_eu`, and `compact_ats` PDF themes plus editable OpenXML `.docx` CVs and cover letters for UK/EU recruitment agency submissions.
- **Interactive Career CRM Server (`applypilot serve` & `crm.py`) & Upgraded Dashboard (`view.py`)** - Full-lifecycle CRM stages (`discovered`, `scored`, `tailored`, `ready_to_apply`, `applied`, `assessment`, `interviewing`, `offer`, `rejected`), 4D sub-score pills, ATS & UK/EU sponsor badges, Base vs. Tailored CV Side-by-Side Diff viewer, expandable CV/Cover Letter/Interview Prep drawers, and live REST API.
- **Post-Application Email Sync CRM (`email_sync.py` & `applypilot sync-email`)** - Classifies recruiting emails (application received, online assessment, interview invitation, job offer, rejection), transitions job `crm_stage` automatically, and auto-generates Interview Prep Dossiers when an interview invite arrives.
- **Native Python Application Proof Engine (`apply/native_agent.py` & `applypilot apply --engine native`)** - Generates auditable pre-submission proof packets (`<job>_PROOF.json`) mapping candidate profile fields, screening memory bank answers, and HTML form inputs without requiring the external Claude Code CLI binary.
- **AI Interview Prep Dossier (`applypilot prep`) & LinkedIn/Recruiter Outreach (`applypilot outreach`)** - Generates role-specific STAR interview dossiers grounded in `resume_facts` plus `<300`-char LinkedIn connection notes, Recruiter InMails, and Hiring Manager cold emails (`prep.py`).
- **Persistent Screening Question Memory Bank (`apply/memory.py` & `applypilot memory`) & Smart Transient Retry (`--retry-transient`)** - Stores canonical answers to recurring ATS questions and classifies `apply_error` into retryable vs. permanent buckets.
- **One-Command Resume Importer (`applypilot init --from-resume`) & Multi-Format Export (`applypilot export`) & Webhook Alerts (`notifications.py`)**.

### Fixed
- **CLI ↔ Pipeline Contract Mismatch (`cli.py` & `pipeline.py`)** - Exported `ALL_STAGES` and `AI_STAGES` in `pipeline.py`, aligned `run_pipeline()` parameters, and added `--workers`, `--dry-run`, and `--all-profiles` to `applypilot run`.

## [0.2.0] - 2026-02-17

### Added
- **Parallel workers for discovery/enrichment** - `applypilot run --workers N` enables
  ThreadPoolExecutor-based parallelism for Workday scraping, smart extract, and detail
  enrichment. Default is sequential (1); power users can scale up.
- **Apply utility modes** - `--gen` (generate prompt for manual debugging), `--mark-applied`,
  `--mark-failed`, `--reset-failed` flags on `applypilot apply`
- **Dry-run mode** - `applypilot apply --dry-run` fills forms without clicking Submit
- **5 new tracking columns** - `agent_id`, `last_attempted_at`, `apply_duration_ms`,
  `apply_task_id`, `verification_confidence` for better apply-stage observability
- **Manual ATS detection** - `manual_ats` list in `config/sites.yaml` skips sites with
  unsolvable CAPTCHAs (e.g. TCS iBegin)
- **Qwen3 `/no_think` optimization** - automatically saves tokens when using Qwen models
- **`config.DEFAULTS`** - centralized dict for magic numbers (`min_score`, `max_apply_attempts`,
  `poll_interval`, `apply_timeout`, `viewport`)

### Fixed
- **Config YAML not found after install** - moved `config/` into the package at
  `src/applypilot/config/` so YAML files (employers, sites, searches) ship with `pip install`
- **Search config format mismatch** - wizard wrote `searches:` key but discovery code
  expected `queries:` with tier support. Aligned wizard output and example config
- **JobSpy install isolation** - removed python-jobspy from package dependencies due to
  broken numpy==1.26.3 exact pin in jobspy metadata. Installed separately with `--no-deps`
- **Scoring batch limit** - default limit of 50 silently left jobs unscored across runs.
  Changed to no limit (scores all pending jobs in one pass)
- **Missing logging output** - added `logging.basicConfig(INFO)` so per-job progress for
  scoring, tailoring, and cover letters is visible during pipeline runs

### Changed
- **Blocked sites externalized** - moved from hardcoded sets in launcher.py to
  `config/sites.yaml` under `blocked:` key
- **Site base URLs externalized** - moved from hardcoded dict in detail.py to
  `config/sites.yaml` under `base_urls:` key
- **SSO domains externalized** - moved from hardcoded list in prompt.py to
  `config/sites.yaml` under `blocked_sso:` key
- **Prompt improvements** - screening context uses `target_role` from profile,
  salary section includes `currency_conversion_note` and dynamic hourly rate examples
- **`acquire_job()` fixed** - writes `agent_id` and `last_attempted_at` to proper columns
  instead of misusing `apply_error`
- **`profile.example.json`** - added `currency_conversion_note` and `target_role` fields

## [0.1.0] - 2026-02-17

### Added
- 6-stage pipeline: discover, enrich, score, tailor, cover letter, apply
- Multi-source job discovery: Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs
- Workday employer portal support (46 preconfigured employers)
- Direct career site scraping (28 preconfigured sites)
- 3-tier job description extraction cascade (JSON-LD, CSS selectors, AI fallback)
- AI-powered job scoring (1-10 fit scale with rationale)
- Resume tailoring with factual preservation (no fabrication)
- Cover letter generation per job
- Autonomous browser-based application submission via Playwright
- Interactive setup wizard (`applypilot init`)
- Cross-platform Chrome/Chromium detection (Windows, macOS, Linux)
- Multi-provider LLM support (Gemini, OpenAI, local models via OpenAI-compatible endpoints)
- Pipeline stats and HTML results dashboard
- YAML-based configuration for employers, career sites, and search queries
- Job deduplication across sources
- Configurable score threshold filtering
- Safety limits for maximum applications per run
- Detailed application results logging
