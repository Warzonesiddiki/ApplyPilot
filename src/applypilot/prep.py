"""AI Interview Prep Dossier & Recruiter/Hiring Manager Outreach Generator.

Generates job-specific Interview Preparation Packs (`applypilot prep`) with
STAR-method answers grounded in the candidate's real resume facts, as well as
LinkedIn connection notes (<300 chars), Recruiter InMails, and Hiring Manager
cold emails (`applypilot outreach`).

Supports both LLM-powered generation and deterministic domain-adaptive generation
so it works reliably in both Tier 1 (offline / no API key) and Tier 2/3.
"""

from __future__ import annotations

import logging
from pathlib import Path
import sqlite3
from typing import Any

from applypilot import config
from applypilot.database import ensure_columns, get_connection
from applypilot.scoring.ats import ATSCoverageResult, compute_ats_coverage
from applypilot.scoring.tailor import _build_file_prefix
from applypilot.scoring.validator import detect_profile_domain

log = logging.getLogger(__name__)


def find_target_job(
    conn: sqlite3.Connection | None = None,
    url_or_query: str | None = None,
) -> dict[str, Any] | None:
    """Resolve a target job by URL, title/company substring, or highest fit_score."""
    if conn is None:
        conn = get_connection()
    ensure_columns(conn)

    if url_or_query and url_or_query.strip():
        q = url_or_query.strip()
        row = conn.execute(
            "SELECT * FROM jobs WHERE url = ? OR application_url = ? LIMIT 1",
            (q, q),
        ).fetchone()
        if not row:
            like = f"%{q}%"
            row = conn.execute(
                "SELECT * FROM jobs WHERE url LIKE ? OR title LIKE ? OR company LIKE ? "
                "ORDER BY fit_score DESC NULLS LAST, discovered_at DESC LIMIT 1",
                (like, like, like),
            ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM jobs ORDER BY fit_score DESC NULLS LAST, discovered_at DESC LIMIT 1"
        ).fetchone()

    if not row:
        return None
    return dict(zip(row.keys(), row))


