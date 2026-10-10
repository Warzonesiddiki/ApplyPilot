# Multi-Profile & EU/UK Technical Architecture Specification

**Document ID:** ARCH-03  
**Target Release:** ApplyPilot `v0.4.0` (Multi-Profile + EU/UK + Dual Domain Edition)

---

## 1. Multi-Profile Workspace Isolation (`src/applypilot/config.py` & `cli.py`)

### 1.1. Directory Layout
Currently, `config.py` hardcodes all paths at `~/.applypilot/`. To support running independent pipelines for **Tahir Siddique** (`tahir`) and **Neha Tariq, ACA** (`neha`) without any cross-contamination of resumes, databases, or browser sessions, we introduce a **Profile Context**:

```
~/.applypilot/
├── .env                                 # Shared global LLM & CapSolver API keys
├── active_profile                       # Optional pointer to current default profile (e.g. "tahir")
└── profiles/
    ├── tahir/
    │   ├── profile.json                 # Tahir's personal, visa, salary (GBP/EUR), skills & resume facts
    │   ├── resume.txt                   # Tahir's master plain-text CV
    │   ├── resume.pdf                   # Tahir's master PDF CV
    │   ├── searches.yaml                # Tahir's EU/UK AI, Full-Stack & FP&A Engineering queries
    │   ├── applypilot.db                # Tahir's isolated SQLite database
    │   ├── dashboard.html               # Tahir's HTML dashboard
    │   ├── tailored_resumes/            # Tahir's per-job tailored CVs (.txt, .pdf, _REPORT.json)
    │   ├── cover_letters/               # Tahir's per-job cover letters (.txt, .pdf)
    │   ├── logs/                        # Tahir's apply logs
    │   ├── chrome-workers/              # Tahir's isolated Chrome session profiles
    │   └── apply-workers/               # Tahir's per-worker upload staging
    └── neha/
        ├── profile.json                 # Neha's ACA qualification, Audit/IFRS/FP&A skills & facts
        ├── resume.txt                   # Neha's master plain-text CV
        ├── resume.pdf                   # Neha's master PDF CV
        ├── searches.yaml                # Neha's EU/UK ACA, Audit, IFRS & Finance queries
        ├── applypilot.db                # Neha's isolated SQLite database
        ├── dashboard.html               # Neha's HTML dashboard
        ├── tailored_resumes/            # Neha's per-job tailored CVs
        ├── cover_letters/               # Neha's per-job cover letters
        ├── logs/                        # Neha's apply logs
        ├── chrome-workers/              # Neha's isolated Chrome session profiles
        └── apply-workers/               # Neha's per-worker upload staging
```

### 1.2. Backwards Compatibility & CLI UX
- Global `--profile / -p <name>` option on `applypilot` (or `APPLYPILOT_PROFILE` environment variable):
  ```bash
  applypilot -p tahir init
  applypilot -p tahir run -w 4
  applypilot -p tahir apply -w 2

  applypilot -p neha init
  applypilot -p neha run -w 4
  applypilot -p neha apply -w 2

  applypilot profiles            # New command: list all profiles and their pipeline stats side-by-side
  ```
- If `--profile` is omitted and legacy files exist directly in `~/.applypilot/profile.json`, it seamlessly falls back to the root `~/.applypilot/` directory so existing single-user setups and tests never break.
- Dynamic path resolution in `config.py`: `set_active_profile(profile_name: str)` updates `APP_DIR`, `DB_PATH`, `PROFILE_PATH`, `RESUME_PATH`, `RESUME_PDF_PATH`, `SEARCH_CONFIG_PATH`, `TAILORED_DIR`, `COVER_LETTER_DIR`, `LOG_DIR`, `CHROME_WORKER_DIR`, and `APPLY_WORKER_DIR`, while keeping `GLOBAL_ENV_PATH = BASE_APP_DIR / ".env"` shared across all profiles.

---

## 2. Domain-Adaptive Tailoring & Validation Engine (`scoring/`)

