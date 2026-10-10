# Direct ATS Discovery Connectors, Cross-Board Deduplication & UK/EU Sponsor Intelligence (`v0.5.0`)

**Document ID:** SPEC-06  
**Scope:** Zero-Browser Direct ATS Scrapers (Greenhouse, Lever, Ashby, SmartRecruiters, Personio, Workable, Recruitee), Cross-Board Fuzzy Deduplication, UK/EU Visa Sponsor Registry Matcher, and Structured Job Metadata Enrichment.

---

## 1. Zero-Browser Public ATS API Connectors (`src/applypilot/discovery/ats_direct.py`)

### 1.1. Motivation
While Workday's CXS API is already supported in `workday.py`, thousands of high-paying UK and European tech, fintech, banking, and multinational finance employers host their career portals on **Greenhouse**, **Lever**, **Ashby**, **SmartRecruiters**, **Personio**, **Workable**, and **Recruitee**.
All seven platforms expose **public, unauthenticated JSON or XML APIs** that return:
- Job title, location, department, and office
- **Full HTML/text job description** (meaning these jobs are **pre-enriched** at discovery time — 0 Playwright page loads and 0 LLM tokens needed for Stage 2!)
- Direct application URL

### 1.2. Supported ATS Endpoints
1. **Greenhouse Job Board API (`greenhouse`)**
   - Endpoint: `https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true`
   - Returns JSON `{"jobs": [{"id", "title", "absolute_url", "location": {"name"}, "content", "updated_at"}]}`.
2. **Lever Postings API (`lever`)**
   - Endpoint: `https://api.lever.co/v0/postings/{site_token}?mode=json`
   - Returns JSON list of postings with `text` (title), `hostedUrl`, `applyUrl`, `categories.location`, `descriptionPlain`, `lists`.
3. **Ashby Posting API (`ashby`)**
   - Endpoint: `https://api.ashbyhq.com/posting-api/job-board/{board_name}?includeCompensation=true`
   - Returns JSON `{"jobs": [{"title", "location", "jobUrl", "applyUrl", "descriptionPlain", "compensation"}]}`.
4. **SmartRecruiters Posting API (`smartrecruiters`)**
   - Endpoint: `https://api.smartrecruiters.com/v1/companies/{company_id}/postings`
5. **Personio Career Feed API (`personio`)**
   - Endpoint: `https://{token}.jobs.personio.de/search.json` (and XML `<workzag-jobs><position>` feed fallback)
6. **Workable Widget API (`workable`)**
   - Endpoint: `https://apply.workable.com/api/v1/widget/accounts/{token}?details=true`
7. **Recruitee Offers API (`recruitee`)**
   - Endpoint: `https://{token}.recruitee.com/api/offers`

### 1.3. Pre-Configured UK & EU ATS Employer Registry (`config/employers.yaml`)
Add an `ats_employers:` section to `src/applypilot/config/employers.yaml` with leading UK & EU employers hiring both Finance/Accounting (ACA, ACCA, FP&A, Audit, Financial Reporting) and Software/AI Engineers:
- **Greenhouse**: Monzo (`monzo`), Starling Bank (`starlingbank`), Checkout.com (`checkoutcom`), Deliveroo (`deliveroo`), Stripe (`stripe`), Wise (`wise`), Adyen (`adyen`), Databricks (`databricks`), Cloudflare (`cloudflare`), Elastic (`elastic`), Contentful (`contentful`), Celonis (`celonis`), N26 (`n26`), Trade Republic (`traderepublic`), Qonto (`qonto`),Alan (`alan`), Helsing (`helsing`), Wayve (`wayve`), Synthesia (`synthesia`), GoCardless (`gocardless`).
- **Lever**: Mistral AI (`mistral`), Palantir (`palantir`), Spotify (`spotify`), Klarna (`klarna`), Plaid (`plaid`), Kraken (`kraken`), Bittrex/FinTechs.
- **Ashby**: DeepL (`deepl`), Linear (`linear`), PostHog (`posthog`), Ramp (`ramp`), ElevenLabs (`elevenlabs`), Synthesia/AI scaleups.
- **SmartRecruiters**: Visa (`Visa`), Bosch (`BoschGroup`), Adidas (`adidas`), Pernod Ricard (`PernodRicard`), Publicis (`PublicisGroupe`), Skechers/Equinix (`Equinix`).

### 1.4. Pre-Enrichment on Ingest
When `ats_direct.py` ingests a job that already includes its full description (`len(description) >= 200`), it populates `full_description`, `application_url`, and `detail_scraped_at` immediately in SQLite so the `enrich` stage skips redundant scraping.

---

## 2. Cross-Board Fuzzy Deduplication Engine (`src/applypilot/database.py`)

