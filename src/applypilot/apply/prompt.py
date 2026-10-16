"""Prompt builder for the autonomous job application agent.

Constructs the full instruction prompt that tells Claude Code / the AI agent
how to fill out a job application form using Playwright MCP tools. All
personal data is loaded from the user's profile -- nothing is hardcoded.
Supports UK & European Union applications, multi-currency compensation,
visa sponsorship / relocation rules, and both Tech and ACA/Finance domains.
"""

from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from applypilot import config
from applypilot.apply import capsolver
from applypilot.scoring.validator import detect_profile_domain

logger = logging.getLogger(__name__)


def _format_bool_field(val: Any, default: str = "Yes") -> str:
    if isinstance(val, bool):
        return "Yes" if val else "No"
    return str(val) if val is not None and str(val).strip() else default


def _build_profile_summary(profile: dict[str, Any]) -> str:
    """Format the applicant profile section of the prompt."""
    p = profile
    personal = p.get("personal", {})
    work_auth = p.get("work_authorization", {})
    comp = p.get("compensation", {})
    exp = p.get("experience", {})
    avail = p.get("availability", {})
    eeo = p.get("eeo_voluntary", {})
    resume_facts = p.get("resume_facts", {})

    lines = [
        f"Name: {personal.get('full_name', '')}",
        f"Email: {personal.get('email', '')}",
        f"Phone: {personal.get('phone', '')}",
    ]

    addr_parts = [
        personal.get("address", ""),
        personal.get("city", ""),
        personal.get("province_state", ""),
        personal.get("country", ""),
        personal.get("postal_code", ""),
    ]
    lines.append(f"Address: {', '.join(part for part in addr_parts if part)}")

    if personal.get("linkedin_url"):
        lines.append(f"LinkedIn: {personal['linkedin_url']}")
    if personal.get("github_url"):
        lines.append(f"GitHub: {personal['github_url']}")
    if personal.get("portfolio_url"):
        lines.append(f"Portfolio: {personal['portfolio_url']}")
    if personal.get("website_url"):
        lines.append(f"Website: {personal['website_url']}")

    # Work authorization & relocation
    lines.append(f"Work Auth: {_format_bool_field(work_auth.get('legally_authorized_to_work'), 'See profile')}")
    lines.append(f"Sponsorship Needed: {_format_bool_field(work_auth.get('require_sponsorship'), 'See profile')}")
    lines.append(f"Willing to Relocate: {_format_bool_field(work_auth.get('willing_to_relocate'), 'Yes')}")
    if work_auth.get("work_permit_type"):
        lines.append(f"Work Permit / Visa Route: {work_auth['work_permit_type']}")
    if work_auth.get("target_regions"):
        lines.append(f"Target Regions: {', '.join(work_auth['target_regions'])}")

    # Compensation
    currency = comp.get("salary_currency", "GBP")
    symbol = {"GBP": "£", "EUR": "€", "USD": "$", "CAD": "$", "CHF": "CHF "}.get(currency.upper(), "")
    lines.append(f"Salary Expectation: {symbol}{comp.get('salary_expectation', '')} {currency}")
    if comp.get("currencies") and isinstance(comp["currencies"], dict):
        curr_parts = []
        for code, cinfo in comp["currencies"].items():
            if isinstance(cinfo, dict):
                sym = cinfo.get("symbol", "")
                curr_parts.append(f"{code}: {sym}{cinfo.get('expectation', '')} ({sym}{cinfo.get('min', '')}-{sym}{cinfo.get('max', '')})")
        if curr_parts:
            lines.append(f"Multi-Currency Expectations: {' | '.join(curr_parts)}")

    # Experience & Qualifications
    if exp.get("years_of_experience_total"):
        lines.append(f"Years Experience: {exp['years_of_experience_total']}")
    if exp.get("education_level"):
        lines.append(f"Education / Qualification: {exp['education_level']}")
    certs = resume_facts.get("certifications", [])
    if certs:
        lines.append(f"Professional Certifications: {', '.join(str(c) for c in certs)}")

    # Availability & Notice Period
    lines.append(f"Available / Start Date: {avail.get('earliest_start_date', 'Immediately')}")
    lines.append(f"Notice Period: {avail.get('notice_period', avail.get('earliest_start_date', 'Immediate / 1 month'))}")

    # Standard responses
    lines.extend([
        "Age 18+: Yes",
        "Background Check: Yes",
        "Felony / Criminal Record: No",
        "Previously Worked Here: No",
        "How Heard: Online Job Board",
    ])

    # EEO / UK & EU Diversity
    lines.append(f"Gender: {eeo.get('gender', 'Decline to self-identify')}")
    lines.append(f"Race / Ethnicity: {eeo.get('race_ethnicity', 'Decline to self-identify')}")
    lines.append(f"Veteran: {eeo.get('veteran_status', 'I am not a protected veteran')}")
    lines.append(f"Disability: {eeo.get('disability_status', 'I do not wish to answer')}")

    return "\n".join(lines)


