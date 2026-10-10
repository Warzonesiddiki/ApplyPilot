# Next-Generation Capabilities & Core Engine Specification (`v0.5.0`)

**Document ID:** SPEC-05  
**Scope:** Pipeline/CLI Unification, Multi-Provider LLM Fallback & Telemetry, Explainable Multi-Dimensional Scoring, Deterministic ATS Keyword Coverage, Native `.docx` & Multi-Theme PDF Export, and Resume-to-Profile Bootstrapper.

---

## 1. CLI & Pipeline Engine Unification (`cli.py` & `pipeline.py`)

### 1.1. Resolution of `v0.3.0` CLI ↔ Pipeline Contract Mismatch
- Export canonical stage constants in `src/applypilot/pipeline.py`:
  ```python
  ALL_STAGES: tuple[str, ...] = ("discover", "enrich", "score", "tailor", "cover", "pdf")
  AI_STAGES: tuple[str, ...] = ("score", "tailor", "cover")
  ```
- Unify `run_pipeline()` signature so every CLI option in `applypilot run` works seamlessly:
  - `stages: list[str] | None = None` (supports both positional arguments `applypilot run discover score` and `--stage / -s` options)
  - `sources: list[str] | None = None` (`jobspy`, `workday`, `ats`, `smartextract`)
  - `min_score: int = 7`
  - `limit: int = 0` (per-stage batch cap; `0` = unlimited)
  - `workers: int = 1` (`--workers / -w`)
  - `dry_run: bool = False` (`--dry-run`)
  - `stream: bool = False` (`--stream`)
  - `batch_size: int = 5` (`--batch-size`)
  - `validation_mode: str = "normal"` (`--validation` / `validation_mode`)
  - `skip_enrich: bool = False`, `skip_tailor: bool = False`, `skip_cover: bool = False`, `skip_pdf: bool = False`
  - `deduplicate: bool = True` (automatic cross-board deduplication after discovery)
  - `export_docx: bool = True` (generate editable `.docx` files alongside `.pdf`)
- Add `--all-profiles` flag to `applypilot run` and `applypilot status` to iterate through all initialized profiles (e.g., `tahir` and `neha`) sequentially and render a consolidated multi-profile summary table.

---

## 2. Multi-Provider LLM Engine with Automatic Fallback & Telemetry (`src/applypilot/llm.py`)

### 2.1. Supported Providers (Priority & Fallback Chain)
Extend `_detect_provider()` and `LLMClient` to support 6 provider families out of the box:
1. **Google Gemini** (`GEMINI_API_KEY`) — Default model: `gemini-2.0-flash` (with automatic fallback from OpenAI-compat endpoint to native `generateContent` API on HTTP 403).
2. **Anthropic Claude** (`ANTHROPIC_API_KEY`) — Default model: `claude-3-5-haiku-latest` or `claude-3-7-sonnet-latest` via native `/v1/messages` REST endpoint.
3. **OpenAI** (`OPENAI_API_KEY`) — Default model: `gpt-4o-mini`.
4. **OpenRouter** (`OPENROUTER_API_KEY`) — Endpoint: `https://openrouter.ai/api/v1`, default model: `google/gemini-2.0-flash-001`.
5. **Groq** (`GROQ_API_KEY`) — Endpoint: `https://api.groq.com/openai/v1`, default model: `llama-3.3-70b-versatile`.
6. **DeepSeek** (`DEEPSEEK_API_KEY`) — Endpoint: `https://api.deepseek.com/v1`, default model: `deepseek-chat`.
7. **Local OpenAI-Compatible Server** (`LLM_URL`, e.g., Ollama / llama.cpp / vLLM / LM Studio).

### 2.2. Automatic Provider Fallback Chain
- If multiple API keys are configured in `.env` (for example, `GEMINI_API_KEY` + `GROQ_API_KEY` or `OPENROUTER_API_KEY`), `LLMClient` builds an ordered list of provider endpoints.
- When the primary provider returns `429 Too Many Requests`, `503 Service Unavailable`, or `502 Bad Gateway` and a fallback provider is configured, `LLMClient` automatically fails over to the next provider before sleeping, ensuring zero pipeline stalls during large runs.

### 2.3. Token & Cost Telemetry
- `LLMClient` records cumulative telemetry (`calls`, `prompt_tokens_est`, `completion_tokens_est`, `estimated_cost_usd`, `failovers`, `total_latency_ms`) accessible via `get_telemetry()`.
- `pipeline.py` prints LLM telemetry at the end of every run.

---

## 3. Explainable Multi-Dimensional Job Scoring (`src/applypilot/scoring/scorer.py`)

### 3.1. 4-Dimension Sub-Score Breakdown
Upgrade `SCORE_PROMPT` and `_parse_score_response()` to evaluate jobs across four explicit dimensions in addition to the overall `fit_score` (1–10):
1. **`skills_score` (1–10)**: Technical stack, ERP/financial systems, or accounting/audit standards match (`IFRS`, `ISA`, `UK GAAP`, `Python`, `SQL`, `SAP`, `Zoho Books`, etc.).
2. **`qualifications_score` (1–10)**: Professional qualification & seniority match (`ACA`, `ICAEW`, `ACCA Finalist`, years of post-qualification or domain experience).
3. **`domain_score` (1–10)**: Industry & functional alignment (Big 4 Audit, FP&A, Multi-Entity Financial Reporting, AI/FinTech Engineering).
4. **`location_score` (1–10)**: Geographic, remote/hybrid, and UK/EU visa sponsorship or right-to-work feasibility.