### 2.1. Profile Domain Classification
We add `"domain"` to `profile.json` (auto-detected if omitted based on `target_role` and `skills_boundary`):
- `"software_engineering"` (or `"fpa_tech_hybrid"`): For **Tahir Siddique** — emphasizes system architecture, AI/agentic workflows, Python/TypeScript/React/FastAPI, financial calculation engines, latency/automation metrics, and engineering projects.
- `"finance_accounting"`: For **Neha Tariq, ACA** — emphasizes Chartered Accountancy (`ACA`), statutory & external audit (ISA), financial reporting (`IFRS`, `IAS`, `UK GAAP`, `FRS 102`), internal controls (`SOX`), FP&A, consolidation, tax/advisory, Big 4 / mid-tier client portfolio metrics, and professional qualifications.

### 2.2. Dynamic Resume Schema & Sections
| Aspect | Software / AI Engineering (`tahir`) | ACA Chartered Accountancy & Finance (`neha`) |
|---|---|---|
| **Persona in Tailor Prompt** | Senior Technical Recruiter & Engineering Hiring Manager | Senior Finance, Audit & Executive Accounting Recruiter (Big 4 / FTSE / Multinational) |
| **Skills Section Header** | `TECHNICAL SKILLS` | `CORE COMPETENCIES & TECHNICAL SKILLS` (or `TECHNICAL SKILLS`) |
| **Skill Categories** | Driven dynamically by keys in `profile["skills_boundary"]` (e.g., `Languages`, `Frameworks`, `AI & Agentic Systems`, `FP&A & Financial Engines`, `Cloud & DevOps`) | Driven dynamically by keys in `profile["skills_boundary"]` (e.g., `Accounting & Reporting Standards`, `Audit & Assurance`, `FP&A & Commercial Finance`, `ERP & Financial Systems`, `Tax & Compliance`) |
| **Qualifications / Certifications** | Optional (`CERTIFICATIONS` or merged into `EDUCATION`) | First-class `QUALIFICATIONS & CERTIFICATIONS` section (preserving `ACA` / `ICAEW` / `ICAP` membership & exam passes) |
| **Projects Section** | Required or Optional (`PROJECTS`) | Optional (`PROJECTS` or `KEY ENGAGEMENTS & ACHIEVEMENTS` — never forced if the base CV has no software projects) |
| **Fabrication Watchlist** | Excludes all skills in `profile["skills_boundary"]` and `profile["resume_facts"]["certifications"]` | Removes the blanket ban on `"certif"` / `"certified"` / `"aca"` / `"acca"` when present in the candidate's profile or base CV |

### 2.3. Dedicated Cover Letter PDF Renderer (`scoring/pdf.py`)
- `convert_to_pdf()` will inspect whether the input file is a cover letter (`*_CL.txt` or starts with `"Dear "`) vs. a structured resume:
  - **Resume**: Uses `parse_resume()` + `build_html()` with full `html.escape()` protection on all text fields and support for `QUALIFICATIONS` / `CERTIFICATIONS` sections.
  - **Cover Letter**: Uses a new `build_cover_letter_html(text, profile)` renderer that formats the candidate's header (name, qualification/title, email, phone, LinkedIn, location), current date (`DD Month YYYY`), salutation, cleanly spaced body paragraphs, and sign-off on a single A4/Letter page.

---

## 3. Pan-EU & UK Discovery Engine (`discovery/`)