def _build_location_check(profile: dict[str, Any], search_config: dict[str, Any]) -> str:
    """Build the location eligibility check section of the prompt.

    Supports UK & Pan-EU target regions as well as relocation-enabled applications.
    """
    personal = profile.get("personal", {})
    work_auth = profile.get("work_authorization", {})
    accept_patterns, reject_patterns = config.load_location_filters(search_config)

    target_regions = work_auth.get("target_regions", [])
    willing_to_relocate = _format_bool_field(work_auth.get("willing_to_relocate"), "Yes").lower() in ("yes", "true", "1")

    eligible_places = list(dict.fromkeys(target_regions + accept_patterns))
    if not eligible_places:
        primary_city = personal.get("city", "")
        primary_country = personal.get("country", "")
        eligible_places = [p for p in (primary_city, primary_country, "United Kingdom", "European Union", "Europe", "EMEA", "Remote") if p]

    city_list = ", ".join(eligible_places[:35])
    reject_note = ""
    if reject_patterns:
        reject_note = f"\n- Job explicitly requires: {', '.join(reject_patterns)} -> NOT ELIGIBLE. Output RESULT:FAILED:not_eligible_location"

    relocation_rule = (
        f'- "Hybrid" or "onsite" in ANY target country/city ({city_list}) -> ELIGIBLE (candidate is willing to relocate / work in UK & EU). Apply.'
        if willing_to_relocate
        else f'- "Hybrid" or "onsite" in {city_list} -> ELIGIBLE. Apply.'
    )

    return f"""== LOCATION CHECK (do this FIRST before any form) ==
Read the job page. Determine the work arrangement and geography. Then decide:
- "Remote", "work from anywhere", "UK Remote", "EU Remote", or "EMEA Remote" -> ELIGIBLE. Apply.
{relocation_rule}
- "Hybrid" or "onsite" in another city BUT the posting also says "remote OK", "relocation provided", or "visa sponsorship available" -> ELIGIBLE. Apply.{reject_note}
- "Onsite only" in a country outside ({city_list}) with NO remote or relocation option -> NOT ELIGIBLE. Stop immediately. Output RESULT:FAILED:not_eligible_location
- Cannot determine location -> Continue applying.
Do NOT abort applications located in the United Kingdom, Ireland, Netherlands, Germany, Luxembourg, Switzerland, or any European Union country — those ARE the target markets!"""


def _build_salary_section(profile: dict[str, Any]) -> str:
    """Build multi-currency salary negotiation instructions (GBP, EUR, CHF, USD)."""
    comp = profile.get("compensation", {})
    currency = comp.get("salary_currency", "GBP")
    floor = str(comp.get("salary_expectation", "65000"))
    range_min = str(comp.get("salary_range_min", floor))
    range_max = str(comp.get("salary_range_max", str(int(floor) + 20000) if floor.isdigit() else floor))
    conversion_note = comp.get("currency_conversion_note", "")
    currencies = comp.get("currencies", {})

    symbol = {"GBP": "£", "EUR": "€", "USD": "$", "CAD": "$", "CHF": "CHF "}.get(currency.upper(), "")

    try:
        floor_int = int(floor)
        examples = [
            (f"{symbol}{floor_int // 1000}K/yr", f"{symbol}{floor_int // 12:,}/mo", f"{symbol}{floor_int // 220}/day", f"{symbol}{floor_int // 2080}/hr"),
        ]
        rate_breakdown = f"({examples[0][0]} = {examples[0][1]} monthly gross = {examples[0][2]} daily rate = {examples[0][3]} hourly)"
    except (ValueError, TypeError):
        rate_breakdown = "(Monthly gross = Annual / 12; Daily rate = Annual / 220; Hourly rate = Annual / 2080)"

    multi_curr_lines = []
    if isinstance(currencies, dict) and currencies:
        for code, cinfo in currencies.items():
            if isinstance(cinfo, dict):
                sym = cinfo.get("symbol", "")
                multi_curr_lines.append(
                    f"  - {code}: target {sym}{cinfo.get('expectation', floor)} (range {sym}{cinfo.get('min', range_min)}-{sym}{cinfo.get('max', range_max)})"
                )
    multi_curr_block = "\n".join(multi_curr_lines)

    if conversion_note:
        convert_line = f"Posting/form uses a different currency? -> {conversion_note}"
    else:
        convert_line = "Posting/form uses GBP (£), EUR (€), CHF, or USD ($)? -> Match the local currency of the job location."

    return f"""== SALARY & COMPENSATION (multi-currency UK/EU aware) ==
Base target: {symbol}{floor} {currency} (range {symbol}{range_min}-{symbol}{range_max} {currency}).
{f"Currency-specific targets:{chr(10)}{multi_curr_block}" if multi_curr_block else ""}

Decision tree:
1. Job posting shows a salary range (e.g. "£65,000-£85,000" or "€70,000-€90,000")? -> Answer with the MIDPOINT in that currency.
2. {convert_line}
3. Form asks for ANNUAL GROSS salary? -> Enter the annual figure (digits only if numeric field, e.g. {floor}).
4. Form asks for MONTHLY GROSS salary (common in Germany, Netherlands, Austria, France, Poland)? -> Divide annual figure by 12. {rate_breakdown}
5. Form asks for DAILY RATE (common for UK contracts)? -> Divide annual figure by 220.
6. Form asks for HOURLY RATE? -> Divide annual figure by 2080.
7. Asked for a range? -> Give "{symbol}{range_min}-{symbol}{range_max} {currency}" (or local currency equivalent)."""