### 3.2. Matched vs. Missing ATS Keywords
- Extract both:
  - `MATCHED_KEYWORDS`: Keywords from the JD already present in the candidate's profile/CV.
  - `MISSING_KEYWORDS`: High-value ATS keywords from the JD that the tailoring stage should weave in (where truthful and within the candidate's `skills_boundary`).
- Persist `score_breakdown` (JSON string containing sub-scores, matched keywords, and missing keywords) in the `jobs` table.

---

## 4. Deterministic ATS Keyword Coverage Engine (`src/applypilot/scoring/ats.py`)

### 4.1. Purpose
Provide a fast, deterministic, zero-LLM-token analyzer that measures how well a resume covers the exact technical, financial, regulatory, and domain keywords in a job description.

### 4.2. Capabilities
- `extract_jd_keywords(job_description: str, domain: str | None = None) -> list[str]`:
  - Extracts domain-recognized skills, standards, tools, qualifications, and high-signal multi-word terms (e.g., `IFRS 15`, `IFRS 16`, `IFRS 9`, `UK GAAP`, `FRS 102`, `ISA`, `ACA`, `ACCA`, `SOX`, `COSO`, `FP&A`, `DCF`, `VAT`, `Corporation Tax`, `SAP`, `Oracle`, `Zoho Books`, `QuickBooks`, `Xero`, `Power BI`, `Python`, `FastAPI`, `Kubernetes`, `PostgreSQL`, `LangGraph`, etc.) plus dynamic capitalized terms from the JD.
- `compute_ats_coverage(job_description: str, resume_text: str, baseline_resume: str | None = None) -> dict`:
  - Returns:
    - `coverage_pct`: `int` (0–100) for the tailored resume
    - `baseline_pct`: `int` (0–100) for the original base resume
    - `delta_pct`: `int` improvement (`coverage_pct - baseline_pct`)
    - `matched_keywords`: `list[str]`
    - `missing_keywords`: `list[str]`
    - `total_jd_keywords`: `int`
- Stored in `jobs.ats_coverage_pct` (`INTEGER`) and included in `*_REPORT.json` and the HTML/CRM Dashboard.

---

## 5. Multi-Theme PDF & Native Word (`.docx`) Export (`scoring/pdf.py` & `scoring/docx_export.py`)

### 5.1. Selectable Resume PDF Themes (`scoring/pdf.py`)
Add `theme` support (`"modern"`, `"executive_uk_eu"`, `"compact_ats"`) to `build_html()` and `build_cover_letter_html()`:
- **`modern`** (default): Crisp Calibri/Inter 1-to-2 page ATS layout.
- **`executive_uk_eu`**: Tailored for UK/EU Chartered Accountancy (`ACA` / `ACCA`) and Senior Finance roles — refined Georgia/Garamond serif or executive sans-serif typography, prominent `QUALIFICATIONS & MEMBERSHIPS` banner, and clean A4 pagination rules (`page-break-inside: avoid` on role blocks).
- **`compact_ats`**: Ultra-dense single-page layout for strict 1-page applications.

### 5.2. Pure-Python Native Word (`.docx`) Generator (`src/applypilot/scoring/docx_export.py`)
UK and EU recruitment agencies (Reed, Hays, Robert Walters, Michael Page, eFinancialCareers) frequently require editable `.docx` CVs.
- Implement a zero-external-dependency OpenXML (`.docx`) builder using Python's standard `zipfile` and `xml.etree.ElementTree` / escaped XML templates:
  - `convert_resume_to_docx(txt_path: Path, docx_path: Path | None = None) -> Path`
  - `convert_cover_letter_to_docx(txt_path: Path, docx_path: Path | None = None, profile: dict | None = None) -> Path`
  - `batch_convert_docx() -> dict`
- Formats Name, Title/Qualification, Contact Line, Section Headers with bottom borders, Bold Job Headers, Italic Subtitles, Bullet Lists, and Category-bolded Skills lines in valid WordprocessingML (`word/document.xml`, `word/styles.xml`, `[Content_Types].xml`, `_rels/.rels`).

---

## 6. One-Command AI Profile & Search Importer (`src/applypilot/wizard/importer.py`)

- Support `applypilot init --from-resume <path/to/resume.txt_or_pdf>` (or `applypilot import-resume <path>`):
  - Reads plain text (or extracts text from PDF/DOCX).
  - Uses the configured LLM (or a deterministic parser fallback when offline/no LLM key) to extract:
    - `personal` (name, email, phone, city, country, LinkedIn)
    - `domain` (`finance_accounting`, `fpa_tech_hybrid`, `software_engineering`)
    - `experience` (`years`, `target_role`, `education_level`)
    - `skills_boundary` (grouped by category)
    - `resume_facts` (`preserved_companies`, `preserved_projects`, `preserved_school`, `certifications`, `real_metrics`)
  - Automatically writes `profile.json`, `resume.txt`, and a tailored `searches.yaml` for the active profile.