### 2.1. Problem
The same job opening (e.g., *"Financial Accountant (ACA) — Revolut — London, UK"*) frequently appears on LinkedIn, Indeed UK, Reed.co.uk, eFinancialCareers, and Revolut's direct Greenhouse/Workday board. Deduplicating only by `url` leaves 4–5 identical jobs in the database, multiplying LLM scoring and tailoring costs.

### 2.2. Normalized Job Fingerprinting
- Add `fingerprint TEXT` column to `jobs` table in `database.py`.
- `compute_job_fingerprint(company: str | None, title: str | None, location: str | None) -> str`:
  - Lowercases and strips corporate suffixes (`ltd`, `limited`, `plc`, `inc`, `corp`, `gmbh`, `ag`, `b.v.`, `s.a.`, `group`, `holdings`, `uk`, `europe`).
  - Normalizes title by removing parenthetical noise like `(m/f/d)`, `(hybrid)`, `(remote)`, `ref: 12345`, while keeping core seniority and role tokens.
  - Extracts primary city/country token from location (`london`, `dublin`, `amsterdam`, `berlin`, `remote`, etc.).
  - Produces a deterministic key: `f"{norm_company}::{norm_title}::{norm_loc}"`.

### 2.3. Smart Deduplication & Direct-ATS Prioritization (`deduplicate_jobs(conn)`)
- When multiple rows share the same `fingerprint`:
  - Rank rows by source quality:
    1. **Direct Employer ATS** (`strategy` in `workday_api`, `greenhouse_api`, `lever_api`, `ashby_api`, `smartrecruiters_api`, or direct career site)
    2. **Already applied / already tailored / already scored**
    3. **Richest description** (`len(full_description or description)`)
  - Keep the highest-ranked canonical row, merge any missing fields (`salary`, `full_description`, `application_url`, `fit_score`) from duplicate rows into the canonical row, and remove the redundant aggregator duplicates.

---

## 3. UK & EU Visa Sponsorship & Structured Job Intelligence (`src/applypilot/enrichment/sponsors.py`)

### 3.1. Built-In UK & EU Licensed Visa Sponsor Matcher
Create `src/applypilot/enrichment/sponsors.py` with a curated, fast-lookup registry of **UK Home Office Skilled Worker Licensed Sponsors**, **Netherlands IND Recognized Sponsors**, **Ireland Critical Skills Employers**, and **Germany/EU Blue Card Sponsors** (including all Big 4, top 25 audit/accounting firms, FTSE 100/250, European banks, FinTechs, and global tech companies), plus JD text analysis:
- `evaluate_sponsorship_and_metadata(company: str | None, title: str | None, description: str | None, location: str | None) -> dict`:
  - Detects explicit negative phrases in JD (*"cannot provide sponsorship"*, *"must have existing right to work without sponsorship"*, *"no visa sponsorship"*, *"security clearance required"*, *"SC cleared"*) → `sponsor_status = "no_sponsorship_stated"`.
  - Detects explicit positive phrases (*"visa sponsorship available"*, *"skilled worker visa"*, *"relocation package"*, *"eu blue card"*, *"critical skills permit"*, *"ind recognized sponsor"*) → `sponsor_status = "sponsorship_offered"`.
  - Checks employer name against the UK/EU Licensed Sponsor Registry → `sponsor_status = "licensed_sponsor"`.
  - Otherwise → `sponsor_status = "unknown"`.
- Also extracts:
  - `work_arrangement`: `"remote"` | `"hybrid"` | `"onsite"`
  - `is_expired`: `bool` (detects closed/expired job page markers)
  - `extracted_qualifications`: `list[str]` (e.g., `["ACA", "ACCA", "IFRS", "SOX"]`)

### 3.2. SQLite Schema Additions (`database.py`)
Add to `_ALL_COLUMNS` and `init_db()` with automatic forward migration via `ensure_columns()`:
- `fingerprint TEXT`
- `sponsor_status TEXT` (`sponsorship_offered`, `licensed_sponsor`, `no_sponsorship_stated`, `unknown`)
- `work_arrangement TEXT` (`remote`, `hybrid`, `onsite`)
- `ats_coverage_pct INTEGER`
- `score_breakdown TEXT` (JSON with sub-scores & matched/missing keywords)
- `docx_resume_path TEXT`
- `docx_cover_path TEXT`
- `crm_stage TEXT DEFAULT 'discovered'` (`discovered`, `scored`, `tailored`, `ready`, `applied`, `assessment`, `interviewing`, `offer`, `rejected`, `archived`)
- `user_notes TEXT`
- `prep_pack_path TEXT`
- `outreach_path TEXT`