def _build_screening_section(profile: dict[str, Any]) -> str:
    """Build the screening questions guidance section."""
    personal = profile.get("personal", {})
    exp = profile.get("experience", {})
    avail = profile.get("availability", {})
    work_auth = profile.get("work_authorization", {})
    resume_facts = profile.get("resume_facts", {})
    domain = detect_profile_domain(profile)

    city = personal.get("city", "their city")
    country = personal.get("country", "")
    loc_display = f"{city}, {country}".strip(", ")
    years = exp.get("years_of_experience_total", "multiple")
    target_role = exp.get("target_role") or exp.get("current_job_title") or exp.get("current_title") or "professional"
    willing_relocate = _format_bool_field(work_auth.get("willing_to_relocate"), "Yes")
    require_sponsorship = _format_bool_field(work_auth.get("require_sponsorship"), "No")
    permit_type = work_auth.get("work_permit_type", "")
    notice_period = avail.get("notice_period", avail.get("earliest_start_date", "Immediate / 1 month"))
    certs = ", ".join(str(c) for c in resume_facts.get("certifications", []))

    if domain == "finance_accounting":
        domain_confidence = (
            f"Skills, accounting standards, and tools -> be confident. This candidate is a qualified {target_role} "
            f"({certs or exp.get('education_level', 'ACA')}) with {years} years experience. "
            "If the question asks 'Do you have experience with [IFRS / UK GAAP / FRS 102 / ISA / SOX / SAP / NetSuite / FP&A]?' "
            "and it is within accounting, audit, financial reporting, or corporate finance, answer YES."
        )
    else:
        domain_confidence = (
            f"Skills and tools -> be confident. This candidate is a {target_role} with {years} years experience. "
            "If the question asks 'Do you have experience with [tool]?' and it's in the same domain "
            "(AI, backend, full-stack, FP&A systems, cloud, automation, data), answer YES. Engineers learn tools fast. Don't sell short."
        )

    return f"""== SCREENING QUESTIONS (be strategic) ==
Hard facts -> answer truthfully from the profile. No guessing. This includes:
  - Current location & relocation: based in {loc_display}; willing to relocate to UK/EU: {willing_relocate}
  - Work authorization: {_format_bool_field(work_auth.get('legally_authorized_to_work'), 'see profile')}
  - Visa sponsorship required: {require_sponsorship}{f' ({permit_type})' if permit_type else ''}
  - Notice period / earliest start date: {notice_period}
  - Professional qualifications / certifications: {certs or exp.get('education_level', 'see profile')}
  - Criminal/background check: No criminal record, happy to complete background check

{domain_confidence}

Open-ended questions ("Why do you want this role?", "Tell us about yourself", "What interests you?") -> Write 2-3 sentences. Be specific to THIS job. Reference something from the job description. Connect it to a real achievement from the resume. No generic fluff. No "I am passionate about..." -- sound like a real professional.

UK/EU GDPR & Privacy Consent checkboxes ("I consent to the processing of my personal data for recruitment purposes") -> ALWAYS check/accept (required by EU/UK law to submit an application).
EEO/diversity monitoring -> "Decline to self-identify" or "Prefer not to say" for everything."""


