# ApplyPilot Upgrade — Phased Implementation Roadmap & Execution Plan

**Objective:** Transform ApplyPilot into a multi-profile, domain-adaptive (Tech/AI/FP&A + ACA Chartered Accountancy/Finance), Pan-EU & UK autonomous job application system for **Tahir Siddique** and **Neha Tariq, ACA**.

---

## Phase 0: Research, Planning & Specification Documents (Completed)
- [x] `docs/01_AUDIT_AND_GAP_ANALYSIS.md` — Full codebase audit & 7 critical EU/UK + dual-profile blockers identified.
- [x] `docs/02_EU_UK_MARKET_RESEARCH.md` — Country tiering, `country_indeed` mapping, EU/UK job boards, Workday employers, and European ATS selectors.
- [x] `docs/03_MULTI_PROFILE_ARCHITECTURE.md` — Technical design for `--profile` isolation, domain-adaptive tailoring (`software_engineering` vs `finance_accounting`), cover letter PDF engine, and EU/UK auto-apply logic.
- [x] `docs/04_IMPLEMENTATION_ROADMAP.md` — Step-by-step execution plan and verification checklist.
- [x] `docs/profiles/` — Pre-built EU/UK profile and search configuration templates for both **Tahir Siddique** (`tahir`) and **Neha Tariq, ACA** (`neha`).

---

## Phase 1: Core Architecture & Multi-Profile Isolation
1. **`src/applypilot/config.py`**:
   - Add `set_active_profile(profile_name: str | None)` and `get_active_profile() -> str | None` + `list_profiles() -> list[str]`.
   - Support `~/.applypilot/profiles/<profile_name>/` workspace isolation while sharing global `~/.applypilot/.env` API keys.
   - Add unified `load_location_filters(search_cfg)`, `is_location_allowed(location, search_cfg)`, and `is_title_allowed(title, search_cfg)` helpers supporting both `location_accept` and `location.accept_patterns` as well as `exclude_titles`.
2. **`src/applypilot/cli.py`**:
   - Add global `--profile / -p` option to `applypilot` CLI callback (and `APPLYPILOT_PROFILE` env var).
   - Add `applypilot profiles` command to display a side-by-side status table for all configured profiles (`tahir`, `neha`, etc.).
3. **`src/applypilot/database.py`**:
   - Add `company` and `pdf_generated_at` columns to `_ALL_COLUMNS` and `init_db()` (with automatic forward migration via `ensure_columns()`).
   - Fix `pipeline.py` streaming mode `_PENDING_SQL["pdf"]` to check `pdf_generated_at IS NULL` instead of `LIKE '%.txt'`, eliminating the infinite loop.

---

## Phase 2: Pan-EU & UK Discovery & Enrichment Upgrade
1. **`src/applypilot/discovery/jobspy.py`**:
   - Lazy-import `from jobspy import scrape_jobs` so ApplyPilot never crashes if `python-jobspy` is not installed.
   - Save `company` into the `jobs` table (`store_jobspy_results()`).
   - Support per-location `country_indeed` in `_run_one_search()` with automatic EU/UK country detection (`uk`, `ireland`, `netherlands`, `germany`, `luxembourg`, `switzerland`, `france`, `spain`, `poland`, `sweden`, `denmark`, `belgium`, `austria`, `italy`, `portugal`, `finland`).
   - Support both `boards:` and `sites:` keys in `searches.yaml` and enforce `exclude_titles`.
2. **`src/applypilot/discovery/workday.py` & `smartextract.py`**:
   - Use unified location & title filters from `config.py` (fixing the bug where 100% of non-remote jobs were dropped).
   - Populate `company` column from `employer_name` / extracted company data.
   - Wrap worker futures and `collect_page_intelligence()` in `try/except` so a single failing site never aborts a multi-site crawl.
3. **`src/applypilot/config/employers.yaml` & `sites.yaml`**:
   - Expand `employers.yaml` with **40+ top UK & EU Workday employers** across Big 4 / Advisory (PwC, BDO), UK/EU Banking & FinTech (Barclays, NatWest, Lloyds, Standard Chartered, Santander, Deutsche Bank, UBS, AXA, Aviva, Zurich, LSEG, Wise, Revolut, Klarna), and UK/EU Tech & Multinationals (AstraZeneca, GSK, Unilever, Haleon, RELX, Diageo, Dyson, ARM, Airbus, Siemens, ASML, Philips, Spotify, Sanofi).
   - Expand `sites.yaml` with UK & EU job boards for both Tech and ACA/Finance: `eFinancialCareers UK`, `ICAEW Jobs`, `ACCA Careers`, `Reed.co.uk`, `Totaljobs`, `CWJobs`, `CV-Library`, `Relocate.me`, `EuroTechJobs`, `GermanTechJobs`, `SwissDevJobs`, `Landing.jobs`, `NoFluffJobs`, `IrishJobs`.
