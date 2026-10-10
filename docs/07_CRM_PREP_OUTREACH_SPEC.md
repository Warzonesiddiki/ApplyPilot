# Career CRM Web App, Interview Prep Dossier, Outreach & Auto-Apply Memory Specification (`v0.5.0`)

**Document ID:** SPEC-07  
**Scope:** Interactive Local Career CRM (`applypilot serve` & `applypilot dashboard`), AI Interview Prep Pack (`applypilot prep`), Recruiter & Partner Outreach (`applypilot outreach`), Screening Question Memory Bank (`question_bank`), Smart Retry Classifier, CSV/JSON/Excel Export (`applypilot export`), and Webhook Notifications.

---

## 1. Interactive Career CRM & Enhanced Dashboard (`src/applypilot/crm.py` & `view.py`)

### 1.1. Full-Lifecycle Career CRM Stages
Every job in SQLite tracks a `crm_stage` (automatically synchronized with pipeline progress unless manually overridden by the candidate):
1. `discovered` — Found via JobSpy, Workday, Direct ATS, or SmartExtract
2. `scored` — Evaluated by AI with 4-dimensional sub-scores
3. `tailored` — Tailored CV (`.txt`, `.pdf`, `.docx`) & Cover Letter generated
4. `ready` — Ready for auto-apply or manual submission
5. `applied` — Submitted with timestamp & proof
6. `assessment` — Online assessment / technical or accounting case study stage
7. `interviewing` — Recruiter screen, Hiring Manager / Partner interview
8. `offer` — Offer received / negotiation
9. `rejected` — Closed / rejected

### 1.2. Upgraded Dashboard (`view.py`) & Interactive Web Server (`applypilot serve`)
1. **Rich Static + Interactive HTML Dashboard (`view.py`)**:
   - Displays **Multi-Profile Banner**, **KPI Summary Cards** (Total, Enriched, High-Fit `7+`, Tailored, Applied, Interviewing/Offers, Avg ATS Coverage %, Verified UK/EU Sponsors), **CRM Funnel Breakdown**, **Score Distribution**, **Jobs by Source**, and **Filterable Job Cards**.
   - Each job card displays:
     - **Company Name** (fixed from `v0.3.0` where only `site` was shown)
     - **Fit Score Badge** + **4 Sub-Score Pills** (`Skills`, `Qualifications`, `Domain`, `Location`)
     - **ATS Keyword Coverage Badge** (e.g., `ATS Match: 88% (+24%)`)
     - **UK/EU Visa Sponsor Badge** (`✓ Licensed UK/EU Sponsor`, `★ Sponsorship Offered`, or `⚠ No Sponsorship Stated`)
     - **Work Arrangement Pill** (`Remote`, `Hybrid`, `Onsite`)
     - **Interactive Resume Diff & Cover Letter Preview Drawer** (view tailored CV, base vs. tailored diff, cover letter, and interview prep notes right inside the browser)
   - Client-side filters for **Search Text**, **Min Score**, **Sponsor Status**, **Work Arrangement**, and **CRM Stage**, plus a **One-Click CSV Export** button in the browser.
2. **Local Interactive Career CRM Server (`applypilot serve --port 8080`)**:
   - Built with Python's standard `http.server.ThreadingHTTPServer` bound to `0.0.0.0` (compatible with live browser previews and local desktop use with zero extra dependencies).
   - Provides REST API endpoints:
     - `GET /` — Serves the interactive Career CRM web application
     - `GET /api/jobs` — Returns JSON list of jobs + stats + available profiles
     - `POST /api/jobs/stage` — Updates `crm_stage` and `user_notes` for a job URL
     - `POST /api/jobs/prep` — Generates an Interview Prep Pack & Outreach messages for a job on demand
     - `GET /api/export.csv` — Downloads filtered jobs as CSV

---

## 2. AI Interview Prep Pack & Outreach Generator (`src/applypilot/prep.py`)

### 2.1. `applypilot prep` — Tailored Interview Dossier
- CLI command: `applypilot prep [URL_OR_QUERY] [--profile/-p <slug>]`
- Generates a structured Markdown dossier saved to `~/.applypilot/profiles/<profile>/interview_prep/{company}_{title}_{hash}_PREP.md` (and updates `jobs.prep_pack_path`):
  1. **Role & Employer Brief**: Key priorities extracted from the job description, company context, and ATS keywords.
  2. **Candidate Value Proposition (60-Second Elevator Pitch)**: Tailored to the candidate's domain (`ACA Chartered Accountant` for Neha; `ACCA Finalist / Senior Accountant & ERP Specialist` for Tahir; or `Senior Software/AI Engineer`).
  3. **8 Role-Specific Interview Questions & Tailored STAR Answers**:
     - Technical / IFRS / ISA Audit / FP&A / ERP / Engineering questions directly targeting the JD requirements.
     - Concrete **Situation – Task – Action – Result (STAR)** answers grounded strictly in `profile["resume_facts"]` (`preserved_companies`, `real_metrics`, `certifications`).
  4. **Gap Mitigation Strategy**: How to address any `missing_keywords` or UK/EU relocation/sponsorship questions confidently.
  5. **5 High-Impact Questions to Ask the Interviewer / Audit Partner / Finance Director / Hiring Manager**.
