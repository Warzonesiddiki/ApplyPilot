"""Resume tailoring: LLM-powered ATS-optimized resume generation per job.

Every piece of personal data -- name, email, phone, skills, companies, projects,
qualifications/certifications, school -- is loaded at runtime from the user's profile.
Supports both Software/AI/FP&A Engineering and Chartered Accountancy (ACA) / Finance domains.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

from applypilot import config
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client
from applypilot.schemas import ResumeJudgeResponse, TailoredResumeResponse
from applypilot.scoring.validator import (
    BANNED_WORDS,
    detect_profile_domain,
    sanitize_text,
    validate_json_fields,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # max cross-run retries before giving up

# Statuses whose resume may be persisted as `tailored_resume_path` and therefore
# submitted by the apply pipeline. `approved_with_judge_warning` is deliberately
# absent: it only occurs when a caller opts into `accept_judge_warning`.
SUCCESS_STATUSES: frozenset[str] = frozenset({"approved"})

# Every status a tailoring run can produce is normalised into exactly one bucket so
# the reported totals always account for every artefact written to TAILORED_DIR.
_STATUS_BUCKETS: tuple[str, ...] = ("approved", "failed_validation", "failed_judge", "failed_other", "error")


def _status_bucket(status: str | None) -> str:
    """Map a tailoring report status onto its accounting bucket."""
    name = (status or "error").strip().lower()
    if name in ("approved", "approved_with_judge_warning"):
        return "approved"
    if name in ("failed_validation", "failed_judge"):
        return name
    if name == "error":
        return "error"
    return "failed_other"


# ── Prompt Builders (profile-driven & domain-adaptive) ────────────────────

def _build_tailor_prompt(profile: dict[str, Any]) -> str:
    """Build the resume tailoring system prompt from the user's profile."""
    domain = detect_profile_domain(profile)
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Format skills boundary for the prompt
    skills_lines = []
    skill_keys_sample: dict[str, str] = {}
    for category, items in boundary.items():
        if isinstance(items, list) and items:
            label = category.replace("_", " ").title() if "_" in category else category
            skills_lines.append(f"{label}: {', '.join(items)}")
            skill_keys_sample[label] = "..."
    skills_block = "\n".join(skills_lines)
    if not skill_keys_sample:
        skill_keys_sample = {"Core Skills": "...", "Tools & Systems": "..."}

    # Preserved entities
    companies = resume_facts.get("preserved_companies", [])
    projects = resume_facts.get("preserved_projects", [])
    school = resume_facts.get("preserved_school", "")
    certifications = resume_facts.get("certifications", [])
    real_metrics = resume_facts.get("real_metrics", [])

    companies_str = ", ".join(companies) if companies else "N/A"
    projects_str = ", ".join(projects) if projects else "N/A"
    certs_str = ", ".join(certifications) if certifications else "N/A"
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"

    banned_str = ", ".join(BANNED_WORDS)

    education = profile.get("experience", {})
    education_level = education.get("education_level", "")
    edu_default = f"{school} | {education_level}".strip(" |")

    skills_json_str = json.dumps(skill_keys_sample)

    if domain == "finance_accounting":
        persona = (
            "You are a senior finance, audit, and executive accounting recruiter "
            "(Big 4, FTSE, and European multinational experience) rewriting a CV to get this candidate an interview."
        )
        stretch_rule = (
            "You MAY add 2-3 closely related accounting/audit/finance standards or ERP modules "
            "(e.g., FRS 101/102 if IFRS/UK GAAP, SOX/ICFR if ISA statutory audit, Power Query if Advanced Excel). "
            "Do NOT invent unrelated programming languages or unheld qualifications. "
            "CRITICAL: If the candidate is an ACCA Finalist / part-qualified (e.g. 11 of 13 exams passed), "
            "always state 'ACCA Finalist (11 of 13 exams passed)' and NEVER claim full ACCA membership."
        )
        voice_examples = (
            '- Write like a Chartered Accountant (ACA), ACCA Finalist, or senior finance professional. Direct, commercial, standards-precise.\n'
            '- GOOD: "Led IFRS 15 and IFRS 16 statutory audit testing across an AED 500M multi-entity group, resolving complex consolidation adjustments ahead of board sign-off"\n'
            '- BAD: "Leveraged cutting-edge accounting methodologies to drive transformative financial synergies"'
        )
    else:
        persona = "You are a senior technical recruiter rewriting a resume to get this person an interview."
        stretch_rule = (
            "You MAY add 2-3 closely related tools (Kubernetes if Docker, Terraform if AWS, Redis if PostgreSQL). "
            "No unrelated languages/frameworks."
        )
        voice_examples = (
            '- Write like a real engineer. Short, direct.\n'
            '- GOOD: "Automated financial reporting with Python + API integrations, cut processing time from 10 hours to 2"\n'
            '- BAD: "Leveraged cutting-edge AI technologies to drive transformative operational efficiencies"'
        )

    return f"""{persona}

Take the base resume and job description. Return a tailored resume as a JSON object.

## RECRUITER SCAN (6 seconds):
1. Title -- matches what they're hiring?
2. Summary -- 2 sentences proving you've done this work
3. First 3 bullets of most recent role -- verbs and outcomes match?
4. Skills & Qualifications -- must-haves visible immediately?

## SKILLS BOUNDARY (real skills only):
{skills_block}

{stretch_rule}

## TAILORING RULES:

TITLE: Match the target role. Keep seniority and professional designation (e.g., ACA, Senior, Lead, Manager). Drop company suffixes and team names.

SUMMARY: Rewrite from scratch. Lead with the 1-2 qualifications/skills that matter most for THIS role. Sound like someone who has already done this job.

SKILLS: Reorder each category so the job's must-haves appear first.

Reframe EVERY bullet for this role. Same real work, different angle. Every bullet must be reworded. Never copy verbatim.

PROJECTS: Reorder by relevance ({projects_str}). If the candidate is a finance/accounting professional with no software projects, return an empty list [] for "projects".

BULLETS: Strong action verb + what you delivered + quantified impact. Vary verbs (Built, Designed, Led, Audited, Consolidated, Automated, Delivered, Optimized, Reconciled). Most relevant first. Max 4 per role.

## VOICE:
{voice_examples}
- BANNED WORDS (using ANY of these = validation failure — do not use them even once):
  {banned_str}
- No em dashes. Use commas, periods, or hyphens.

## HARD RULES:
- Do NOT invent work, companies, degrees, or certifications
- Do NOT change real numbers ({metrics_str})
- Preserved companies: {companies_str} -- names stay as-is
- Preserved qualifications/certifications: {certs_str}
- Preserved school: {school}
- Must fit 1-2 pages cleanly.

## OUTPUT: Return ONLY valid JSON. No markdown fences. No commentary. No "here is" preamble.

{{"title":"Role Title","summary":"2-3 tailored sentences.","skills":{skills_json_str},"experience":[{{"header":"Title at Company","subtitle":"Location / Scope | Dates","bullets":["bullet 1","bullet 2","bullet 3","bullet 4"]}}],"projects":[{{"header":"Project Name - Description","subtitle":"Tech | Dates","bullets":["bullet 1","bullet 2"]}}],"certifications":"{certs_str}","education":"{edu_default}"}}"""


