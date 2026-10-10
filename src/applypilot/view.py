"""ApplyPilot HTML Dashboard & Career CRM View Generator.

Generates a self-contained HTML dashboard with:
  - Multi-profile header & KPI summary cards (Total, Enriched, Scored, High-Fit 7+,
    Verified UK/EU Sponsors, Avg ATS Keyword Coverage %, Applied / Interviewing)
  - Score distribution bar chart & CRM stage funnel
  - Jobs-by-source breakdown
  - Filterable job cards with Company Name, 4D Sub-Scores, ATS Coverage Badge,
    UK/EU Visa Sponsor Badge, Work Arrangement Pill, and expandable Tailored
    Resume / Cover Letter / Interview Prep / Outreach drawers
  - Interactive CRM stage & notes editing when served via `applypilot serve`
"""

from __future__ import annotations

from html import escape
import json
from pathlib import Path
import webbrowser

from rich.console import Console

from applypilot import config
from applypilot.database import ensure_columns, get_connection, get_stats

console = Console()


def _read_optional_file(path_str: str | None) -> str:
    if not path_str:
        return ""
    try:
        p = Path(path_str)
        if p.exists():
            return p.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""


def generate_dashboard(output_path: str | None = None, interactive_api: bool = False) -> str:
    """Generate an HTML dashboard of all jobs with fit scores, ATS coverage, and CRM controls.

    Args:
        output_path: Where to write the HTML file. Defaults to `<APP_DIR>/dashboard.html`.
        interactive_api: If True, enables live `/api/jobs/*` CRM stage & prep actions.

    Returns:
        Absolute path to the generated HTML file.
    """
    out = Path(output_path) if output_path else config.APP_DIR / "dashboard.html"

    conn = get_connection()
    ensure_columns(conn)
    stats = get_stats(conn)

    total = stats.get("total", 0)
    ready = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL AND application_url IS NOT NULL"
    ).fetchone()[0]
    scored = stats.get("scored", 0)
    high_fit = conn.execute("SELECT COUNT(*) FROM jobs WHERE fit_score >= 7").fetchone()[0]
    verified_sponsors = stats.get("verified_sponsors", 0)
    avg_ats = stats.get("avg_ats_coverage", 0.0)
    applied_count = stats.get("applied", 0)

    # Score distribution
    score_dist: dict[int, int] = dict(stats.get("score_distribution", []))

    # Site stats
    site_stats = conn.execute("""
        SELECT site,
               COUNT(*) as total,
               SUM(CASE WHEN fit_score >= 7 THEN 1 ELSE 0 END) as high_fit,
               SUM(CASE WHEN fit_score BETWEEN 5 AND 6 THEN 1 ELSE 0 END) as mid_fit,
               SUM(CASE WHEN fit_score < 5 AND fit_score IS NOT NULL THEN 1 ELSE 0 END) as low_fit,
               SUM(CASE WHEN fit_score IS NULL THEN 1 ELSE 0 END) as unscored,
               ROUND(AVG(fit_score), 1) as avg_score
        FROM jobs GROUP BY site ORDER BY high_fit DESC, total DESC
    """).fetchall()

    # Fetch all jobs (scored first, then unscored discovered jobs so dashboard is never empty)
    jobs = conn.execute("""
        SELECT url, title, company, salary, description, location, site, strategy,
               full_description, application_url, detail_error,
               fit_score, score_reasoning, score_breakdown,
               ats_coverage_pct, sponsor_status, work_arrangement,
               tailored_resume_path, docx_resume_path, cover_letter_path,
               prep_pack_path, outreach_path, crm_stage, user_notes
        FROM jobs
        ORDER BY COALESCE(fit_score, 0) DESC, site, title
    """).fetchall()

    base_resume_txt = _read_optional_file(str(config.RESUME_PATH)) if config.RESUME_PATH.exists() else ""

    colors = {
        "RemoteOK": "#10b981", "WelcomeToTheJungle": "#f59e0b",
        "indeed": "#2164f3", "linkedin": "#0a66c2", "glassdoor": "#0caa41",
        "Reed.co.uk": "#ec4899", "eFinancialCareers UK": "#3b82f6",
        "ICAEW Jobs": "#10b981", "ACCA Careers": "#ef4444",
    }

    score_bars = ""
    max_count = max(score_dist.values()) if score_dist else 1
    for s in range(10, 0, -1):
        count = score_dist.get(s, 0)
        pct = (count / max_count * 100) if max_count else 0
        score_color = "#10b981" if s >= 7 else ("#f59e0b" if s >= 5 else "#ef4444")
        score_bars += f"""
        <div class="score-row">
          <span class="score-label">{s}</span>
          <div class="score-bar-track">
            <div class="score-bar-fill" style="width:{pct}%;background:{score_color}"></div>
          </div>
          <span class="score-count">{count}</span>
        </div>"""

    site_rows = ""
    for s in site_stats:
        site = s["site"] or "?"
        color = colors.get(site, "#60a5fa")
        avg = s["avg_score"] or 0
        site_rows += f"""
        <div class="site-row">
          <div class="site-name" style="color:{color}">{escape(site)}</div>
          <div class="site-nums">{s['total']} jobs &middot; {s['high_fit']} strong fit &middot; avg score {avg}</div>
          <div class="bar-track">
            <div class="bar-fill" style="width:{s['high_fit']/max(s['total'],1)*100}%;background:{color}"></div>
            <div class="bar-fill" style="width:{s['mid_fit']/max(s['total'],1)*100}%;background:{color}66"></div>
          </div>
        </div>"""

    job_sections = ""
    current_score = None
    for j in jobs:
        score = j["fit_score"] if j["fit_score"] is not None else 0
        if score != current_score:
            if current_score is not None:
                job_sections += "</div>"
            score_color = "#10b981" if score >= 7 else ("#f59e0b" if score >= 5 else "#64748b")
            score_label = {
                10: "Perfect Match", 9: "Excellent Fit", 8: "Strong Fit",
                7: "Good Fit", 6: "Moderate+", 5: "Moderate", 0: "Unscored / Discovered",
            }.get(score, f"Score {score}")
            count_at_score = score_dist.get(score, 0) if score > 0 else (total - scored)
            job_sections += f"""
            <h2 class="score-header" style="border-color:{score_color}">
              <span class="score-badge" style="background:{score_color}">{score if score > 0 else '–'}</span>
              {score_label} ({count_at_score} jobs)
            </h2>
            <div class="job-grid">"""
            current_score = score

        title = escape(j["title"] or "Untitled")
        company = escape(j["company"] or j["site"] or "Employer")
        url = escape(j["url"] or "")
        salary = escape(j["salary"] or "")
        location = escape(j["location"] or "")
        site = escape(j["site"] or "")
        site_color = colors.get(j["site"] or "", "#60a5fa")
        apply_url = escape(j["application_url"] or j["url"] or "")
        sponsor_status = j["sponsor_status"] or "unknown"
        work_arr = j["work_arrangement"] or "onsite"
        crm_stage = j["crm_stage"] or "discovered"
        ats_pct = j["ats_coverage_pct"]

        reasoning_raw = j["score_reasoning"] or ""
        reasoning_lines = reasoning_raw.split("\n")
        keywords = reasoning_lines[0][:140] if reasoning_lines else ""
        reasoning = reasoning_lines[1][:240] if len(reasoning_lines) > 1 else ""

        sub_pills_html = ""
        if j["score_breakdown"]:
            try:
                bd = json.loads(j["score_breakdown"])
                sub_pills_html = (
                    f'<div class="subscore-row">'
                    f'<span class="sub-pill">Skills: {bd.get("skills_score", "-")}</span>'
                    f'<span class="sub-pill">Quals: {bd.get("qualifications_score", "-")}</span>'
                    f'<span class="sub-pill">Domain: {bd.get("domain_score", "-")}</span>'
                    f'<span class="sub-pill">Loc/Visa: {bd.get("location_score", "-")}</span>'
                    f"</div>"
                )
            except Exception:
                pass

        meta_parts = [
            f'<span class="meta-tag company-tag">{company}</span>',
            f'<span class="meta-tag site-tag" style="background:{site_color}33;color:{site_color}">{site}</span>',
        ]
        if salary:
            meta_parts.append(f'<span class="meta-tag salary">{salary}</span>')
        if location:
            meta_parts.append(f'<span class="meta-tag location">{location[:42]}</span>')
        if ats_pct is not None:
            meta_parts.append(f'<span class="meta-tag ats-tag">ATS: {ats_pct}%</span>')
        if sponsor_status == "sponsorship_offered":
            meta_parts.append('<span class="meta-tag sponsor-yes">★ Visa Sponsorship Offered</span>')
        elif sponsor_status == "licensed_sponsor":
            meta_parts.append('<span class="meta-tag sponsor-lic">✓ Licensed UK/EU Sponsor</span>')
        elif sponsor_status == "no_sponsorship_stated":
            meta_parts.append('<span class="meta-tag sponsor-no">⚠ No Sponsorship Stated</span>')
        meta_parts.append(f'<span class="meta-tag arr-tag">{escape(work_arr.title())}</span>')
        meta_parts.append(f'<span class="meta-tag stage-tag">{escape(crm_stage.upper())}</span>')
        meta_html = " ".join(meta_parts)

        full_text = j["full_description"] or j["description"] or ""
        desc_preview = escape(full_text)[:280]
        full_desc_html = escape(full_text).replace("\n", "<br>")
        desc_len = len(full_text)

        tailored_txt = _read_optional_file(j["tailored_resume_path"])
        cover_txt = _read_optional_file(j["cover_letter_path"])
        prep_txt = _read_optional_file(j["prep_pack_path"])
        outreach_txt = _read_optional_file(j["outreach_path"])

        drawers_html = ""
        if full_text:
            drawers_html += (
                f"<details class='full-desc-details'><summary class='expand-btn'>"
                f"Full Description ({desc_len:,} chars)</summary>"
                f"<div class='full-desc'>{full_desc_html}</div></details>"
            )
        if tailored_txt:
            drawers_html += (
                f"<details class='full-desc-details'><summary class='expand-btn'>"
                f"Tailored CV Preview</summary>"
                f"<div class='full-desc'>{escape(tailored_txt)}</div></details>"
            )
            if base_resume_txt:
                drawers_html += (
                    f"<details class='full-desc-details'><summary class='expand-btn'>"
                    f"Base vs. Tailored CV Side-by-Side Diff</summary>"
                    f"<div class='diff-grid'>"
                    f"<div class='full-desc'><strong>Base CV</strong>\n\n{escape(base_resume_txt)}</div>"
                    f"<div class='full-desc'><strong>Tailored CV (ATS: {ats_pct if ats_pct is not None else '-'}%)</strong>\n\n{escape(tailored_txt)}</div>"
                    f"</div></details>"
                )
        if cover_txt:
            drawers_html += (
                f"<details class='full-desc-details'><summary class='expand-btn'>"
                f"Cover Letter Preview</summary>"
                f"<div class='full-desc'>{escape(cover_txt)}</div></details>"
            )
        if prep_txt:
            drawers_html += (
                f"<details class='full-desc-details'><summary class='expand-btn'>"
                f"Interview Prep Dossier</summary>"
                f"<div class='full-desc'>{escape(prep_txt)}</div></details>"
            )
        if outreach_txt:
            drawers_html += (
                f"<details class='full-desc-details'><summary class='expand-btn'>"
                f"LinkedIn & Cold Email Outreach</summary>"
                f"<div class='full-desc'>{escape(outreach_txt)}</div></details>"
            )

        user_notes_txt = escape(j["user_notes"] or "")
        notes_html = f'<div class="notes-box">📝 {user_notes_txt}</div>' if user_notes_txt else ""
        if interactive_api:
            notes_html += (
                f'<div class="notes-edit-row">'
                f'<input type="text" class="search-input notes-input" placeholder="Add CRM note / follow-up..." value="{user_notes_txt}" '
                f'onchange="saveJobNote(\'{url}\', this.value)">'
                f'</div>'
            )

        crm_controls_html = ""
        if interactive_api:
            stages_list = [
                "discovered", "enriched", "scored", "tailored",
                "ready_to_apply", "applied", "assessment", "interviewing", "offer", "rejected", "archived",
            ]
            opts = "".join(
                f'<option value="{st}" {"selected" if st == crm_stage else ""}>{st.upper()}</option>'
                for st in stages_list
            )
            crm_controls_html = (
                f'<select class="select-input stage-select" onchange="updateJobStage(\'{url}\', this.value)">{opts}</select>'
                f'<button class="filter-btn" onclick="generatePrepAndOutreach(\'{url}\', this)">⚡ Prep & Outreach</button>'
                f'<button class="filter-btn" onclick="generateProofPacket(\'{url}\', this)">🛡 Proof</button>'
            )

        apply_html = f'<a href="{apply_url}" class="apply-link" target="_blank">Apply</a>' if apply_url else ""

        job_sections += f"""
        <div class="job-card" data-score="{score}" data-site="{escape(j['site'] or '')}"
             data-location="{location.lower()}" data-sponsor="{escape(sponsor_status)}"
             data-arrangement="{escape(work_arr)}" data-stage="{escape(crm_stage)}"
             data-url="{url}">
          <div class="card-header">
            <span class="score-pill" style="background:{'#10b981' if score >= 7 else ('#f59e0b' if score >= 5 else '#64748b')}">{score if score > 0 else '–'}</span>
            <a href="{url}" class="job-title" target="_blank">{title}</a>
          </div>
          <div class="meta-row">{meta_html}</div>
          {sub_pills_html}
          {f'<div class="keywords-row">{escape(keywords)}</div>' if keywords else ''}
          {f'<div class="reasoning-row">{escape(reasoning)}</div>' if reasoning else ''}
          {notes_html}
          <p class="desc-preview">{desc_preview}...</p>
          {drawers_html}
          <div class="card-footer">{crm_controls_html}{apply_html}</div>
        </div>"""

    if current_score is not None:
        job_sections += "</div>"

    active_prof = config.get_active_profile() or "default"
    prof_options_html = ""
    if interactive_api:
        all_profs = [p["name"] for p in config.list_profiles()] or ["default"]
        if active_prof not in all_profs:
            all_profs.insert(0, active_prof)
        opts_str = "".join(
            f'<option value="{escape(p)}" {"selected" if p == active_prof else ""}>{escape(p)}</option>'
            for p in all_profs
        )
        prof_options_html = (
            f'<span class="filter-label">Profile:</span>'
            f'<select class="select-input" onchange="window.location.href=\'/?profile=\' + encodeURIComponent(this.value)">{opts_str}</select>'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ApplyPilot Career CRM & Dashboard ({escape(active_prof)})</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background: #0f172a; color: #e2e8f0; padding: 2rem; }}
  h1 {{ font-size: 1.8rem; font-weight: 700; margin-bottom: 0.4rem; }}
  .subtitle {{ color: #94a3b8; margin-bottom: 2rem; }}
  .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 1rem; margin-bottom: 2rem; }}
  .stat-card {{ background: #1e293b; border-radius: 12px; padding: 1.15rem; }}
  .stat-num {{ font-size: 1.8rem; font-weight: 700; }}
  .stat-label {{ color: #94a3b8; font-size: 0.8rem; margin-top: 0.25rem; }}
  .stat-ok .stat-num {{ color: #10b981; }}
  .stat-scored .stat-num {{ color: #60a5fa; }}
  .stat-high .stat-num {{ color: #f59e0b; }}
  .stat-total .stat-num {{ color: #e2e8f0; }}
  .filters {{ background: #1e293b; border-radius: 12px; padding: 1.1rem; margin-bottom: 2rem; display: flex; gap: 0.75rem; flex-wrap: wrap; align-items: center; }}
  .filter-label {{ color: #94a3b8; font-size: 0.82rem; font-weight: 600; }}
  .filter-btn {{ background: #334155; border: none; color: #94a3b8; padding: 0.4rem 0.75rem; border-radius: 6px; cursor: pointer; font-size: 0.78rem; transition: all 0.15s; }}
  .filter-btn:hover {{ background: #475569; color: #e2e8f0; }}
  .filter-btn.active {{ background: #60a5fa; color: #0f172a; font-weight: 600; }}
  .search-input, .select-input {{ background: #334155; border: 1px solid #475569; color: #e2e8f0; padding: 0.4rem 0.75rem; border-radius: 6px; font-size: 0.8rem; }}
  .score-section {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 2.5rem; }}
  .score-dist, .sites-section {{ background: #1e293b; border-radius: 12px; padding: 1.5rem; }}
  .score-dist h3, .sites-section h3 {{ font-size: 1rem; margin-bottom: 1rem; color: #94a3b8; }}
  .score-row {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.4rem; }}
  .score-label {{ width: 1.5rem; text-align: right; font-size: 0.85rem; font-weight: 600; }}
  .score-bar-track {{ flex: 1; height: 14px; background: #334155; border-radius: 4px; overflow: hidden; }}
  .score-bar-fill {{ height: 100%; border-radius: 4px; }}
  .score-count {{ width: 2.5rem; font-size: 0.8rem; color: #94a3b8; }}
  .site-row {{ margin-bottom: 0.8rem; }}
  .site-name {{ font-weight: 600; font-size: 0.9rem; }}
  .site-nums {{ color: #94a3b8; font-size: 0.75rem; margin: 0.15rem 0; }}
  .bar-track {{ height: 8px; background: #334155; border-radius: 4px; display: flex; overflow: hidden; }}
  .bar-fill {{ height: 100%; }}
  .score-header {{ font-size: 1.2rem; font-weight: 600; margin: 2.2rem 0 1rem; padding-bottom: 0.5rem; border-bottom: 3px solid; display: flex; align-items: center; gap: 0.75rem; }}
  .score-badge {{ display: inline-flex; align-items: center; justify-content: center; width: 2rem; height: 2rem; border-radius: 8px; color: #0f172a; font-weight: 700; font-size: 1rem; }}
  .job-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(390px, 1fr)); gap: 1rem; }}
  .job-card {{ background: #1e293b; border-radius: 10px; padding: 1rem; border-left: 3px solid #334155; }}
  .card-header {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem; }}
  .score-pill {{ display: inline-flex; align-items: center; justify-content: center; min-width: 1.6rem; height: 1.6rem; border-radius: 6px; color: #0f172a; font-weight: 700; font-size: 0.8rem; flex-shrink: 0; }}
  .job-title {{ color: #e2e8f0; text-decoration: none; font-weight: 600; font-size: 0.95rem; }}
  .job-title:hover {{ color: #60a5fa; }}
  .meta-row, .subscore-row {{ display: flex; flex-wrap: wrap; gap: 0.35rem; margin-bottom: 0.4rem; }}
  .meta-tag {{ font-size: 0.7rem; padding: 0.15rem 0.45rem; border-radius: 4px; background: #334155; color: #94a3b8; }}
  .meta-tag.company-tag {{ background: #312e81; color: #c7d2fe; font-weight: 600; }}
  .meta-tag.salary {{ background: #064e3b; color: #6ee7b7; }}
  .meta-tag.location {{ background: #1e3a5f; color: #93c5fd; }}
  .meta-tag.ats-tag {{ background: #3b0764; color: #e9d5ff; font-weight: 600; }}
  .meta-tag.sponsor-yes, .meta-tag.sponsor-lic {{ background: #065f46; color: #a7f3d0; font-weight: 600; }}
  .meta-tag.sponsor-no {{ background: #7f1d1d; color: #fecaca; }}
  .meta-tag.stage-tag {{ background: #1e293b; border: 1px solid #475569; color: #f8fafc; }}
  .sub-pill {{ font-size: 0.68rem; padding: 0.1rem 0.4rem; border-radius: 4px; background: #0f172a; color: #cbd5e1; border: 1px solid #334155; }}
  .keywords-row {{ font-size: 0.75rem; color: #10b981; margin-bottom: 0.3rem; line-height: 1.4; }}
  .reasoning-row {{ font-size: 0.75rem; color: #94a3b8; margin-bottom: 0.5rem; font-style: italic; line-height: 1.4; }}
  .desc-preview {{ font-size: 0.8rem; color: #64748b; line-height: 1.5; margin-bottom: 0.75rem; max-height: 3.6em; overflow: hidden; }}
  .card-footer {{ display: flex; justify-content: flex-end; gap: 0.5rem; }}
  .apply-link {{ font-size: 0.8rem; color: #60a5fa; text-decoration: none; padding: 0.3rem 0.8rem; border: 1px solid #60a5fa33; border-radius: 6px; font-weight: 500; }}
  .full-desc-details {{ margin-bottom: 0.5rem; }}
  .expand-btn {{ font-size: 0.78rem; color: #60a5fa; cursor: pointer; list-style: none; padding: 0.2rem 0; }}
  .full-desc {{ font-size: 0.78rem; color: #cbd5e1; line-height: 1.55; margin-top: 0.4rem; padding: 0.75rem; background: #0f172a; border-radius: 8px; max-height: 350px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; }}
  .diff-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0.6rem; }}
  .stage-select {{ font-size: 0.72rem; padding: 0.25rem 0.45rem; }}
  .notes-box {{ font-size: 0.74rem; color: #fde68a; background: #451a0366; padding: 0.3rem 0.5rem; border-radius: 5px; margin-bottom: 0.45rem; white-space: pre-wrap; }}
  .notes-edit-row {{ margin-bottom: 0.45rem; }}
  .notes-input {{ width: 100%; font-size: 0.74rem; padding: 0.28rem 0.5rem; }}
  .kanban-board {{ display: grid; grid-template-columns: repeat(5, minmax(260px, 1fr)); gap: 1rem; overflow-x: auto; margin-top: 1rem; }}
  .kanban-col {{ background: #1e293b88; border: 1px solid #334155; border-radius: 12px; padding: 0.85rem; min-height: 360px; }}
  .kanban-col h3 {{ font-size: 0.88rem; color: #e2e8f0; margin-bottom: 0.75rem; padding-bottom: 0.45rem; border-bottom: 2px solid #475569; display: flex; justify-content: space-between; }}
  .kanban-col .job-card {{ margin-bottom: 0.75rem; }}
  .hidden {{ display: none !important; }}
  .job-count {{ color: #94a3b8; font-size: 0.85rem; margin-bottom: 1rem; }}
</style>
</head>
<body>

<h1>ApplyPilot Career CRM & Dashboard ({escape(active_prof)})</h1>
<p class="subtitle">{total} jobs &middot; {scored} scored &middot; {high_fit} strong matches (7+) &middot; {verified_sponsors} UK/EU licensed sponsors &middot; {avg_ats}% avg ATS coverage</p>

<div class="summary">
  <div class="stat-card stat-total"><div class="stat-num">{total}</div><div class="stat-label">Total Jobs</div></div>
  <div class="stat-card stat-ok"><div class="stat-num">{ready}</div><div class="stat-label">Enriched (Ready)</div></div>
  <div class="stat-card stat-scored"><div class="stat-num">{scored}</div><div class="stat-label">Scored by AI</div></div>
  <div class="stat-card stat-high"><div class="stat-num">{high_fit}</div><div class="stat-label">Strong Fit (7+)</div></div>
  <div class="stat-card stat-ok"><div class="stat-num">{verified_sponsors}</div><div class="stat-label">Verified UK/EU Sponsors</div></div>
  <div class="stat-card stat-scored"><div class="stat-num">{avg_ats}%</div><div class="stat-label">Avg ATS Coverage</div></div>
  <div class="stat-card stat-ok"><div class="stat-num">{applied_count}</div><div class="stat-label">Applied</div></div>
</div>

<div class="filters">
  {prof_options_html}
  <span class="filter-label">Score:</span>
  <button class="filter-btn active" onclick="filterScore(0, event)">All</button>
  <button class="filter-btn" onclick="filterScore(7, event)">7+ Strong</button>
  <button class="filter-btn" onclick="filterScore(8, event)">8+ Excellent</button>
  <button class="filter-btn" onclick="filterScore(9, event)">9+ Perfect</button>
  <span class="filter-label">Sponsor:</span>
  <select class="select-input" onchange="filterSponsor(this.value)">
    <option value="">All Employers</option>
    <option value="verified">Licensed / Sponsorship Offered</option>
  </select>
  <span class="filter-label">Mode:</span>
  <select class="select-input" onchange="filterArrangement(this.value)">
    <option value="">All Modes</option>
    <option value="remote">Remote</option>
    <option value="hybrid">Hybrid</option>
    <option value="onsite">Onsite</option>
  </select>
  <span class="filter-label">CRM Stage:</span>
  <select class="select-input" onchange="filterStage(this.value)">
    <option value="">All Stages</option>
    <option value="discovered">Discovered</option>
    <option value="scored">Scored</option>
    <option value="tailored">Tailored</option>
    <option value="applied">Applied</option>
    <option value="interviewing">Interviewing</option>
    <option value="offer">Offer</option>
    <option value="rejected">Rejected</option>
  </select>
  <span class="filter-label">Search:</span>
  <input type="text" class="search-input" placeholder="Company, title, location, keyword..." oninput="filterText(this.value)">
  <span class="filter-label">Layout:</span>
  <button class="filter-btn" id="view-toggle-btn" onclick="toggleKanbanView()">📋 Kanban Board</button>
  {('<button class="filter-btn" onclick="syncInbox(this)">📬 Sync Inbox</button><a href="/api/export?format=csv" class="apply-link" style="margin-left:auto">⬇ Export CSV</a>' if interactive_api else '')}
</div>

<div class="score-section">
  <div class="score-dist">
    <h3>Score Distribution</h3>
    {score_bars}
  </div>
  <div class="sites-section">
    <h3>By Source</h3>
    {site_rows}
  </div>
</div>

<div id="job-count" class="job-count"></div>

<div id="grid-container">
{job_sections}
</div>

<div id="kanban-container" class="kanban-board hidden">
  <div class="kanban-col" data-bucket="funnel_1"><h3>1. Discovered / Scored</h3><div class="kanban-items"></div></div>
  <div class="kanban-col" data-bucket="funnel_2"><h3>2. Tailored / Ready</h3><div class="kanban-items"></div></div>
  <div class="kanban-col" data-bucket="funnel_3"><h3>3. Applied</h3><div class="kanban-items"></div></div>
  <div class="kanban-col" data-bucket="funnel_4"><h3>4. OA / Interviewing</h3><div class="kanban-items"></div></div>
  <div class="kanban-col" data-bucket="funnel_5"><h3>5. Offer / Outcome</h3><div class="kanban-items"></div></div>
</div>

<script>
let minScore = 0;
let searchText = '';
let sponsorFilter = '';
let arrFilter = '';
let stageFilter = '';

function filterScore(min, evt) {{
  minScore = min;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  if (evt && evt.target) evt.target.classList.add('active');
  applyFilters();
}}

function filterSponsor(val) {{
  sponsorFilter = val;
  applyFilters();
}}

function filterArrangement(val) {{
  arrFilter = val;
  applyFilters();
}}

function filterStage(val) {{
  stageFilter = val;
  applyFilters();
}}

function filterText(text) {{
  searchText = text.toLowerCase();
  applyFilters();
}}

async function updateJobStage(url, crm_stage) {{
  try {{
    const res = await fetch('/api/jobs/stage', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{url, crm_stage}})
    }});
    if (res.ok) {{
      document.querySelectorAll(`.job-card[data-url="${{CSS.escape(url)}}"]`).forEach(card => {{
        card.dataset.stage = crm_stage;
      }});
      if (isKanban) renderKanban();
    }}
  }} catch (e) {{ console.error(e); }}
}}

async function saveJobNote(url, user_notes) {{
  try {{
    await fetch('/api/jobs/notes', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{url, user_notes}})
    }});
  }} catch (e) {{ console.error(e); }}
}}

async function generateProofPacket(url, btn) {{
  const orig = btn.textContent;
  btn.textContent = 'Building...';
  btn.disabled = true;
  try {{
    const res = await fetch('/api/jobs/proof', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{url}})
    }});
    if (res.ok) {{
      btn.textContent = '✓ Proof Saved';
    }} else {{
      btn.textContent = orig;
      btn.disabled = false;
    }}
  }} catch (e) {{
    console.error(e);
    btn.textContent = orig;
    btn.disabled = false;
  }}
}}

async function syncInbox(btn) {{
  const orig = btn.textContent;
  btn.textContent = 'Syncing...';
  btn.disabled = true;
  try {{
    await fetch('/api/sync-email', {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: '{{}}'}});
    window.location.reload();
  }} catch (e) {{
    btn.textContent = orig;
    btn.disabled = false;
  }}
}}

let isKanban = false;
function stageToBucket(st) {{
  if (st === 'tailored' || st === 'ready' || st === 'ready_to_apply') return 'funnel_2';
  if (st === 'applied') return 'funnel_3';
  if (st === 'assessment' || st === 'interviewing') return 'funnel_4';
  if (st === 'offer' || st === 'rejected' || st === 'archived') return 'funnel_5';
  return 'funnel_1';
}}

function renderKanban() {{
  document.querySelectorAll('#kanban-container .kanban-items').forEach(el => el.innerHTML = '');
  document.querySelectorAll('#grid-container .job-card:not(.hidden)').forEach(card => {{
    const clone = card.cloneNode(true);
    const bucket = stageToBucket(card.dataset.stage || 'discovered');
    const col = document.querySelector(`#kanban-container .kanban-col[data-bucket="${{bucket}}"] .kanban-items`);
    if (col) col.appendChild(clone);
  }});
}}

function toggleKanbanView() {{
  isKanban = !isKanban;
  const grid = document.getElementById('grid-container');
  const kanban = document.getElementById('kanban-container');
  const btn = document.getElementById('view-toggle-btn');
  if (isKanban) {{
    grid.classList.add('hidden');
    kanban.classList.remove('hidden');
    btn.textContent = '📊 Score Grid';
    btn.classList.add('active');
    renderKanban();
  }} else {{
    kanban.classList.add('hidden');
    grid.classList.remove('hidden');
    btn.textContent = '📋 Kanban Board';
    btn.classList.remove('active');
  }}
}}

async function generatePrepAndOutreach(url, btn) {{
  const orig = btn.textContent;
  btn.textContent = 'Generating...';
  btn.disabled = true;
  try {{
    await fetch('/api/jobs/prep', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{url}})
    }});
    await fetch('/api/jobs/outreach', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{url}})
    }});
    window.location.reload();
  }} catch (e) {{
    console.error(e);
    btn.textContent = orig;
    btn.disabled = false;
  }}
}}

function applyFilters() {{
  let shown = 0;
  let total = 0;
  document.querySelectorAll('.job-card').forEach(card => {{
    total++;
    const score = parseInt(card.dataset.score) || 0;
    const text = card.textContent.toLowerCase();
    const sp = card.dataset.sponsor || '';
    const arr = card.dataset.arrangement || '';
    const st = card.dataset.stage || '';
    const scoreMatch = minScore === 0 || score >= minScore;
    const textMatch = !searchText || text.includes(searchText);
    const spMatch = !sponsorFilter || (sp === 'licensed_sponsor' || sp === 'sponsorship_offered');
    const arrMatch = !arrFilter || arr === arrFilter;
    const stMatch = !stageFilter || st === stageFilter;
    if (scoreMatch && textMatch && spMatch && arrMatch && stMatch) {{
      card.classList.remove('hidden');
      shown++;
    }} else {{
      card.classList.add('hidden');
    }}
  }});
  document.getElementById('job-count').textContent = `Showing ${{shown}} of ${{total}} jobs`;
  document.querySelectorAll('.score-header').forEach(header => {{
    const grid = header.nextElementSibling;
    if (grid && grid.classList.contains('job-grid')) {{
      const visible = grid.querySelectorAll('.job-card:not(.hidden)').length;
      header.style.display = visible ? '' : 'none';
      grid.style.display = visible ? '' : 'none';
    }}
  }});
  if (typeof isKanban !== 'undefined' && isKanban) renderKanban();
}}

applyFilters();
</script>

</body>
</html>"""

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    abs_path = str(out.resolve())
    console.print(f"[green]Dashboard written to {abs_path}[/green]")
    return abs_path


def open_dashboard(output_path: str | None = None) -> None:
    """Generate the dashboard and open it in the default browser."""
    path = generate_dashboard(output_path)
    console.print("[dim]Opening in browser...[/dim]")
    webbrowser.open(f"file:///{path}")