def _build_hard_rules(profile: dict[str, Any]) -> str:
    """Build the hard rules section with work auth and name from profile."""
    personal = profile.get("personal", {})
    work_auth = profile.get("work_authorization", {})

    # `full_name` is user-supplied JSON: it may be missing, null, or whitespace.
    # `dict.get(k, default)` returns None when the key exists with a null value, so
    # the `or ""` fallback and the strip are both required before splitting.
    full_name = str(personal.get("full_name") or "").strip() or "Candidate"
    clean_parts = full_name.split(",")[0].strip().split()
    first_token = clean_parts[0] if clean_parts else full_name
    preferred_name = str(personal.get("preferred_name") or "").strip() or first_token
    preferred_last = clean_parts[-1] if len(clean_parts) > 1 else ""
    display_name = f"{preferred_name} {preferred_last}".strip() if preferred_last else preferred_name

    sponsorship = _format_bool_field(work_auth.get("require_sponsorship"), "No")
    permit_type = work_auth.get("work_permit_type", "")

    work_auth_rule = f"Work auth & sponsorship: Sponsorship needed = {sponsorship}."
    if permit_type:
        work_auth_rule = f"Work auth: {permit_type}. Sponsorship needed: {sponsorship}."

    name_rule = f"Name: Legal name = {full_name}."
    if preferred_name and preferred_name != first_token:
        name_rule += f' Preferred name = {preferred_name}. Use "{display_name}" unless a field specifically says "legal name".'

    return f"""== HARD RULES (never break these) ==
1. Never lie about: citizenship, work authorization, criminal history, education credentials, security clearance, licenses.
2. {work_auth_rule}
3. {name_rule}"""