def _build_judge_prompt(profile: dict[str, Any]) -> str:
    """Build the LLM judge prompt from the user's profile."""
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    all_skills: list[str] = []
    for items in boundary.values():
        if isinstance(items, list):
            all_skills.extend(items)
    for cert in resume_facts.get("certifications", []):
        all_skills.append(str(cert))
    skills_str = ", ".join(all_skills) if all_skills else "N/A"

    real_metrics = resume_facts.get("real_metrics", [])
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"

    return f"""You are a resume quality judge. A tailoring engine rewrote a resume to target a specific job. Your job is to catch LIES, not style changes.

Return one JSON object with exactly these keys: "verdict" ("PASS" or "FAIL") and "issues" (string; use "none" when clear). Do not include Markdown or commentary.

## CONTEXT -- what the tailoring engine was instructed to do (all of this is ALLOWED):
- Change the title to match the target role
- Rewrite the summary from scratch for the target job
- Reorder bullets and projects to put the most relevant first
- Reframe bullets to use the job's language
- Drop low-relevance bullets and replace with more relevant ones from other sections
- Reorder the skills section to put job-relevant skills first
- Change tone and wording extensively

## WHAT IS FABRICATION (FAIL for these):
1. Adding tools, languages, frameworks, or qualifications that aren't in the original or allowed boundary. The allowed skills/qualifications are: {skills_str}
2. Inventing NEW metrics or numbers not in the original. The real metrics are: {metrics_str}
3. Inventing work that has no basis in any original bullet (completely new achievements).
4. Adding companies, roles, or degrees that don't exist.
5. Changing real numbers (inflating 80% to 95%, 500 nodes to 1000 nodes).

## WHAT IS NOT FABRICATION (do NOT fail for these):
- Rewording any bullet, even heavily, as long as the underlying work is real
- Combining two original bullets into one
- Splitting one original bullet into two
- Describing the same work with different emphasis
- Dropping bullets entirely
- Reordering anything
- Changing the title or summary completely

## TOLERANCE RULE:
The goal is to get interviews, not to be a perfect fact-checker. Allow up to 3 minor stretches per resume:
- Adding a closely related tool/standard the candidate could realistically know is a MINOR STRETCH, not fabrication.
- Reframing a metric with slightly different wording is a MINOR STRETCH.
- Adding any LEARNABLE skill given their existing stack/qualification is a MINOR STRETCH.
- Only FAIL if there are MAJOR lies: completely invented projects, fake companies, fake degrees, wildly inflated numbers, or skills from a completely different domain.

NUMBERS ARE NEVER A MINOR STRETCH: any figure, percentage or currency amount that is not present in the original resume or the real metrics list above is a MAJOR lie. FAIL it.

Be strict about major lies. Be lenient about minor stretches and learnable skills. Do not fail for style, tone, or restructuring."""