def _build_deterministic_prep_markdown(
    job: dict[str, Any],
    profile: dict[str, Any],
    ats: ATSCoverageResult,
) -> str:
    """Build a structured, fact-grounded Interview Prep Dossier."""
    domain = detect_profile_domain(profile)
    personal = profile.get("personal", {})
    exp = profile.get("experience", {})
    facts = profile.get("resume_facts", {})

    full_name = personal.get("full_name", "Candidate")
    target_role = exp.get("target_role", "Senior Professional")
    years = exp.get("years", "6+")
    companies = facts.get("preserved_companies", []) or ["leading organizations"]
    metrics = facts.get("real_metrics", []) or ["delivered measurable operational and financial impact"]
    certs = facts.get("certifications", [])
    projects = facts.get("preserved_projects", [])

    job_title = job.get("title") or target_role
    company = job.get("company") or job.get("site") or "Target Employer"
    location = job.get("location") or "UK / EU"
    score = job.get("fit_score") or "N/A"
    sponsor = job.get("sponsor_status") or "unknown"

    matched_str = ", ".join(ats["matched_keywords"]) if ats["matched_keywords"] else "Core domain competencies"
    missing_str = ", ".join(ats["missing_keywords"]) if ats["missing_keywords"] else "None identified"

    comp_1 = companies[0] if len(companies) > 0 else "most recent employer"
    comp_2 = companies[1] if len(companies) > 1 else comp_1
    metric_1 = metrics[0] if len(metrics) > 0 else "significant efficiency and accuracy gains"
    metric_2 = metrics[1] if len(metrics) > 1 else metric_1
    metric_3 = metrics[2] if len(metrics) > 2 else metric_1
    cert_str = ", ".join(certs) if certs else target_role

    if domain == "finance_accounting":
        pitch = (
            f"\"I am {full_name}, a {cert_str} finance and audit professional with {years}+ years of experience "
            f"across {', '.join(companies[:3])}. My background combines statutory IFRS/UK GAAP financial reporting, "
            f"multi-entity consolidation, FP&A, and internal controls — including {metric_1} and {metric_2}. "
            f"I'm particularly drawn to {company}'s {job_title} role because it directly aligns with my track record "
            f"of delivering audit-ready reporting and commercial decision support at scale.\""
        )
        questions = [
            (
                f"1. Technical Accounting & IFRS / GAAP Application at {company}",
                "Walk us through a complex IFRS (e.g., IFRS 15 Revenue, IFRS 16 Leases, or IFRS 9 Financial Instruments) or multi-entity consolidation issue you resolved.",
                f"**Situation:** At **{comp_1}**, we managed complex statutory financial reporting and audit deliverables across multiple operating entities.\n"
                f"**Task:** Ensure full compliance with IFRS standards and resolve technical adjustments ahead of external audit sign-off.\n"
                f"**Action:** Reviewed underlying contracts and schedules, built standardized IFRS working papers and reconciliation controls, and partnered directly with operational and audit leads.\n"
                f"**Result:** Achieved **{metric_1}** with zero material audit restatements.",
            ),
            (
                "2. Budgeting, Rolling Forecasts & Commercial FP&A",
                "How have you improved forecast accuracy or supported executive leadership with variance analysis?",
                f"**Situation:** At **{comp_1}**, leadership needed tighter visibility into divisional performance and working capital drivers.\n"
                f"**Task:** Upgrade the budgeting and rolling forecast model and surface actionable KPI variances.\n"
                f"**Action:** Built driver-based financial models and executive KPI dashboards, partnering with department heads on monthly budget vs. actual reviews.\n"
                f"**Result:** Delivered **{metric_2}** and accelerated management decision cycles.",
            ),
            (
                "3. Month-End / Year-End Close & Process Automation",
                "How do you manage tight month-end close deadlines across multiple entities or ERP systems?",
                f"**Situation:** Across **{comp_2}**, month-end and year-end close involved high transaction volumes and intercompany reconciliations.\n"
                f"**Task:** Shorten the close cycle while strengthening balance sheet substantiation.\n"
                f"**Action:** Standardized close checklists, automated recurring journal and reconciliation templates in ERP/Excel, and instituted pre-close review gates.\n"
                f"**Result:** Delivered **{metric_3}** and improved reporting timeliness.",
            ),
            (
                "4. Internal Controls, Audit Readiness & Risk Mitigation (SOX / COSO / ISA)",
                "Describe a time you identified a control weakness and implemented remediation.",
                f"**Situation:** During assurance and financial review engagements at **{comp_2}**, we evaluated key revenue, expenditure, and treasury controls.\n"
                f"**Task:** Remediate control gaps and strengthen governance under ISA/COSO frameworks.\n"
                f"**Action:** Designed automated reconciliation checks, segregation-of-duties matrices, and clear audit trails.\n"
                f"**Result:** Eliminated recurring reconciliation bottlenecks and passed statutory audit inspections cleanly.",
            ),
            (
                "5. ERP Systems, BI & Data Integrity",
                "What experience do you have implementing, migrating, or optimizing ERP and BI reporting workflows?",
                f"**Situation:** Financial data across **{comp_1}** required clean integration between general ledger systems and management reporting.\n"
                f"**Task:** Streamline chart-of-accounts mapping, tax/VAT coding, and dashboard reporting.\n"
                f"**Action:** Configured ERP workflows, validated historical trial balance migrations, and built automated reporting templates.\n"
                f"**Result:** Cut manual spreadsheet intervention and ensured single-source-of-truth reporting.",
            ),
            (
                "6. Stakeholder Management with Non-Finance Leaders",
                "Tell us about a time you influenced a commercial or operational leader using financial data.",
                f"**Situation:** At **{comp_1}**, operational teams needed clarity on cost drivers and margin performance.\n"
                f"**Task:** Translate complex financial variances into clear commercial actions.\n"
                f"**Action:** Presented concise scenario analyses showing cash flow and margin trade-offs in plain business terms.\n"
                f"**Result:** Secured stakeholder alignment and achieved **{metric_2}**.",
            ),
        ]
        questions_to_ask = [
            f"What are the top priorities for the **{job_title}** in the first 90 days (e.g., upcoming year-end audit, ERP enhancement, or group reporting transformation)?",
            f"How is the finance/audit team structured at **{company}**, and how closely does this role partner with commercial operations and external auditors?",
            "Which ERP, consolidation, and BI tools are currently in place, and are there any planned system upgrades on the roadmap?",
            "What are the key KPIs that the Finance Director / Partner uses to measure success in this role over the first 12 months?",
            "How does the team balance statutory/regulatory compliance with commercial business partnering?",
        ]
    else:
        proj_1 = projects[0] if projects else "production system architecture"
        pitch = (
            f"\"I am {full_name}, a {target_role} with {years}+ years of experience building production systems "
            f"across {', '.join(companies[:3])}. I specialize in {matched_str}, with a proven track record including "
            f"{metric_1} and {metric_2}. I'm excited about {company}'s {job_title} role because it directly matches "
            f"the scalable systems and automation problems I've solved in production.\""
        )
        questions = [
            (
                f"1. System Architecture & Production Scale at {company}",
                "Walk us through the most complex system or pipeline you architected end-to-end.",
                f"**Situation:** At **{comp_1}**, we needed to scale **{proj_1}** under strict reliability and latency requirements.\n"
                f"**Task:** Design a resilient architecture capable of handling production workloads without bottlenecks.\n"
                f"**Action:** Engineered modular services, optimized database queries and caching layers, and added automated observability.\n"
                f"**Result:** Achieved **{metric_1}** in production.",
            ),
            (
                "2. Performance Optimization & Reliability",
                "Tell us about a critical performance bottleneck or production incident you resolved.",
                f"**Situation:** At **{comp_2}**, high-volume data processing created latency spikes.\n"
                f"**Task:** Diagnose root causes and permanently reduce processing time.\n"
                f"**Action:** Profiled hot paths, refactored synchronous bottlenecks into batched/async pipelines, and tuned indexing.\n"
                f"**Result:** Delivered **{metric_2}**.",
            ),
            (
                "3. Cross-Functional Delivery & Business Impact",
                "How do you align technical architecture decisions with commercial or product outcomes?",
                f"**Situation:** Stakeholders at **{comp_1}** needed faster turnaround on critical business workflows.\n"
                f"**Task:** Deliver high-impact automation without disrupting existing operations.\n"
                f"**Action:** Partnered directly with domain stakeholders, shipped iterative releases with automated validation, and measured adoption.\n"
                f"**Result:** Delivered **{metric_3}**.",
            ),
        ]
        questions_to_ask = [
            f"What are the most important technical or product milestones for the **{job_title}** in the first 90 days at **{company}**?",
            "What does the current architecture and deployment pipeline look like, and what are the biggest scaling challenges over the next year?",
            "How do engineering, product, and domain teams collaborate on roadmap prioritization?",
            "How does the team measure engineering quality, reliability, and developer velocity?",
            "What growth paths exist for senior contributors on this team?",
        ]

    q_blocks = []
    for title_hdr, prompt_q, star_ans in questions:
        q_blocks.append(f"### {title_hdr}\n**Likely Question:** *\"{prompt_q}\"*\n\n{star_ans}\n")

    ask_blocks = "\n".join(f"{i}. {q}" for i, q in enumerate(questions_to_ask, 1))

    return f"""# Interview Preparation Dossier: {job_title} — {company}

- **Candidate:** {full_name} (`{target_role}`)
- **Company:** {company}
- **Role:** {job_title}
- **Location:** {location}
- **Fit Score:** {score}/10 | **ATS Keyword Coverage:** {ats['coverage_pct']}%
- **UK/EU Sponsor Signal:** `{sponsor}`
- **Job URL:** {job.get('url', 'N/A')}

---

## 1. 60-Second Executive Elevator Pitch

> {pitch}

---

## 2. ATS Keyword & Requirement Mapping

- **Matched Keywords to Lead With:** `{matched_str}`
- **Keywords to Bridge / Emphasize in Conversation:** `{missing_str}`
- **Core Verified Resume Facts to Anchor Every Answer:**
  - **Employers:** {', '.join(companies)}
  - **Qualifications / Certifications:** {cert_str}
  - **Key Quantified Outcomes:** {'; '.join(metrics[:5])}

---

## 3. Role-Specific Interview Questions & Tailored STAR Answers

{chr(10).join(q_blocks)}---

## 4. Addressing Gaps & UK/EU Relocation / Right-to-Work Questions

- **If asked about missing tools/keywords (`{missing_str}`):** Emphasize your rapid mastery of adjacent standards and systems across **{', '.join(companies[:2])}**, and map each requirement to an equivalent production workflow you have already delivered.
- **If asked about UK/EU Right-to-Work / Relocation / Notice Period:**
  - State your availability and notice period clearly (`{profile.get('work_authorization', {}).get('notice_period', 'Immediate / 1 Month')}`).
  - Confirm full readiness to relocate / work in **{location}** and highlight international multi-jurisdiction experience across UK, EU, and global standards.

---

## 5. High-Impact Questions to Ask the Interviewer

{ask_blocks}
"""