- Works both with an LLM (when configured) and with a deterministic, high-precision template engine fallback (when running in Tier 1 / offline mode) so `applypilot prep` **always** succeeds!

### 2.2. `applypilot outreach` — LinkedIn & Hiring Manager Outreach
- CLI command: `applypilot outreach [URL_OR_QUERY] [--profile/-p <slug>]`
- Generates 3 ready-to-send outreach templates saved to `~/.applypilot/profiles/<profile>/outreach/{company}_{title}_{hash}_OUTREACH.md` (and updates `jobs.outreach_path`):
  1. **LinkedIn Connection Request Note (< 300 characters)** — strictly under LinkedIn's 300-character limit, referencing the exact role, qualification (`ACA` / `ACCA Finalist` / `Senior Engineer`), and 1 headline metric.
  2. **LinkedIn InMail / Direct Message to Talent Acquisition / Recruiter (80–120 words)**.
  3. **Direct Cold Email to Hiring Manager / Finance Director / Audit Partner / VP Engineering** with Subject Line + concise 3-paragraph body.

---

## 3. Screening Question Memory Bank & Smart Apply Retry (`src/applypilot/apply/memory.py`)

### 3.1. Persistent Screening Question Memory Bank (`question_bank` Table)
- Create a `question_bank` table in SQLite:
  ```sql
  CREATE TABLE IF NOT EXISTS question_bank (
      question_key      TEXT PRIMARY KEY,
      question_text     TEXT NOT NULL,
      canonical_answer  TEXT NOT NULL,
      category          TEXT,
      times_used        INTEGER DEFAULT 1,
      updated_at        TEXT
  )
  ```
- Seed default canonical Q&A entries from `profile.json` (Notice Period, Legal Right to Work in UK/EU, Visa Sponsorship Requirement, Willingness to Relocate, Salary Expectations in GBP/EUR/CHF, Professional Qualification Status, Years of Experience).
- Inject stored canonical answers into `apply/prompt.py:build_prompt()` so the browser agent answers recurring ATS questions identically and instantaneously across applications.
- CLI command `applypilot memory` to list, add, or update canonical screening answers.

### 3.2. Smart Apply Error Classifier & Retry (`classify_apply_error`)
- Classify `apply_error` strings into structured categories:
  - `retryable`: `timeout`, `network`, `rate_limit`, `browser_crash`, `element_detached`, `502`, `503`
  - `captcha_blocked`: `captcha`, `turnstile`, `cloudflare`, `bot_detected`
  - `auth_or_sso`: `sso`, `verification_code`, `login_required`, `account_locked`
  - `permanent_ineligible`: `not_eligible`, `expired`, `closed`, `no_sponsorship`, `manual_ats`
- Add `--retry-transient` flag to `applypilot apply` to reset only `retryable` (and optionally `captcha_blocked`) jobs while keeping `permanent_ineligible` jobs closed.

---

## 4. Data Export & Webhook Notifications (`src/applypilot/export.py` & `notifications.py`)

### 4.1. Multi-Format Pipeline Export (`applypilot export`)
- CLI command: `applypilot export [--format csv|json|markdown] [--min-score 0] [--output <path>] [--profile/-p <slug>]`
- Exports all jobs (with company, title, location, fit score, sub-scores, ATS coverage %, sponsor status, work arrangement, CRM stage, URLs, and file paths) to CSV, JSON, or a Markdown summary report.

### 4.2. Webhook Notification Dispatcher (`src/applypilot/notifications.py`)
- Supports `WEBHOOK_URL` (Slack, Discord, or generic JSON webhook) and `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in `.env`.
- Automatically sends notifications or Daily Digests (`send_daily_digest()`) when:
  - High-fit jobs (`fit_score >= 8`) are discovered & scored
  - A pipeline run or scheduled daemon cycle (`applypilot run --daemon --interval 360`) completes

---

## 5. Post-Application Email Lifecycle & Inbox Sync (`src/applypilot/email_sync.py`)

- CLI command: `applypilot sync-email [--inbox-file <path>] [--no-auto-prep] [--profile/-p <slug>]`
- REST endpoint: `POST /api/sync-email`
- Classifies incoming recruiting emails (`classify_application_email`) into:
  - `application_received` → `crm_stage = "applied"`, `apply_status = "applied"`
  - `assessment` → `crm_stage = "assessment"`
  - `interview_invite` → `crm_stage = "interviewing"`
  - `offer` → `crm_stage = "offer"`
  - `rejected` → `crm_stage = "rejected"`
- Matches emails to tracked jobs in SQLite by normalized company name and role title (`match_email_to_job`), prevents stage downgrades via `STAGE_PRIORITY`, appends timestamped audit notes to `user_notes`, and automatically generates an **Interview Prep Dossier** when an interview or online assessment invite is detected.

---

## 6. Native Python Application Proof-Packet Engine (`src/applypilot/apply/native_agent.py`)

- CLI command: `applypilot apply --engine native` (also automatic fallback in `--engine auto` when the external `claude` CLI binary is not installed)
- REST endpoint: `POST /api/jobs/proof`
- Parses HTML form inputs (`extract_form_fields_from_html`), maps candidate profile fields + canonical Screening Question Memory Bank answers (`question_bank`), resolves tailored PDF/DOCX CVs and cover letters, and writes an auditable JSON **Pre-Submission Proof Packet** (`~/.applypilot/proofs/<job>_PROOF.json`).