def _build_captcha_section() -> str:
    """Build the CAPTCHA detection and solving instructions.

    The CapSolver API key is never interpolated into the prompt: solving runs in
    the server-side ``capsolver`` MCP server, which returns only the solved token
    (docs/01_AUDIT_AND_GAP_ANALYSIS.md section 2.3.2). Every literal brace in the
    page JavaScript below is doubled because this whole section is one f-string.
    """
    config.load_env()
    capsolver_status = (
        "configured" if capsolver.is_configured()
        else "NOT CONFIGURED — skip to MANUAL FALLBACK for all CAPTCHAs"
    )

    return f"""== CAPTCHA ==
You solve CAPTCHAs through the `capsolver` MCP tools, which run server-side inside ApplyPilot.
The CapSolver API key is NEVER placed in page JavaScript and NEVER sent to api.capsolver.com
from browser_evaluate -- the employer's page could read it. The MCP tools hold the credential
and return only the solved token.
API base: https://api.capsolver.com
Status: {capsolver_status}

CRITICAL RULE: When ANY CAPTCHA appears (hCaptcha, reCAPTCHA, Turnstile -- regardless of what it looks like visually), you MUST:
1. Run CAPTCHA DETECT to get the type and sitekey
2. Run CAPTCHA SOLVE through the capsolver MCP tools, then inject the token
3. ONLY go to MANUAL FALLBACK if the capsolver tool reports an error
Do NOT skip the tool call based on what the CAPTCHA looks like. CapSolver solves CAPTCHAs server-side -- it does NOT need to see or interact with images, puzzles, or games. Even "drag the pipe" or "click all traffic lights" hCaptchas are solved via API token, not visually. ALWAYS try the tool first.

--- CAPTCHA DETECT ---
Run this browser_evaluate after every navigation, Apply/Submit/Login click, or when a page feels stuck.
IMPORTANT: Detection order matters. hCaptcha elements also have data-sitekey, so check hCaptcha BEFORE reCAPTCHA.

browser_evaluate function: () => {{
  const r = {{}};
  const url = window.location.href;
  // 1. hCaptcha (check FIRST -- hCaptcha uses data-sitekey too)
  const hc = document.querySelector('.h-captcha, [data-hcaptcha-sitekey]');
  if (hc) {{
    r.type = 'hcaptcha'; r.sitekey = hc.dataset.sitekey || hc.dataset.hcaptchaSitekey;
  }}
  if (!r.type && document.querySelector('script[src*="hcaptcha.com"], iframe[src*="hcaptcha.com"]')) {{
    const el = document.querySelector('[data-sitekey]');
    if (el) {{ r.type = 'hcaptcha'; r.sitekey = el.dataset.sitekey; }}
  }}
  // 2. Cloudflare Turnstile
  if (!r.type) {{
    const cf = document.querySelector('.cf-turnstile, [data-turnstile-sitekey]');
    if (cf) {{
      r.type = 'turnstile'; r.sitekey = cf.dataset.sitekey || cf.dataset.turnstileSitekey;
      if (cf.dataset.action) r.action = cf.dataset.action;
      if (cf.dataset.cdata) r.cdata = cf.dataset.cdata;
    }}
  }}
  if (!r.type && document.querySelector('script[src*="challenges.cloudflare.com"]')) {{
    r.type = 'turnstile_script_only'; r.note = 'Wait 3s and re-detect.';
  }}
  // 3. reCAPTCHA v3 (invisible, loaded via render= param)
  if (!r.type) {{
    const s = document.querySelector('script[src*="recaptcha"][src*="render="]');
    if (s) {{
      const m = s.src.match(/render=([^&]+)/);
      if (m && m[1] !== 'explicit') {{ r.type = 'recaptchav3'; r.sitekey = m[1]; }}
    }}
  }}
  // 4. reCAPTCHA v2 (checkbox or invisible)
  if (!r.type) {{
    const rc = document.querySelector('.g-recaptcha');
    if (rc) {{ r.type = 'recaptchav2'; r.sitekey = rc.dataset.sitekey; }}
  }}
  if (!r.type && document.querySelector('script[src*="recaptcha"]')) {{
    const el = document.querySelector('[data-sitekey]');
    if (el) {{ r.type = 'recaptchav2'; r.sitekey = el.dataset.sitekey; }}
  }}
  // 5. FunCaptcha (Arkose Labs)
  if (!r.type) {{
    const fc = document.querySelector('#FunCaptcha, [data-pkey], .funcaptcha');
    if (fc) {{ r.type = 'funcaptcha'; r.sitekey = fc.dataset.pkey; }}
  }}
  if (!r.type && document.querySelector('script[src*="arkoselabs"], script[src*="funcaptcha"]')) {{
    const el = document.querySelector('[data-pkey]');
    if (el) {{ r.type = 'funcaptcha'; r.sitekey = el.dataset.pkey; }}
  }}
  if (r.type) {{ r.url = url; return r; }}
  return null;
}}

Result actions:
- null -> no CAPTCHA. Continue normally.
- "turnstile_script_only" -> browser_wait_for time: 3, re-run detect.
- Any other type -> proceed to CAPTCHA SOLVE below.

--- CAPTCHA SOLVE ---
Two steps: solve through the capsolver MCP tool, then inject the returned token.

STEP 1 -- SOLVE
Call the `mcp__capsolver__solve_captcha` MCP tool with:
  captcha_type: the type reported by DETECT (hcaptcha / recaptchav2 / recaptchav3 / turnstile / funcaptcha)
  website_url:  the url from DETECT
  website_key:  the sitekey from DETECT
  extra:        optional CapSolver task extras, e.g. {{"pageAction": "submit"}} for reCAPTCHA v3,
                or a metadata object such as {{"action": "...", "cdata": "..."}} for Turnstile
The tool creates the task, polls until solved, and returns
{{"task_id": "...", "token": "...", "status": "ready"}}.

Advanced (manual polling): call `mcp__capsolver__create_task`, then
`mcp__capsolver__get_task_result` with the returned task_id, waiting 3s between polls
(max 10 polls). Extract the token: reCAPTCHA/hCaptcha -> solution.gRecaptchaResponse,
Turnstile -> solution.token.

If the tool reports an error, or CapSolver is NOT CONFIGURED -> MANUAL FALLBACK.

STEP 2 -- INJECT TOKEN (replace THE_TOKEN with actual token string):

For reCAPTCHA v2/v3:
browser_evaluate function: () => {{
  const token = 'THE_TOKEN';
  document.querySelectorAll('[name="g-recaptcha-response"]').forEach(el => {{ el.value = token; el.style.display = 'block'; }});
  if (window.___grecaptcha_cfg) {{
    const clients = window.___grecaptcha_cfg.clients;
    for (const key in clients) {{
      const walk = (obj, d) => {{
        if (d > 4 || !obj) return;
        for (const k in obj) {{
          if (typeof obj[k] === 'function' && k.length < 3) try {{ obj[k](token); }} catch(e) {{}}
          else if (typeof obj[k] === 'object') walk(obj[k], d+1);
        }}
      }};
      walk(clients[key], 0);
    }}
  }}
  return 'injected';
}}

For hCaptcha:
browser_evaluate function: () => {{
  const token = 'THE_TOKEN';
  const ta = document.querySelector('[name="h-captcha-response"], textarea[name*="hcaptcha"]');
  if (ta) ta.value = token;
  document.querySelectorAll('iframe[data-hcaptcha-response]').forEach(f => f.setAttribute('data-hcaptcha-response', token));
  const cb = document.querySelector('[data-hcaptcha-widget-id]');
  if (cb && window.hcaptcha) try {{ window.hcaptcha.getResponse(cb.dataset.hcaptchaWidgetId); }} catch(e) {{}}
  return 'injected';
}}

For Turnstile:
browser_evaluate function: () => {{
  const token = 'THE_TOKEN';
  const inp = document.querySelector('[name="cf-turnstile-response"], input[name*="turnstile"]');
  if (inp) inp.value = token;
  if (window.turnstile) try {{ const w = document.querySelector('.cf-turnstile'); if (w) window.turnstile.getResponse(w); }} catch(e) {{}}
  return 'injected';
}}

For FunCaptcha:
browser_evaluate function: () => {{
  const token = 'THE_TOKEN';
  const inp = document.querySelector('#FunCaptcha-Token, input[name="fc-token"]');
  if (inp) inp.value = token;
  if (window.ArkoseEnforcement) try {{ window.ArkoseEnforcement.setConfig({{data: {{blob: token}}}}) }} catch(e) {{}}
  return 'injected';
}}

After injecting: browser_wait_for time: 2, then snapshot.
- Widget gone or green check -> success. Click Submit if needed.
- No change -> click Submit/Verify/Continue button (some sites need it).
- Still stuck -> token may have expired (~2 min lifetime). Re-run from STEP 1.

--- MANUAL FALLBACK ---
You should ONLY be here if CapSolver createTask returned errorId > 0. If you haven't tried CapSolver yet, GO BACK and try it first.
If CapSolver genuinely failed (errorId > 0):
1. Audio challenge: Look for "audio" or "accessibility" button -> click it for an easier challenge.
2. Text/logic puzzles: Solve them yourself. Think step by step. Common tricks: "All but 9 die" = 9 left. "3 sisters and 4 brothers, how many siblings?" = 7.
3. Simple text captchas ("What is 3+7?", "Type the word") -> solve them.
4. All else fails -> Output RESULT:CAPTCHA."""


