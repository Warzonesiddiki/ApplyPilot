# ApplyPilot v0.3.0 — Technical Audit & EU/UK Multi-Profile Gap Analysis

**Date:** 2026-10-10  
**Scope:** Full codebase audit + readiness assessment for dual-candidate deployment (**Tahir Siddique** & **Neha Tariq, ACA**) targeting the **United Kingdom (UK)** and **European Union (EU / EEA / Switzerland)**.

---

## 1. Executive Summary

ApplyPilot `v0.3.0` is built with a solid 6-stage pipeline architecture (`discover` → `enrich` → `score` → `tailor` → `cover` → `pdf` / `apply`), but it was originally authored for **a single software engineer located in North America (US/Canada)**.

Attempting to run `v0.3.0` as-is for **two candidates** (one Tech / AI / FP&A Engineering profile and one ACA Chartered Accountant / Finance profile) targeting the **UK and EU** will fail due to **7 critical blockers**:

| # | Blocker | Severity | Location in Codebase | Impact on Your Goal |
|---|---------|----------|----------------------|---------------------|
| **B1** | **Europe is hardcoded as `NOT ELIGIBLE` in the Auto-Apply Agent Prompt** | **P0 (Blocker)** | `src/applypilot/apply/prompt.py:100` | Literal instruction: `- City is overseas (India, Philippines, Europe, etc.) with no remote option -> NOT ELIGIBLE. Output RESULT:FAILED:not_eligible_location`. Every hybrid/onsite EU/UK job is auto-aborted by the agent! |
| **B2** | **Certifications (`"certif"`, `"certified"`) are hardcoded as "Fabrication" errors** | **P0 (Blocker)** | `src/applypilot/scoring/validator.py:64` | `FABRICATION_WATCHLIST` bans `"certif"` and `"certified"`. Any ACA / ICAEW / ACCA / Chartered Accountant resume or financial certification triggers an automatic validation failure. |
| **B3** | **Single Global User Directory (`~/.applypilot/`)** | **P0 (Blocker)** | `src/applypilot/config.py:10-28` | Only one `profile.json`, `resume.txt`, `searches.yaml`, and `applypilot.db` can exist. Running for both Tahir and Neha overwrites resumes and mixes job queues. |
| **B4** | **100% of Non-Remote Jobs Dropped by Location Filter Bug** | **P0 (Blocker)** | `discovery/jobspy.py:72`, `workday.py:39`, `smartextract.py:42` | Code reads `location_accept`, whereas `searches.example.yaml` defines `location.accept_patterns` and `wizard/init.py` writes neither. Empty `accept` list causes `_location_ok()` to return `False` for all non-remote jobs. |
| **B5** | **Tech-Only Resume Schema, Prompts & Section Validator** | **P0 (Blocker)** | `scoring/tailor.py:72-119`, `validator.py:67, 102` | Hardcodes software engineering categories (`Languages`, `Frameworks`, `DevOps & Infra`, `Databases`, `Tools`), requires a `PROJECTS` section, and instructs the LLM to *"Write like a real engineer"*, which is unsuitable for ACA Accounting / Audit / IFRS / Corporate Finance roles. |
| **B6** | **North-America-Only Discovery Configs (`country_indeed="usa"`, Canadian Workday/Sites)** | **P1 (Major)** | `discovery/jobspy.py:216`, `config/employers.yaml`, `config/sites.yaml` | `jobspy.py` only queries a single `country_indeed` (default `"usa"`), missing UK and EU country boards (`uk`, `germany`, `netherlands`, `ireland`, `france`, `luxembourg`, etc.). `employers.yaml` is ~80% Canadian banks/pension funds. |
| **B7** | **Corrupted Cover Letter PDFs & Worker Upload Collisions** | **P0 (Blocker)** | `scoring/pdf.py:372`, `apply/prompt.py:424` | `convert_to_pdf()` parses cover letters as resumes (rendering `"Dear Hiring Manager,"` as an 18pt name header and dropping the sign-off). `prompt.py` writes all worker uploads to a single shared `apply-workers/current/` folder. |

---

## 2. Detailed Codebase Audit Findings