def generate_interview_prep(
    job: dict[str, Any],
    profile: dict[str, Any] | None = None,
    resume_text: str | None = None,
    use_llm: bool = True,
) -> tuple[str, Path]:
    """Generate and save a tailored Interview Prep Dossier for a job.

    Returns:
        (markdown_content, saved_file_path)
    """
    config.ensure_dirs()
    if profile is None:
        try:
            profile = config.load_profile()
        except Exception:
            profile = {}
    if resume_text is None and config.RESUME_PATH.exists():
        resume_text = config.RESUME_PATH.read_text(encoding="utf-8")

    tailored_txt = resume_text or ""
    if job.get("tailored_resume_path") and Path(job["tailored_resume_path"]).exists():
        tailored_txt = Path(job["tailored_resume_path"]).read_text(encoding="utf-8")

    ats = compute_ats_coverage(
        job.get("full_description") or job.get("description"),
        tailored_txt,
        base_resume=resume_text,
        profile=profile,
    )

    markdown = _build_deterministic_prep_markdown(job, profile, ats)

    if use_llm and config.has_llm_configured():
        try:
            from applypilot.llm import get_client

            client = get_client()
            prompt = (
                "You are an executive interview coach. Enhance the following structured Interview Prep Dossier "
                "for the candidate using ONLY the real employers, qualifications, and metrics already present in the dossier. "
                "Keep all 5 sections in clean Markdown.\n\n"
                f"JOB DESCRIPTION:\n{(job.get('full_description') or job.get('description') or '')[:3500]}\n\n"
                f"DRAFT DOSSIER:\n{markdown}"
            )
            llm_out = client.ask(prompt, temperature=0.2, max_tokens=2500)
            if llm_out and len(llm_out.strip()) > 400:
                markdown = llm_out.strip() + "\n"
        except Exception as exc:
            log.debug("Using deterministic interview prep fallback: %s", exc)

    prefix = _build_file_prefix(job)
    out_path = config.PREP_DIR / f"{prefix}_PREP.md"
    out_path.write_text(markdown, encoding="utf-8")

    if job.get("url"):
        conn = get_connection()
        ensure_columns(conn)
        conn.execute("UPDATE jobs SET prep_pack_path = ? WHERE url = ?", (str(out_path), job["url"]))
        conn.commit()

    return markdown, out_path