def build_prompt(
    job: dict[str, Any],
    tailored_resume: str,
    cover_letter: str | None = None,
    dry_run: bool = False,
    worker_id: int = 0,
    redact_secrets: bool = False,
) -> str:
    """Build the full instruction prompt for the apply agent."""
    profile = config.load_profile()
    search_config = config.load_search_config()
    personal = profile["personal"]

    # --- Resolve resume PDF path ---
    resume_path = job.get("tailored_resume_path")
    if not resume_path:
        raise ValueError(f"No tailored resume for job: {job.get('title', 'unknown')}")

    src_pdf = Path(resume_path).with_suffix(".pdf").resolve()
    if not src_pdf.exists():
        raise ValueError(f"Resume PDF not found: {src_pdf}")

    # Copy to a clean, per-worker isolated directory for upload
    full_name = str(personal.get("full_name") or "").strip() or "Candidate"
    name_slug = re.sub(r"[^\w\s-]", "", full_name.split(",")[0]).strip().replace(" ", "_") or "Candidate"
    dest_dir = config.APPLY_WORKER_DIR / f"worker-{worker_id}" / "uploads"
    dest_dir.mkdir(parents=True, exist_ok=True)
    upload_pdf = dest_dir / f"{name_slug}_Resume.pdf"
    shutil.copy(str(src_pdf), str(upload_pdf))
    pdf_path = str(upload_pdf)

    # --- Cover letter handling ---
    cover_letter_text = cover_letter or ""
    cl_upload_path = ""
    cl_path = job.get("cover_letter_path")
    if cl_path and Path(cl_path).exists():
        cl_src = Path(cl_path)
        cl_txt = cl_src.with_suffix(".txt")
        if cl_txt.exists():
            cover_letter_text = cl_txt.read_text(encoding="utf-8")
        elif cl_src.suffix == ".txt":
            cover_letter_text = cl_src.read_text(encoding="utf-8")
        cl_pdf_src = cl_src.with_suffix(".pdf")
        if cl_pdf_src.exists():
            cl_upload = dest_dir / f"{name_slug}_Cover_Letter.pdf"
            shutil.copy(str(cl_pdf_src), str(cl_upload))
            cl_upload_path = str(cl_upload)

    # --- Build all prompt sections ---
    profile_summary = _build_profile_summary(profile)
    location_check = _build_location_check(profile, search_config)
    salary_section = _build_salary_section(profile)
    screening_section = _build_screening_section(profile)
    hard_rules = _build_hard_rules(profile)
    captcha_section = _build_captcha_section()

    from applypilot.apply.memory import build_memory_prompt_section
    memory_section = build_memory_prompt_section(profile=profile)

    city = personal.get("city", "the area")
    if not cover_letter_text:
        cl_display = (
            f"None available. Skip if optional. If required, write 2 factual "
            f"sentences: (1) relevant experience from the resume that matches "
            f"this role, (2) available immediately and based in {city}."
        )
    else:
        cl_display = cover_letter_text

    phone_digits = "".join(c for c in personal.get("phone", "") if c.isdigit())

    from applypilot.config import load_blocked_sso
    blocked_sso = load_blocked_sso()

    clean_name_parts = full_name.split(",")[0].strip().split()
    preferred_name = str(personal.get("preferred_name") or "").strip() or (
        clean_name_parts[0] if clean_name_parts else full_name
    )
    last_name = clean_name_parts[-1] if len(clean_name_parts) > 1 else ""
    display_name = f"{preferred_name} {last_name}".strip()

    password_display = "<REDACTED_PASSWORD>" if redact_secrets else personal.get("password", "")
    company_display = job.get("company") or job.get("site", "Unknown")

    if dry_run:
        submit_instruction = "IMPORTANT: Do NOT click the final Submit/Apply button. Review the form, verify all fields, then output RESULT:APPLIED with a note that this was a dry run."
    else:
        submit_instruction = "BEFORE clicking Submit/Apply, take a snapshot and review EVERY field on the page. Verify all data matches the APPLICANT PROFILE and TAILORED RESUME -- name, email, phone, location, work auth, resume uploaded, cover letter if applicable. If anything is wrong or missing, fix it FIRST. Only click Submit after confirming everything is correct."

    prompt = f"""You are an autonomous job application agent. Your ONE mission: get this candidate an interview. You have all the information and tools. Think strategically. Act decisively. Submit the application.

== JOB ==
URL: {job.get('application_url') or job['url']}
Title: {job['title']}
Company: {company_display}
Fit Score: {job.get('fit_score', 'N/A')}/10

== FILES ==
Resume PDF (upload this): {pdf_path}
Cover Letter PDF (upload if asked): {cl_upload_path or "N/A"}

== RESUME TEXT (use when filling text fields) ==
{tailored_resume}

== COVER LETTER TEXT (paste if text field, upload PDF if file field) ==
{cl_display}

== APPLICANT PROFILE ==
{profile_summary}

== YOUR MISSION ==
Submit a complete, accurate application. Use the profile and resume as source data -- adapt to fit each form's format.

If something unexpected happens and these instructions don't cover it, figure it out yourself. You are autonomous. Navigate pages, read content, try buttons, explore the site. The goal is always the same: submit the application. Do whatever it takes to reach that goal.

{hard_rules}

== NEVER DO THESE (immediate RESULT:FAILED if encountered) ==
- NEVER grant camera, microphone, screen sharing, or location permissions. If a site requests them -> RESULT:FAILED:unsafe_permissions
- NEVER do video/audio verification, selfie capture, ID photo upload, or biometric anything -> RESULT:FAILED:unsafe_verification
- NEVER set up a freelancing profile (Mercor, Toptal, Upwork, Fiverr, Turing, etc.). These are contractor marketplaces, not job applications -> RESULT:FAILED:not_a_job_application
- NEVER agree to hourly/contract rates, availability calendars, or "set your rate" flows unless applying for a standard UK/EU contract role.
- NEVER install browser extensions, download executables, or run assessment software.
- NEVER enter payment info, bank details, or SSN/SIN/NI numbers.
- NEVER click "Allow" on any browser permission popup. Always deny/block.
- If the site is NOT a job application form (it's a profile builder, skills marketplace, talent network signup, coding assessment platform) -> RESULT:FAILED:not_a_job_application

{location_check}

{salary_section}

{screening_section}

{memory_section}

== STEP-BY-STEP ==
1. browser_navigate to the job URL.
2. browser_snapshot to read the page. Then run CAPTCHA DETECT (see CAPTCHA section). If a CAPTCHA is found, solve it before continuing.
3. LOCATION CHECK. Read the page for location info. If not eligible, output RESULT and stop.
4. Find and click the Apply button. If email-only (page says "email resume to X"):
   - send_email with subject "Application for {job['title']} -- {display_name}", body = 2-3 sentence pitch + contact info, attach resume PDF: ["{pdf_path}"]
   - Output RESULT:APPLIED. Done.
   After clicking Apply: browser_snapshot. Run CAPTCHA DETECT -- many sites trigger CAPTCHAs right after the Apply click. If found, solve before continuing.
5. Login wall?
   5a. FIRST: check the URL. If you landed on {', '.join(blocked_sso)}, or any SSO/OAuth page -> STOP. Output RESULT:FAILED:sso_required. Do NOT try to sign in to Google/Microsoft/SSO.
   5b. Check for popups. Run browser_tabs action "list". If a new tab/window appeared (login popup), switch to it with browser_tabs action "select". Check the URL there too -- if it's SSO -> RESULT:FAILED:sso_required.
   5c. Regular login form (employer's own site)? Try sign in: {personal.get('email', '')} / {password_display}
   5d. After clicking Login/Sign-in: run CAPTCHA DETECT. Login pages frequently have invisible CAPTCHAs that silently block form submissions. If found, solve it then retry login.
   5e. Sign in failed? Try sign up with same email and password.
   5f. Need email verification? Use search_emails + read_email to get the code.
   5g. After login, run browser_tabs action "list" again. Switch back to the application tab if needed.
   5h. All failed? Output RESULT:FAILED:login_issue. Do not loop.
6. Upload resume. ALWAYS upload fresh -- delete any existing resume first, then browser_file_upload with the PDF path above. This is the tailored resume for THIS job. Non-negotiable.
7. Upload cover letter if there's a field for it. Text field -> paste the cover letter text. File upload -> use the cover letter PDF path.
8. Check ALL pre-filled fields. ATS systems parse your resume and auto-fill -- it's often WRONG.
   - "Current Job Title" or "Most Recent Title" -> use the title from the TAILORED RESUME summary, NOT whatever the parser guessed.
   - Compare every other field to the APPLICANT PROFILE. Fix mismatches. Fill empty fields.
9. Answer screening questions using the rules above.
10. {submit_instruction}
11. After submit: browser_snapshot. Run CAPTCHA DETECT -- submit buttons often trigger invisible CAPTCHAs. If found, solve it (the form will auto-submit once the token clears, or you may need to click Submit again). Then check for new tabs (browser_tabs action: "list"). Switch to newest, close old. Snapshot to confirm submission. Look for "thank you" or "application received".
12. Output your result.

== RESULT CODES (output EXACTLY one) ==
RESULT:APPLIED -- submitted successfully
RESULT:EXPIRED -- job closed or no longer accepting applications
RESULT:CAPTCHA -- blocked by unsolvable captcha
RESULT:LOGIN_ISSUE -- could not sign in or create account
RESULT:FAILED:not_eligible_location -- onsite outside acceptable area, no remote option
RESULT:FAILED:not_eligible_work_auth -- requires unauthorized work location
RESULT:FAILED:reason -- any other failure (brief reason)

== BROWSER EFFICIENCY ==
- browser_snapshot ONCE per page to understand it. Then use browser_take_screenshot to check results (10x less memory).
- Only snapshot again when you need element refs to click/fill.
- Multi-page forms (Workday, Taleo, iCIMS, Personio, SmartRecruiters): snapshot each new page, fill all fields, click Next/Continue. Repeat until final review page.
- Fill ALL fields in ONE browser_fill_form call. Not one at a time.
- Keep your thinking SHORT. Don't repeat page structure back.
- CAPTCHA AWARENESS: After any navigation, Apply/Submit/Login click, or when a page feels stuck -- run CAPTCHA DETECT (see CAPTCHA section). Invisible CAPTCHAs (Turnstile, reCAPTCHA v3) show NO visual widget but block form submissions silently. The detect script finds them even when invisible.

== FORM TRICKS ==
- Popup/new window opened? browser_tabs action "list" to see all tabs. browser_tabs action "select" with the tab index to switch. ALWAYS check for new tabs after clicking login/apply/sign-in buttons.
- "Upload your resume" pre-fill page (Workday, Lever, Personio, etc.): This is NOT the application form yet. Click "Select file" or the upload area, then browser_file_upload with the resume PDF path. Wait for parsing to finish. Then click Next/Continue to reach the actual form.
- File upload not working? Try: (1) browser_click the upload button/area, (2) browser_file_upload with the path. If still failing, look for a hidden file input or a "Select file" link and click that first.
- Dropdown won't fill? browser_click to open it, then browser_click the option.
- Checkbox won't check via fill_form? Use browser_click on it instead. Snapshot to verify.
- Phone field with country prefix: just type digits {phone_digits}
- Date fields (UK/EU DD/MM/YYYY format): {datetime.now().strftime('%d/%m/%Y')} (or ISO {datetime.now().strftime('%Y-%m-%d')} if requested)
- Validation errors after submit? Take BOTH snapshot AND screenshot. Snapshot shows text errors, screenshot shows red-highlighted fields. Fix all, retry.
- Honeypot fields (hidden, "leave blank"): skip them.
- Format-sensitive fields: read the placeholder text, match it exactly.

{captcha_section}

== WHEN TO GIVE UP ==
- Same page after 3 attempts with no progress -> RESULT:FAILED:stuck
- Job is closed/expired/page says "no longer accepting" -> RESULT:EXPIRED
- Page is broken/500 error/blank -> RESULT:FAILED:page_error
Stop immediately. Output your RESULT code. Do not loop."""

    return prompt