### 2.1. Pipeline & Data Layer Bugs
1. **Missing `company` Column in SQLite (`database.py:72-125`, `jobspy.py:114`)**:
   - `jobspy.py:114` extracts `company = str(row.get("company", ""))` into a dead variable and never stores it.
   - `scorer.py`, `tailor.py`, and `cover_letter.py` pass `COMPANY: {job['site']}` to the LLM. For jobs scraped from Indeed, LinkedIn, Reed, or eFinancialCareers, the LLM writes cover letters addressed to `"indeed"` or `"linkedin"` instead of the actual employer!
2. **Filename Collisions Overwrite Tailored CVs (`tailor.py:510-514`, `cover_letter.py:244-248`)**:
   - Files are named `{safe_site}_{safe_title}.txt` (e.g., `linkedin_Financial_Analyst.txt` or `indeed_Software_Engineer.txt`). Multiple openings with the same title on the same board overwrite each other on disk.
3. **Infinite Loop in Streaming Mode (`pipeline.py:224-227`)**:
   - `_PENDING_SQL["pdf"]` checks `WHERE tailored_resume_path LIKE '%.txt'`, which never changes after `batch_convert()`, causing `applypilot run --stream` to loop forever.
4. **Permanent `fit_score = 0` on Transient LLM Errors (`scorer.py:88, 156`)**:
   - If the LLM rate-limits or times out during `score_job()`, `score = 0` is written to `fit_score`, preventing the job from ever being retried on subsequent runs.
5. **Unescaped HTML in PDF Generation (`scoring/pdf.py:165-225`)**:
   - Resume fields are interpolated directly into HTML without `html.escape()`. Text like `<10ms` or `C++` template syntax or `&` can corrupt the DOM rendered by Playwright.

### 2.2. EU & UK Localization Gaps in Auto-Apply (`apply/prompt.py`)
1. **Hardcoded Overseas Rejection (`prompt.py:100`)**:
   - Explicitly lists `Europe` as an ineligible overseas location. Must be replaced with an EU/UK-aware location & relocation/sponsorship policy.
2. **Currency & Compensation Logic (`prompt.py:106-141`)**:
   - Assumes `$` prefix, `USD` / `CAD`, `$110K` senior minimum, and `annual / 2080` hourly conversion.
   - UK & EU roles post salaries in **GBP (`£`)**, **EUR (`€`)**, **CHF (`CHF`)**, **PLN**, **SEK**, **DKK**, either as annual gross, monthly gross (common in Germany/Austria/Netherlands/France), or daily contractor rates (UK Inside/Outside IR35).
3. **Date Formatting (`prompt.py:553`)**:
   - Uses `%m/%d/%Y` (US format). UK and EU application forms expect `DD/MM/YYYY` (or ISO `YYYY-MM-DD`).
4. **Right-to-Work & Visa Sponsorship Nuances (`prompt.py:144-188`)**:
   - UK and EU employers ask specific questions:
     - *"Do you currently have the legal right to work in the UK / EU?"*
     - *"Will you require Skilled Worker visa sponsorship / EU Blue Card / Employment Permit?"*
     - *"What is your notice period?"* (UK/EU standard question on almost every ATS form).
5. **European ATS Platforms**:
   - In addition to Workday, Greenhouse, Lever, and Ashby, UK and EU employers heavily use **Personio** (`*.jobs.personio.de` / `.com`), **Teamtailor**, **SmartRecruiters**, **Pinpoint**, **Workable**, **Recruitee**, and **Softgarden**. Enrichment CSS selectors (`enrichment/detail.py`) need patterns for these European ATS platforms.

### 2.3. Security & Privacy Hardening
1. **Claude Code Tool Allowlisting (`apply/launcher.py:321-330`)**:
   - Uses `--permission-mode bypassPermissions` with only a partial `--disallowedTools` list for Gmail, leaving `Bash` and file tools exposed to indirect prompt injection from untrusted job pages.
2. **CapSolver Key Exposure (`apply/prompt.py:248, 275`)**:
   - Injects `CAPSOLVER_API_KEY` into `browser_evaluate` `fetch()` calls inside the untrusted webpage DOM.
3. **File Permissions (`wizard/init.py`)**:
   - `profile.json` and `.env` should be written with `0o600` permissions, and `--gen` debug prompts should redact passwords and API keys.