### 3.1. Per-Location `country_indeed` in `jobspy.py`
Currently `jobspy.py:216` uses a single global `defaults.get("country_indeed", "usa")` for all locations.
- **Upgrade**: Each entry in `locations:` in `searches.yaml` can specify its own `country_indeed` (e.g., `location: "London, UK"`, `country_indeed: "uk"`; `location: "Amsterdam, Netherlands"`, `country_indeed: "netherlands"`; `location: "Dublin, Ireland"`, `country_indeed: "ireland"`).
- Furthermore, if `country_indeed` is omitted on a location entry, an automatic EU/UK country resolver maps `"UK"` / `"London"` → `"uk"`, `"Ireland"` / `"Dublin"` → `"ireland"`, `"Netherlands"` / `"Amsterdam"` → `"netherlands"`, `"Germany"` / `"Berlin"` / `"Frankfurt"` → `"germany"`, `"Luxembourg"` → `"luxembourg"`, `"Switzerland"` / `"Zurich"` → `"switzerland"`, `"France"` / `"Paris"` → `"france"`, `"Spain"` / `"Madrid"` → `"spain"`, `"Poland"` / `"Warsaw"` → `"poland"`, `"Sweden"` / `"Stockholm"` → `"sweden"`, `"Denmark"` / `"Copenhagen"` → `"denmark"`, `"Belgium"` / `"Brussels"` → `"belgium"`, `"Austria"` / `"Vienna"` → `"austria"`, `"Italy"` / `"Milan"` → `"italy"`, `"Portugal"` / `"Lisbon"` → `"portugal"`, `"Finland"` / `"Helsinki"` → `"finland"`.

### 3.2. Unified Location & Title Filtering (`config.py` + `discovery/*`)
- Centralize `check_location_allowed(location, search_cfg)` and `check_title_allowed(title, search_cfg)` in `config.py` and call them across `jobspy.py`, `workday.py`, and `smartextract.py`.
- Support both `location_accept` and `location.accept_patterns` (and `location_reject_non_remote` / `location.reject_patterns`).
- Enforce `exclude_titles` from `searches.yaml` during discovery so irrelevant roles (e.g., German-only nursing/retail or unrelated internships) never pollute the database or waste LLM scoring tokens.

---

## 4. UK & EU Auto-Apply Prompt & Multi-Currency Engine (`apply/prompt.py`)

### 4.1. EU/UK Location & Relocation Eligibility Check
Replace the hardcoded North American check in `prompt.py:80-103` with a profile-driven regional policy:
- Any role in the candidate's `target_regions` (**United Kingdom, Ireland, Netherlands, Germany, Luxembourg, Switzerland, Sweden, Denmark, Belgium, France, Spain, Portugal, Italy, Austria, Poland, Finland, Malta, Cyprus, or EU/EMEA Remote**) is **ELIGIBLE**.
- If the role is hybrid/onsite in a target UK/EU city and the candidate requires relocation/visa sponsorship, the agent answers truthfully based on `profile["work_authorization"]` (`willing_to_relocate: "Yes"`, `require_sponsorship: ...`, `notice_period: ...`).

### 4.2. Multi-Currency Compensation Engine (`GBP`, `EUR`, `CHF`)
Support `compensation.currencies` in `profile.json`:
```json
"compensation": {
  "salary_expectation": "65000",
  "salary_currency": "GBP",
  "salary_range_min": "55000",
  "salary_range_max": "85000",
  "currencies": {
    "GBP": {"expectation": "65000", "min": "55000", "max": "85000", "symbol": "£"},
    "EUR": {"expectation": "75000", "min": "65000", "max": "95000", "symbol": "€"},
    "CHF": {"expectation": "110000", "min": "95000", "max": "135000", "symbol": "CHF"},
    "USD": {"expectation": "85000", "min": "75000", "max": "110000", "symbol": "$"}
  }
}
```
The prompt instructs the agent to match the currency of the country/job posting (`£` GBP for UK, `€` EUR for Eurozone, `CHF` for Switzerland) and convert annual gross to monthly gross (`÷ 12`, common in DE/NL/AT) or daily rate (`÷ 220`, common in UK contracting) when a form requests monthly or daily figures.

### 4.3. Per-Worker Upload Isolation
In `apply/prompt.py:build_prompt()`, pass `worker_id: int = 0` so files are staged in:
`config.APPLY_WORKER_DIR / f"worker-{worker_id}" / "uploads" / f"{name_slug}_Resume.pdf"`
eliminating race conditions when running multiple Chrome workers.
