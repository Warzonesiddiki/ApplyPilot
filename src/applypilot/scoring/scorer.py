"""Job fit scoring: Explainable 4-dimensional LLM evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale across 4 sub-dimensions (Skills, Qualifications,
Domain, Location/Right-to-Work) and computes deterministic ATS keyword overlap.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from applypilot import config
from applypilot.database import ensure_columns, get_connection
from applypilot.enrichment.sponsors import is_job_expired
from applypilot.llm import get_client
from applypilot.scoring.ats import compute_ats_coverage, select_best_base_resume
from applypilot.schemas import ScoreResponse

log = logging.getLogger(__name__)
_SCORING_BATCH_BASE_SIZE = 16
_SCORING_BATCH_MAX_SIZE = 64


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a senior job fit evaluator across technology, engineering, accounting, audit, and finance. Given a candidate's resume and a job description, score how well the candidate fits the role overall and across 4 sub-dimensions.

SCORING CRITERIA (1-10):
- 9-10: Perfect match. Candidate has direct experience and qualifications (e.g., required technical stack OR required professional qualifications like ACA/ACCA/IFRS/FP&A) in nearly all required areas.
- 7-8: Strong match. Candidate has most required skills/qualifications, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill or qualification gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

Return exactly one JSON object (no Markdown) with these fields:
- score, skills_score, qualifications_score, domain_score, location_score: integers from 1 to 10
- keywords, missing_keywords: arrays of strings
- reasoning: 2-3 sentences explaining the score and fit"""


def _parse_score_response(response: str | ScoreResponse | dict[str, Any]) -> dict[str, Any]:
    """Validate the LLM scoring response and produce the stable pipeline shape."""
    if isinstance(response, ScoreResponse):
        validated = response
    elif isinstance(response, str):
        validated = ScoreResponse.model_validate_json(response)
    else:
        validated = ScoreResponse.model_validate(response)
    parsed = validated.model_dump()
    parsed["keywords"] = ", ".join(parsed["keywords"])
    parsed["missing_keywords"] = ", ".join(parsed["missing_keywords"])
    return parsed


def score_job(resume_text: str, job: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, Any]:
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
        response = client.structured_chat(
            messages,
            response_model=ScoreResponse,
            max_tokens=512,
            temperature=0.2,
        )
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


def run_scoring(limit: int = 0, rescore: bool = False, workers: int = 1) -> dict[str, Any]:
    """Score eligible jobs in bounded batches to cap database and worker memory.

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
    conditions = "full_description IS NOT NULL"
    if not rescore:
        conditions += " AND fit_score IS NULL"
    snapshot = conn.execute(f"SELECT COUNT(*), MAX(rowid) FROM jobs WHERE {conditions}").fetchone()
    total_eligible = int(snapshot[0] or 0) if snapshot is not None else 0
    max_rowid = int(snapshot[1]) if snapshot is not None and snapshot[1] is not None else 0
    target_count = min(total_eligible, limit) if limit > 0 else total_eligible

    if target_count == 0:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    batch_size = max(_SCORING_BATCH_BASE_SIZE, min(max(1, workers) * 2, _SCORING_BATCH_MAX_SIZE))
    log.info("Scoring %d jobs in batches of at most %d (workers=%d)...", target_count, batch_size, workers)
    started = time.perf_counter()
    completed_count = 0
    scored_count = 0
    errors = 0
    last_rowid = 0

    def _score_one(job_item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            best_res_txt, variant_name = select_best_base_resume(
                job_item.get("full_description"), default_resume_path=config.RESUME_PATH, profile=profile
            )
            res_txt = best_res_txt or default_resume_text
            scored = score_job(res_txt, job_item, profile=profile)
            scored["url"] = job_item["url"]
            scored["resume_variant"] = variant_name
            return job_item, scored
        except Exception as exc:
            log.exception("Unexpected failure preparing score for job %r", job_item.get("url"))
            return job_item, {"score": 0, "keywords": "", "reasoning": f"Scoring error: {exc}"}

    score_pool = (
        ThreadPoolExecutor(max_workers=min(workers, batch_size), thread_name_prefix="applypilot-score")
        if workers > 1
        else None
    )
    try:
        while completed_count < target_count:
            fetch_size = min(batch_size, target_count - completed_count)
            query = (
                "SELECT rowid AS _scoring_rowid, url, title, company, site, location, description, full_description "
                "FROM jobs WHERE rowid > ? AND rowid <= ? AND "
                f"{conditions} ORDER BY rowid LIMIT ?"
            )
            rows = conn.execute(query, (last_rowid, max_rowid, fetch_size)).fetchall()
            if not rows:
                break

            batch_jobs: list[dict[str, Any]] = []
            for row in rows:
                job_item = dict(row)
                rowid = int(job_item.pop("_scoring_rowid"))
                last_rowid = max(last_rowid, rowid)
                batch_jobs.append(job_item)

            batch_results: list[dict[str, Any]] = []
            if score_pool is not None and len(batch_jobs) > 1:
                future_to_job = {score_pool.submit(_score_one, job): job for job in batch_jobs}
                for future in as_completed(future_to_job):
                    job = future_to_job[future]
                    try:
                        _, result = future.result()
                    except Exception as exc:
                        log.exception("Scoring worker crashed for job %r", job.get("url"))
                        result = {"score": 0, "keywords": "", "reasoning": f"Scoring worker error: {exc}"}
                    completed_count += 1
                    if result["score"] == 0:
                        errors += 1
                    else:
                        batch_results.append(result)
                    log.info(
                        "[%d/%d] score=%d (ATS=%d%%)  %s",
                        completed_count,
                        target_count,
                        result["score"],
                        result.get("ats_coverage_pct", 0),
                        (job.get("title") or "?")[:60],
                    )
            else:
                for job in batch_jobs:
                    _, result = _score_one(job)
                    completed_count += 1
                    if result["score"] == 0:
                        errors += 1
                    else:
                        batch_results.append(result)
                    log.info(
                        "[%d/%d] score=%d (ATS=%d%%)  %s",
                        completed_count,
                        target_count,
                        result["score"],
                        result.get("ats_coverage_pct", 0),
                        (job.get("title") or "?")[:60],
                    )

            # Persist each completed batch; failures remain eligible for a later run.
            now = datetime.now(UTC).isoformat()
            for result in batch_results:
                conn.execute(
                    "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ?, "
                    "score_breakdown = ?, ats_coverage_pct = COALESCE(ats_coverage_pct, ?), "
                    "crm_stage = CASE WHEN crm_stage IS NULL OR crm_stage = 'discovered' THEN 'scored' ELSE crm_stage END "
                    "WHERE url = ?",
                    (
                        result["score"],
                        f"{result.get('keywords', '')}\n{result['reasoning']}",
                        now,
                        result.get("score_breakdown"),
                        result.get("ats_coverage_pct"),
                        result["url"],
                    ),
                )
            conn.commit()
            scored_count += len(batch_results)
            del batch_jobs, batch_results, rows

    finally:
        if score_pool is not None:
            score_pool.shutdown(wait=True, cancel_futures=True)

    elapsed = time.perf_counter() - started
    log.info(
        "Done: %d scored, %d errors in %.1fs (%.1f jobs/sec)",
        scored_count,
        errors,
        elapsed,
        completed_count / elapsed if elapsed > 0 else 0,
    )

    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": scored_count,
        "errors": errors,
        "elapsed": elapsed,
        "distribution": distribution,
    }
