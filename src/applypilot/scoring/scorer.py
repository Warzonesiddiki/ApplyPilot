"""Job fit scoring: Explainable 4-dimensional LLM evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale across 4 sub-dimensions (Skills, Qualifications,
Domain, Location/Right-to-Work) and computes deterministic ATS keyword overlap.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import re
import time
from datetime import UTC, datetime

from applypilot import config
from applypilot.database import ensure_columns, get_connection, get_jobs_by_stage
from applypilot.enrichment.sponsors import is_job_expired
from applypilot.llm import get_client
from applypilot.scoring.ats import compute_ats_coverage, select_best_base_resume

log = logging.getLogger(__name__)


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a senior job fit evaluator across technology, engineering, accounting, audit, and finance. Given a candidate's resume and a job description, score how well the candidate fits the role overall and across 4 sub-dimensions.

SCORING CRITERIA (1-10):
- 9-10: Perfect match. Candidate has direct experience and qualifications (e.g., required technical stack OR required professional qualifications like ACA/ACCA/IFRS/FP&A) in nearly all required areas.
- 7-8: Strong match. Candidate has most required skills/qualifications, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill or qualification gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
SKILLS_SCORE: [1-10]
QUALIFICATIONS_SCORE: [1-10]
DOMAIN_SCORE: [1-10]
LOCATION_SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match the candidate]
MISSING_KEYWORDS: [comma-separated ATS keywords from the job description that should be emphasized or bridged]
REASONING: [2-3 sentences explaining the score and fit]"""


