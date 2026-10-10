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

## Phase 5: Automated Test Suite & CI Verification
1. Create `tests/` with unit and integration tests covering:
   - Multi-profile isolation (`tahir` vs `neha`)
   - EU/UK location & title filtering and `country_indeed` resolution
   - Dual-domain validation (`software_engineering` and `finance_accounting` / ACA resumes)
   - Resume & Cover Letter HTML/PDF generation and HTML escaping
   - Database migrations (`company`, `pdf_generated_at`) and atomic `acquire_job` locking
2. Update `.github/workflows/ci.yml` to run Ruff and Pytest on `push` and `pull_request`.