def generate_outreach_pack(
    job: dict[str, Any],
    profile: dict[str, Any] | None = None,
    resume_text: str | None = None,
    use_llm: bool = True,
) -> tuple[str, Path]:
    """Generate LinkedIn Connection Note (<300 chars), Recruiter InMail, and Hiring Manager Cold Email.

    Returns:
        (markdown_content, saved_file_path)
    """
    config.ensure_dirs()
    if profile is None:
        try:
            profile = config.load_profile()
        except Exception:
            profile = {}
    if resume_text is None and config.RESUME_PATH.exists():
        resume_text = config.RESUME_PATH.read_text(encoding="utf-8")

    domain = detect_profile_domain(profile)
    personal = profile.get("personal", {})
    exp = profile.get("experience", {})
    facts = profile.get("resume_facts", {})

    full_name = personal.get("full_name", "Candidate")
    email = personal.get("email", "")
    linkedin = personal.get("linkedin", "")
    years = exp.get("years", "6+")
    target_role = exp.get("target_role", "Senior Professional")
    companies = facts.get("preserved_companies", []) or ["leading firms"]
    metrics = facts.get("real_metrics", []) or ["strong measurable outcomes"]
    certs = facts.get("certifications", [])

    job_title = (job.get("title") or target_role).strip()
    company = (job.get("company") or job.get("site") or "your team").strip()
    location = (job.get("location") or "UK/EU").strip()

    short_qual = "ACA Chartered Accountant" if any("aca" in str(c).lower() for c in certs) else (
        "ACCA Finalist & Senior Accountant" if any("acca" in str(c).lower() for c in certs) else target_role
    )
    top_companies = ", ".join(companies[:2])
    top_metric = metrics[0] if metrics else f"{years}+ years of domain delivery"

    # Build a strict <300 char LinkedIn connection note
    conn_note = (
        f"Hi — I just applied for the {job_title} role at {company}. "
        f"As a {short_qual} ({top_companies}) with {years}+ yrs experience ({top_metric[:55]}), "
        f"I'd love to connect and share my CV. Best, {full_name}"
    )
    if len(conn_note) > 295:
        conn_note = (
            f"Hi — I applied for the {job_title[:35]} role at {company[:25]}. "
            f"As a {short_qual[:35]} ({years}+ yrs across {top_companies[:35]}), "
            f"I'd love to connect. Best, {full_name}"
        )[:295]

    if domain == "finance_accounting":
        hm_title = "Finance Director / Audit Partner / Hiring Manager"
        focus_line = "IFRS/UK GAAP financial reporting, statutory audit, FP&A, and multi-entity controls"
    else:
        hm_title = "Engineering Manager / VP Engineering / Hiring Manager"
        focus_line = "production system architecture, automation, and scalable data/AI workflows"

    inmail_msg = (
        f"Subject: Application for {job_title} — {full_name} ({short_qual})\n\n"
        f"Hi [Recruiter Name],\n\n"
        f"I recently submitted my application for the {job_title} position at {company} ({location}). "
        f"With {years}+ years of experience across { ', '.join(companies[:3]) } specializing in {focus_line}, "
        f"I have delivered outcomes including {top_metric}.\n\n"
        f"Given {company}'s focus in this space, I would welcome a brief 10-minute conversation this week "
        f"to walk through how my background maps to your requirements.\n\n"
        f"Best regards,\n{full_name}\n{email} | {linkedin}"
    )

    cold_email = (
        f"Subject: {job_title} at {company} — {full_name} ({short_qual})\n\n"
        f"Dear {hm_title},\n\n"
        f"I am writing regarding the {job_title} opening at {company}. Across {years}+ years with "
        f"{', '.join(companies[:3])}, I have led {focus_line} — including {top_metric}"
        + (f" and {metrics[1]}" if len(metrics) > 1 else "")
        + ".\n\n"
        f"I have attached my tailored CV for your review and would welcome the opportunity to discuss "
        f"how I can contribute immediately to {company}'s team in {location}.\n\n"
        f"Kind regards,\n{full_name}\n{email}\n{linkedin}"
    )

    markdown = f"""# Outreach Pack: {job_title} — {company}

- **Candidate:** {full_name} (`{short_qual}`)
- **Company:** {company}
- **Role:** {job_title} (`{location}`)
- **Job URL:** {job.get('url', 'N/A')}

---

## 1. LinkedIn Connection Request Note ({len(conn_note)} / 300 characters)

```text
{conn_note}
```

---

## 2. LinkedIn InMail / Recruiter Message

```text
{inmail_msg}
```

---

## 3. Direct Cold Email to {hm_title}

```text
{cold_email}
```
"""

    prefix = _build_file_prefix(job)
    out_path = config.OUTREACH_DIR / f"{prefix}_OUTREACH.md"
    out_path.write_text(markdown, encoding="utf-8")

    if job.get("url"):
        conn = get_connection()
        ensure_columns(conn)
        conn.execute("UPDATE jobs SET outreach_path = ? WHERE url = ?", (str(out_path), job["url"]))
        conn.commit()

    return markdown, out_path