4. **`src/applypilot/enrichment/detail.py`**:
   - Add CSS selectors for European ATS platforms (**Personio**, **Teamtailor**, **Pinpoint**, **SmartRecruiters**, **Workable**, **Recruitee**).
   - Fix thread-local connection cleanup (`close_connection()`).

---

## Phase 3: Dual-Domain Scoring, Tailoring, Validation & PDF Engine
1. **`src/applypilot/scoring/validator.py`**:
   - Make `FABRICATION_WATCHLIST` profile-aware: subtract all skills in `profile["skills_boundary"]` and `profile["resume_facts"]` and use word-boundary regex matching (`\b`).
   - Remove `"certif"` and `"certified"` from the unconditional watchlist for candidates with professional qualifications (such as **ACA / ICAEW / ACCA**) or when present in the base resume.
   - Support domain-adaptive section headers (`QUALIFICATIONS`, `CERTIFICATIONS`, `CORE COMPETENCIES`, and optional `PROJECTS` for finance/accounting CVs).
2. **`src/applypilot/scoring/tailor.py` & `cover_letter.py` & `scorer.py`**:
   - Use `job.get("company") or job.get("site")` so prompts reference the actual employer name.
   - Add domain-adaptive prompt builders supporting both:
     - `software_engineering` / `fpa_tech_hybrid` (Tahir Siddique)
     - `finance_accounting` / `chartered_accountant` (Neha Tariq, ACA)
   - Include a short URL hash (`hashlib.sha1(url.encode()).hexdigest()[:6]`) in tailored resume and cover letter filenames (`{safe_company}_{safe_title}_{hash}.txt`) to prevent overwrites across similar job titles.
   - In `scorer.py`, do not persist `fit_score = 0` when an LLM call fails, allowing automatic retry on the next run.
3. **`src/applypilot/scoring/pdf.py`**:
   - Escape all HTML entities (`html.escape()`) in `build_html()`.
   - Add `build_cover_letter_html()` so cover letters (`*_CL.txt`) render as clean, professional A4/Letter business letters instead of corrupted resume headers.
   - Support `QUALIFICATIONS` / `CERTIFICATIONS` sections in resume PDFs.
   - Reuse a single Playwright browser instance in `batch_convert()` and update `pdf_generated_at` in SQLite.

---

## Phase 4: UK & EU Auto-Apply Agent & Security Hardening
1. **`src/applypilot/apply/prompt.py`**:
   - Replace the hardcoded North American location check (which rejected `Europe`) with a profile-driven **UK & EU Location, Relocation & Visa Sponsorship Policy**.
   - Upgrade `_build_salary_section()` to support **multi-currency compensation** (`GBP £`, `EUR €`, `CHF`, `USD`) plus automatic conversion between annual gross, monthly gross (`÷ 12` for DE/NL/AT/FR), and daily rate (`÷ 220` for UK).
   - Add UK/EU standard screening fields: `notice_period`, `willing_to_relocate`, `visa_sponsorship_details`, professional body membership (`ACA / ICAEW`), and `DD/MM/YYYY` date formatting.
   - Isolate per-worker staged upload files in `APPLY_WORKER_DIR / f"worker-{worker_id}" / "uploads"`.
2. **`src/applypilot/apply/launcher.py` & `wizard/init.py`**:
   - Enforce `0o600` permissions on `profile.json` and `.env`, and redact passwords/API keys from `--gen` debug files.
   - Fix Windows port matching in `chrome.py:_kill_on_port()`.

---

## Phase 5: Automated Test Suite & CI Verification (Completed)
1. Create `tests/` with unit and integration tests covering:
   - Multi-profile isolation (`tahir` vs `neha`)
   - EU/UK location & title filtering and `country_indeed` resolution
   - Dual-domain validation (`software_engineering` and `finance_accounting` / ACA resumes)
   - Resume & Cover Letter HTML/PDF generation and HTML escaping
   - Database migrations (`company`, `pdf_generated_at`) and atomic `acquire_job` locking
2. Update `.github/workflows/ci.yml` to run Ruff and Pytest on `push` and `pull_request`.

---

## Phase 6: Next-Gen Core Engine, Multi-Provider LLM & CLI Unification (`SPEC-05`)
1. **`src/applypilot/pipeline.py` & `src/applypilot/cli.py`**:
   - Define `ALL_STAGES` and `AI_STAGES` in `pipeline.py` and unify `run_pipeline()` signature with `cli.py:run()` (supporting `sources`, `limit`, `workers`, `dry_run`, `skip_*`, `validation_mode`, `deduplicate`, `export_docx`).
   - Add `--workers / -w`, `--dry-run`, and `--all-profiles` flags to `applypilot run` and `applypilot status`.