# ── Resume Assembly (profile-driven header) ──────────────────────────────

def assemble_resume_text(data: dict[str, Any], profile: dict[str, Any]) -> str:
    """Convert JSON resume data to formatted plain text.

    Header (name, location, contact) is ALWAYS code-injected from the profile,
    never LLM-generated. All text fields are sanitized.
    """
    personal = profile.get("personal", {})
    resume_facts = profile.get("resume_facts", {})
    domain = detect_profile_domain(profile)
    lines: list[str] = []

    # Header -- always code-injected from profile
    full_name = personal.get("full_name", "")
    # Append ACA if candidate has ACA certification and it's not already in full_name
    certs_list = resume_facts.get("certifications", [])
    if domain == "finance_accounting" and any("aca" in str(c).lower() for c in certs_list) and "aca" not in full_name.lower():
        full_name = f"{full_name}, ACA"

    lines.append(full_name)
    lines.append(sanitize_text(str(data.get("title", "Professional"))))

    # Location line if city/country available
    loc_parts = [p for p in (personal.get("city", ""), personal.get("country", "")) if p]
    if loc_parts:
        lines.append(", ".join(loc_parts))

    # Contact line
    contact_parts: list[str] = []
    if personal.get("email"):
        contact_parts.append(personal["email"])
    if personal.get("phone"):
        contact_parts.append(personal["phone"])
    if personal.get("github_url"):
        contact_parts.append(personal["github_url"])
    if personal.get("linkedin_url"):
        contact_parts.append(personal["linkedin_url"])
    if contact_parts:
        lines.append(" | ".join(contact_parts))
    lines.append("")

    # Summary
    lines.append("SUMMARY")
    lines.append(sanitize_text(str(data.get("summary", ""))))
    lines.append("")

    # Technical Skills / Core Competencies
    lines.append("TECHNICAL SKILLS")
    if isinstance(data.get("skills"), dict):
        for cat, val in data["skills"].items():
            if val and str(val).strip() not in ("", "N/A", "null", "None"):
                lines.append(f"{cat}: {sanitize_text(str(val))}")
    lines.append("")

    # Experience
    lines.append("EXPERIENCE")
    for entry in data.get("experience", []):
        lines.append(sanitize_text(str(entry.get("header", ""))))
        if entry.get("subtitle"):
            lines.append(sanitize_text(str(entry["subtitle"])))
        for b in entry.get("bullets", []):
            lines.append(f"- {sanitize_text(str(b))}")
        lines.append("")

    # Projects (only emit if non-empty)
    projects_data = [
        p for p in (data.get("projects") or [])
        if isinstance(p, dict) and p.get("header") and str(p.get("header")).strip() not in ("", "N/A")
    ]
    if projects_data:
        lines.append("PROJECTS")
        for entry in projects_data:
            lines.append(sanitize_text(str(entry.get("header", ""))))
            if entry.get("subtitle"):
                lines.append(sanitize_text(str(entry["subtitle"])))
            for b in entry.get("bullets", []):
                lines.append(f"- {sanitize_text(str(b))}")
            lines.append("")

    # Qualifications / Certifications (if present in data or profile)
    cert_text = sanitize_text(str(data.get("certifications", "")))
    if not cert_text or cert_text in ("N/A", "None", "null"):
        cert_text = ", ".join(str(c) for c in certs_list if c)
    if cert_text and cert_text not in ("N/A", "None", "null"):
        lines.append("CERTIFICATIONS")
        lines.append(cert_text)
        lines.append("")

    # Education
    lines.append("EDUCATION")
    lines.append(sanitize_text(str(data.get("education", ""))))

    return "\n".join(lines)