def _extract_int_1_to_10(line: str, default: int = 0) -> int:
    try:
        match = re.search(r"\d+", line)
        if match:
            return max(1, min(10, int(match.group())))
    except (AttributeError, ValueError):
        pass
    return default


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data including 4D sub-scores."""
    score = 0
    skills_score = 0
    qualifications_score = 0
    domain_score = 0
    location_score = 0
    keywords = ""
    missing_keywords = ""
    reasoning = response.strip()

    for raw_line in response.split("\n"):
        line = raw_line.strip()
        upper = line.upper()
        if upper.startswith("SCORE:"):
            score = _extract_int_1_to_10(line, 0)
        elif upper.startswith("SKILLS_SCORE:"):
            skills_score = _extract_int_1_to_10(line, 0)
        elif upper.startswith("QUALIFICATIONS_SCORE:"):
            qualifications_score = _extract_int_1_to_10(line, 0)
        elif upper.startswith("DOMAIN_SCORE:"):
            domain_score = _extract_int_1_to_10(line, 0)
        elif upper.startswith("LOCATION_SCORE:"):
            location_score = _extract_int_1_to_10(line, 0)
        elif upper.startswith("KEYWORDS:"):
            keywords = line.split(":", 1)[1].strip()
        elif upper.startswith("MISSING_KEYWORDS:"):
            missing_keywords = line.split(":", 1)[1].strip()
        elif upper.startswith("REASONING:"):
            reasoning = line.split(":", 1)[1].strip()

    if score > 0:
        skills_score = skills_score or score
        qualifications_score = qualifications_score or score
        domain_score = domain_score or score
        location_score = location_score or score

    return {
        "score": score,
        "skills_score": skills_score,
        "qualifications_score": qualifications_score,
        "domain_score": domain_score,
        "location_score": location_score,
        "keywords": keywords,
        "missing_keywords": missing_keywords,
        "reasoning": reasoning,
    }


def score_job(resume_text: str, job: dict, profile: dict | None = None) -> dict:
    """Score a single job against the resume with 4D sub-scores, ATS coverage, and expired pre-filtering."""
    company_name = job.get("company") or job.get("site") or "Unknown"
    full_desc = job.get("full_description") or job.get("description") or ""
    ats = compute_ats_coverage(full_desc, resume_text, profile=profile)

    if is_job_expired(full_desc):
        return {
            "score": 1,
            "skills_score": 1,
            "qualifications_score": 1,
            "domain_score": 1,
            "location_score": 1,
            "keywords": ", ".join(ats["matched_keywords"]),
            "missing_keywords": ", ".join(ats["missing_keywords"]),
            "reasoning": "Listing is closed or expired (pre-filtered before LLM call).",
            "ats_coverage_pct": ats["coverage_pct"],
            "score_breakdown": json.dumps(
                {
                    "skills_score": 1,
                    "qualifications_score": 1,
                    "domain_score": 1,
                    "location_score": 1,
                    "matched_keywords": ats["matched_keywords"],
                    "missing_keywords": ats["missing_keywords"],
                    "baseline_ats_pct": ats["coverage_pct"],
                    "prefiltered": "expired_listing",
                }
            ),
        }

    job_text = (
        f"TITLE: {job.get('title', 'Untitled')}\n"
        f"COMPANY: {company_name}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{full_desc[:6000]}"
    )

    messages = [
        {"role": "system", "content": SCORE_PROMPT},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    try:
        client = get_client()
        response = client.chat(messages, max_tokens=512, temperature=0.2)
        parsed = _parse_score_response(response)
    except Exception as e:
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        return {"score": 0, "keywords": "", "reasoning": f"LLM error: {e}"}

    if not parsed.get("keywords") and ats["matched_keywords"]:
        parsed["keywords"] = ", ".join(ats["matched_keywords"])
    if not parsed.get("missing_keywords") and ats["missing_keywords"]:
        parsed["missing_keywords"] = ", ".join(ats["missing_keywords"])

    parsed["ats_coverage_pct"] = ats["coverage_pct"]
    parsed["score_breakdown"] = json.dumps(
        {
            "skills_score": parsed["skills_score"],
            "qualifications_score": parsed["qualifications_score"],
            "domain_score": parsed["domain_score"],
            "location_score": parsed["location_score"],
            "matched_keywords": ats["matched_keywords"]
            or [k.strip() for k in parsed["keywords"].split(",") if k.strip()],
            "missing_keywords": ats["missing_keywords"]
            or [k.strip() for k in parsed["missing_keywords"].split(",") if k.strip()],
            "baseline_ats_pct": ats["coverage_pct"],
        }
    )
    return parsed


def run_scoring(limit: int = 0, rescore: bool = False, workers: int = 1) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).
        workers: Number of parallel LLM scoring threads.

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    default_resume_text = config.RESUME_PATH.read_text(encoding="utf-8")
    try:
        profile = config.load_profile()
    except Exception:
        profile = None

    conn = get_connection()
    ensure_columns(conn)

    if rescore:
        query = "SELECT * FROM jobs WHERE full_description IS NOT NULL"
        params: list = []
        if limit > 0:
            query += " LIMIT ?"
            params.append(limit)
        jobs = conn.execute(query, params).fetchall()
    else:
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit)

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    log.info("Scoring %d jobs (workers=%d)...", len(jobs), workers)
    t0 = time.time()
    errors = 0
    results: list[dict] = []

    def _score_one(job_item: dict) -> tuple[dict, dict]:
        best_res_txt, variant_name = select_best_base_resume(
            job_item.get("full_description"), default_resume_path=config.RESUME_PATH, profile=profile
        )
        res_txt = best_res_txt or default_resume_text
        scored = score_job(res_txt, job_item, profile=profile)
        scored["url"] = job_item["url"]
        scored["resume_variant"] = variant_name
        return job_item, scored

    if workers > 1 and len(jobs) > 1:
        with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
            futs = [pool.submit(_score_one, j) for j in jobs]
            for completed, fut in enumerate(as_completed(futs), 1):
                job, result = fut.result()
                if result["score"] == 0:
                    errors += 1
                else:
                    results.append(result)
                log.info(
                    "[%d/%d] score=%d (ATS=%d%%)  %s",
                    completed,
                    len(jobs),
                    result["score"],
                    result.get("ats_coverage_pct", 0),
                    (job.get("title") or "?")[:60],
                )
    else:
        for completed, job in enumerate(jobs, 1):
            _, result = _score_one(job)
            if result["score"] == 0:
                errors += 1
            else:
                results.append(result)
            log.info(
                "[%d/%d] score=%d (ATS=%d%%)  %s",
                completed,
                len(jobs),
                result["score"],
                result.get("ats_coverage_pct", 0),
                (job.get("title") or "?")[:60],
            )

    # Write valid scores (> 0) to DB; leave errors as NULL so they can be retried
    now = datetime.now(UTC).isoformat()
    for r in results:
        conn.execute(
            "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ?, "
            "score_breakdown = ?, ats_coverage_pct = COALESCE(ats_coverage_pct, ?), "
            "crm_stage = CASE WHEN crm_stage IS NULL OR crm_stage = 'discovered' THEN 'scored' ELSE crm_stage END "
            "WHERE url = ?",
            (
                r["score"],
                f"{r['keywords']}\n{r['reasoning']}",
                now,
                r.get("score_breakdown"),
                r.get("ats_coverage_pct"),
                r["url"],
            ),
        )
    conn.commit()

    elapsed = time.time() - t0
    log.info(
        "Done: %d scored, %d errors in %.1fs (%.1f jobs/sec)",
        len(results), errors, elapsed, len(jobs) / elapsed if elapsed > 0 else 0,
    )

    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": len(results),
        "errors": errors,
        "elapsed": elapsed,
        "distribution": distribution,
    }