2. **`src/applypilot/llm.py`**:
   - Add native support for **Anthropic Claude**, **OpenRouter**, **Groq**, and **DeepSeek** alongside **Gemini**, **OpenAI**, and **Local Ollama/llama.cpp**.
   - Implement automatic multi-provider failover on HTTP 429/502/503 and track token/cost/latency telemetry (`get_telemetry()`).
3. **`src/applypilot/wizard/importer.py`**:
   - Add `applypilot init --from-resume <path>` to automatically parse an existing plain-text or PDF/DOCX CV into a complete `profile.json`, `resume.txt`, and `searches.yaml`.

---

## Phase 7: Direct ATS API Connectors, Cross-Board Deduplication & Sponsor Registry (`SPEC-06`)
1. **`src/applypilot/discovery/ats_direct.py` & `config/employers.yaml`**:
   - Implement zero-browser, zero-LLM direct API scrapers for **Greenhouse**, **Lever**, **Ashby**, and **SmartRecruiters** that pre-populate `full_description` and `application_url` at discovery time.
   - Expand `employers.yaml` with `ats_employers` covering 40+ premier UK & EU FinTechs, Banks, AI labs, and Multinationals.
2. **`src/applypilot/database.py`**:
   - Add `fingerprint`, `sponsor_status`, `work_arrangement`, `ats_coverage_pct`, `score_breakdown`, `docx_resume_path`, `docx_cover_path`, `crm_stage`, `user_notes`, `prep_pack_path`, and `outreach_path` columns with automatic migration.
   - Implement `compute_job_fingerprint()` and `deduplicate_jobs()` with direct-ATS prioritization and field merging.
3. **`src/applypilot/enrichment/sponsors.py`**:
   - Build the UK Home Office Skilled Worker & EU Recognized Sponsor Matcher (`evaluate_sponsorship_and_metadata()`), automatically tagging jobs with `sponsor_status`, `work_arrangement` (`remote`/`hybrid`/`onsite`), and expired-job detection.

---

## Phase 8: Explainable 4D Scoring, ATS Coverage Engine & Native `.docx` Export (`SPEC-05`)
1. **`src/applypilot/scoring/ats.py`**:
   - Build deterministic ATS keyword extractor and coverage calculator (`compute_ats_coverage()`) measuring baseline vs. tailored resume keyword overlap (`ats_coverage_pct` and `delta_pct`).
2. **`src/applypilot/scoring/scorer.py` & `tailor.py`**:
   - Upgrade `scorer.py` to output 4 sub-scores (`skills_score`, `qualifications_score`, `domain_score`, `location_score`), `matched_keywords`, and `missing_keywords` stored in `score_breakdown`.
   - Pass missing ATS keywords from scoring into `tailor.py` and compute `ats_coverage_pct` upon tailoring completion.
3. **`src/applypilot/scoring/docx_export.py` & `pdf.py`**:
   - Build pure-Python OpenXML Word (`.docx`) exporter for tailored resumes and cover letters (`convert_resume_to_docx`, `convert_cover_letter_to_docx`, `batch_convert_docx`).
   - Add selectable PDF themes (`modern`, `executive_uk_eu`, `compact_ats`) to `pdf.py`.

---

## Phase 9: Career CRM Server, Interview Prep, Recruiter Outreach & Apply Memory (`SPEC-07`)
1. **`src/applypilot/crm.py` & `src/applypilot/view.py`**:
   - Upgrade `view.py` dashboard with Company names, 4D Sub-Score pills, ATS Coverage badges, UK/EU Sponsor badges, Work Arrangement filters, CRM Kanban stages, Resume Diff & Cover Letter modal viewer, and client-side CSV export.
   - Create `applypilot serve` (`crm.py`) running an interactive local Career CRM web server (`0.0.0.0`) with live stage updates, notes editing, and one-click Interview Prep / Outreach generation.
2. **`src/applypilot/prep.py`**:
   - Implement `applypilot prep` (Tailored Interview Prep Dossier with 8 role-specific questions + STAR answers grounded in `resume_facts`) and `applypilot outreach` (LinkedIn `<300` char connection note, Recruiter InMail, and Hiring Manager / Audit Partner cold email).
3. **`src/applypilot/apply/memory.py`, `export.py` & `notifications.py`**:
   - Implement persistent Screening Question Memory Bank (`question_bank` table + `applypilot memory` CLI) and Smart Error Classifier (`classify_apply_error` + `--retry-transient`).
   - Implement `applypilot export` (`csv`, `json`, `markdown`) and `notifications.py` (Slack/Discord/Telegram webhook alerts).

---

## Phase 10: Comprehensive End-to-End Verification
1. Add full unit & integration tests for all new modules (`test_ats_and_dedup.py`, `test_docx_and_ats_coverage.py`, `test_crm_prep_outreach_memory.py`, `test_cli_and_llm.py`).
2. Run `ruff check` and `pytest -v` across the entire test suite with 100% pass rate.