# ── LLM Judge ────────────────────────────────────────────────────────────

def judge_tailored_resume(
    original_text: str, tailored_text: str, job_title: str, profile: dict[str, Any]
) -> dict[str, Any]:
    """LLM judge layer: catches subtle fabrication that programmatic checks miss."""
    judge_prompt = _build_judge_prompt(profile)

    messages = [
        {"role": "system", "content": judge_prompt},
        {"role": "user", "content": (
            f"JOB TITLE: {job_title}\n\n"
            f"ORIGINAL RESUME:\n{original_text}\n\n---\n\n"
            f"TAILORED RESUME:\n{tailored_text}\n\n"
            "Judge this tailored resume:"
        )},
    ]

    client = get_client()
    response = client.structured_chat(
        messages,
        response_model=ResumeJudgeResponse,
        max_tokens=512,
        temperature=0.1,
    )
    return {
        "passed": response.verdict == "PASS",
        "verdict": response.verdict,
        "issues": response.issues,
    }


# ── Core Tailoring ───────────────────────────────────────────────────────

def tailor_resume(
    resume_text: str, job: dict[str, Any], profile: dict[str, Any],
    max_retries: int = 3, validation_mode: str = "normal",
    accept_judge_warning: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Generate a tailored resume via JSON output + fresh context on each retry.

    A `FAIL` verdict from the anti-fabrication judge is a hard failure: the report
    status becomes ``failed_judge`` and the caller must not persist an output path
    for it, because `launcher.acquire_job` would then upload and submit a resume
    the judge rejected. `accept_judge_warning` is an explicit, opt-in escape hatch
    that restores the old `approved_with_judge_warning` behaviour.
    """
    from applypilot.scoring.ats import compute_ats_coverage

    company_name = job.get("company") or job.get("site") or "Unknown"
    full_desc = job.get("full_description") or ""
    job_text = (
        f"TITLE: {job.get('title', 'Untitled')}\n"
        f"COMPANY: {company_name}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{full_desc[:6000]}"
    )

    base_ats = compute_ats_coverage(full_desc, resume_text, profile=profile)
    missing_ats_terms = base_ats.get("missing_keywords", [])[:10]

    report: dict[str, Any] = {
        "attempts": 0, "validator": None, "judge": None,
        "status": "pending", "validation_mode": validation_mode,
        "base_ats_coverage": base_ats.get("coverage_pct", 100),
    }
    avoid_notes: list[str] = []
    tailored = ""
    client = get_client()
    tailor_prompt_base = _build_tailor_prompt(profile)
    if missing_ats_terms:
        tailor_prompt_base += (
            "\n\n## ATS KEYWORD ALIGNMENT:\n"
            f"Target JD keywords not yet verbatim in base resume: {', '.join(missing_ats_terms)}.\n"
            "Where factually supported by the candidate's real experience and allowed skills boundary, "
            "incorporate these exact JD terms naturally into the summary, skills, or bullets."
        )

    for attempt in range(max_retries + 1):
        report["attempts"] = attempt + 1

        prompt = tailor_prompt_base
        if avoid_notes:
            prompt += "\n\n## AVOID THESE ISSUES (from previous attempt):\n" + "\n".join(
                f"- {n}" for n in avoid_notes[-5:]
            )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"ORIGINAL RESUME:\n{resume_text}\n\n---\n\nTARGET JOB:\n{job_text}\n\nReturn the JSON:"},
        ]

        structured = client.structured_chat(
            messages,
            response_model=TailoredResumeResponse,
            max_tokens=2048,
            temperature=0.4,
        )
        data = structured.model_dump()

        # Layer 1: Validate JSON fields
        validation = validate_json_fields(
            data, profile, mode=validation_mode, original_text=resume_text
        )
        report["validator"] = validation

        if not validation["passed"]:
            avoid_notes.extend(validation["errors"])
            if attempt < max_retries:
                continue
            tailored = assemble_resume_text(data, profile)
            report["status"] = "failed_validation"
            return tailored, report

        # Assemble text
        tailored = assemble_resume_text(data, profile)

        # Layer 2: LLM judge — skipped in lenient mode
        if validation_mode == "lenient":
            report["judge"] = {"verdict": "SKIPPED", "passed": True, "issues": "none"}
            report["status"] = "approved"
            return tailored, report

        judge = judge_tailored_resume(resume_text, tailored, job.get("title", ""), profile)
        report["judge"] = judge

        if not judge["passed"]:
            avoid_notes.append(f"Judge rejected: {judge['issues']}")
            if attempt < max_retries and validation_mode != "lenient":
                continue
            # The judge exists to stop fabricated resumes being submitted. Downgrading
            # FAIL to "approved" defeats it, so rejection is the default outcome.
            report["status"] = (
                "approved_with_judge_warning" if accept_judge_warning else "failed_judge"
            )
            return tailored, report

        report["status"] = "approved"
        return tailored, report

    report["status"] = "exhausted_retries"
    return tailored, report


# ── Batch Entry Point ────────────────────────────────────────────────────

def _build_file_prefix(job: dict[str, Any]) -> str:
    """Build a collision-free filename prefix using company/site, title, and short URL hash."""
    company = job.get("company") or job.get("site") or "Job"
    title = job.get("title") or "Role"
    url = job.get("url") or f"{company}_{title}"
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]

    safe_title = re.sub(r"[^\w\s-]", "", title)[:45].strip().replace(" ", "_")
    safe_company = re.sub(r"[^\w\s-]", "", company)[:20].strip().replace(" ", "_")
    return f"{safe_company}_{safe_title}_{url_hash}"


def run_tailoring(
    min_score: int = 7,
    limit: int = 20,
    validation_mode: str = "normal",
) -> dict[str, Any]:
    """Generate tailored resumes for high-scoring jobs."""
    profile = config.load_profile()
    resume_text = config.RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    jobs = get_jobs_by_stage(conn=conn, stage="pending_tailor", min_score=min_score, limit=limit)

    if not jobs:
        log.info("No untailored jobs with score >= %d.", min_score)
        return {"approved": 0, "failed": 0, "errors": 0, "elapsed": 0.0}

    config.TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Tailoring resumes for %d jobs (score >= %d)...", len(jobs), min_score)
    t0 = time.time()
    results: list[dict[str, Any]] = []
    stats: dict[str, int] = dict.fromkeys(_STATUS_BUCKETS, 0)

    for completed, job in enumerate(jobs, 1):
        try:
            from applypilot.scoring.ats import compute_ats_coverage, select_best_base_resume
            best_res_txt, variant_name = select_best_base_resume(
                job.get("full_description"), default_resume_path=config.RESUME_PATH, profile=profile
            )
            job_base_resume = best_res_txt or resume_text
            tailored, report = tailor_resume(
                job_base_resume, job, profile, validation_mode=validation_mode
            )
            report["resume_variant"] = variant_name

            prefix = _build_file_prefix(job)

            # Save tailored resume text
            txt_path = config.TAILORED_DIR / f"{prefix}.txt"
            txt_path.write_text(tailored, encoding="utf-8")

            # Save job description for traceability
            job_path = config.TAILORED_DIR / f"{prefix}_JOB.txt"
            company_name = job.get("company") or job.get("site") or "Unknown"
            job_desc = (
                f"Title: {job.get('title', '')}\n"
                f"Company: {company_name}\n"
                f"Site: {job.get('site', '')}\n"
                f"Location: {job.get('location', 'N/A')}\n"
                f"Score: {job.get('fit_score', 'N/A')}\n"
                f"URL: {job['url']}\n\n"
                f"{job.get('full_description', '')}"
            )
            job_path.write_text(job_desc, encoding="utf-8")

            # Compute deterministic ATS keyword coverage (base vs tailored)
            ats_cov = compute_ats_coverage(
                job.get("full_description"), tailored, base_resume=job_base_resume, profile=profile
            )
            report["ats_coverage"] = ats_cov

            # Save validation report
            report_path = config.TAILORED_DIR / f"{prefix}_REPORT.json"
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

            # Generate PDF and DOCX for approved resumes (best-effort)
            pdf_path = None
            docx_path = None
            if report["status"] in SUCCESS_STATUSES:
                try:
                    from applypilot.scoring.docx_export import convert_to_docx
                    docx_path = str(convert_to_docx(txt_path, profile=profile))
                except Exception:
                    log.debug("DOCX generation failed for %s", txt_path, exc_info=True)
                try:
                    from applypilot.scoring.pdf import convert_to_pdf
                    pdf_path = str(convert_to_pdf(txt_path))
                except Exception:
                    log.debug("PDF generation failed for %s", txt_path, exc_info=True)

            # `path` is the value persisted as `tailored_resume_path`, which is what
            # `launcher.acquire_job` gates on. It stays NULL for anything not approved
            # so a rejected resume can never be uploaded and submitted.
            approved = report["status"] in SUCCESS_STATUSES
            result = {
                "url": job["url"],
                "path": str(txt_path) if approved else None,
                "artefact_path": str(txt_path),
                "pdf_path": pdf_path,
                "docx_path": docx_path,
                "ats_coverage_pct": ats_cov["coverage_pct"],
                "title": job.get("title", ""),
                "site": job.get("site", ""),
                "status": report["status"],
                "attempts": report["attempts"],
            }
        except Exception as e:
            result = {
                "url": job["url"], "title": job.get("title", ""), "site": job.get("site", ""),
                "status": "error", "attempts": 0, "path": None, "artefact_path": None,
                "pdf_path": None, "docx_path": None,
                "ats_coverage_pct": None,
            }
            log.error("%d/%d [ERROR] %s -- %s", completed, len(jobs), (job.get("title") or "")[:40], e)

        results.append(result)
        stats[_status_bucket(result.get("status"))] += 1

        elapsed = time.time() - t0
        rate = completed / elapsed if elapsed > 0 else 0
        log.info(
            "%d/%d [%s] attempts=%s | %.1f jobs/min | %s",
            completed, len(jobs),
            result["status"].upper(),
            result.get("attempts", "?"),
            rate * 60,
            (result.get("title") or "")[:40],
        )

    # Persist to DB: increment attempt counter for ALL, save path only for approved
    from applypilot.database import ensure_columns
    ensure_columns(conn)
    now = datetime.now(UTC).isoformat()
    for r in results:
        if _status_bucket(r["status"]) == "approved" and r.get("path"):
            pdf_gen_at = now if r.get("pdf_path") else None
            conn.execute(
                "UPDATE jobs SET tailored_resume_path=?, tailored_at=?, pdf_generated_at=?, "
                "docx_resume_path=?, ats_coverage_pct=?, "
                "crm_stage = CASE WHEN crm_stage IN ('discovered', 'scored') OR crm_stage IS NULL THEN 'tailored' ELSE crm_stage END, "
                "tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                (r["path"], now, pdf_gen_at, r.get("docx_path"), r.get("ats_coverage_pct"), r["url"]),
            )
        else:
            conn.execute(
                "UPDATE jobs SET tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                (r["url"],),
            )
    conn.commit()

    elapsed = time.time() - t0
    failed_total = (
        stats["failed_validation"] + stats["failed_judge"] + stats["failed_other"]
    )
    accounted = stats["approved"] + failed_total + stats["error"]
    if accounted != len(results):  # pragma: no cover - guards future status additions
        log.error("Tailoring accounting mismatch: %d counted vs %d results", accounted, len(results))
    log.info(
        "Tailoring done in %.1fs: %d approved, %d failed_validation, %d failed_judge, "
        "%d failed_other, %d errors",
        elapsed,
        stats["approved"],
        stats["failed_validation"],
        stats["failed_judge"],
        stats["failed_other"],
        stats["error"],
    )

    return {
        "approved": stats["approved"],
        "failed": failed_total,
        "errors": stats["error"],
        "elapsed": elapsed,
    }
