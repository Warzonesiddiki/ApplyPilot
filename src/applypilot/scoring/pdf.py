"""Text-to-PDF conversion for tailored resumes and cover letters.

Parses the structured text resume format (or plain-text cover letter format),
renders via an HTML/CSS template with full HTML entity escaping, and exports
to PDF using headless Chromium via Playwright.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from html import escape
from pathlib import Path

from applypilot import config
from applypilot.database import get_connection

log = logging.getLogger(__name__)


# ── Resume Parser ────────────────────────────────────────────────────────

def parse_resume(text: str) -> dict:
    """Parse a structured text resume into sections.

    Expects a format with header lines (name, title, location, contact)
    followed by ALL-CAPS section headers (SUMMARY, TECHNICAL SKILLS, etc.).

    Args:
        text: Full resume text.

    Returns:
        {"name": str, "title": str, "location": str, "contact": str, "sections": dict}
    """
    lines = [line.rstrip() for line in text.strip().split("\n")]

    # Header: first few lines before SUMMARY
    header_lines: list[str] = []
    body_start = 0
    for i, line in enumerate(lines):
        if line.strip().upper() == "SUMMARY":
            body_start = i
            break
        if line.strip():
            header_lines.append(line.strip())

    name = header_lines[0] if len(header_lines) > 0 else ""
    title = header_lines[1] if len(header_lines) > 1 else ""
    location = ""
    contact = ""
    if len(header_lines) > 3:
        location = header_lines[2]
        contact = header_lines[3]
    elif len(header_lines) > 2:
        if "@" in header_lines[2] or "|" in header_lines[2]:
            contact = header_lines[2]
        else:
            location = header_lines[2]

    # Split body into sections by ALL-CAPS headers
    sections: dict[str, str] = {}
    current_section: str | None = None
    current_lines: list[str] = []

    for line in lines[body_start:]:
        stripped = line.strip()
        if (
            stripped
            and stripped == stripped.upper()
            and not stripped.startswith("-")
            and len(stripped) > 3
            and not stripped.startswith("\u2022")
        ):
            if current_section:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = stripped
            current_lines = []
        else:
            current_lines.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_lines).strip()

    return {
        "name": name,
        "title": title,
        "location": location,
        "contact": contact,
        "sections": sections,
    }


def parse_skills(text: str) -> list[tuple[str, str]]:
    """Parse skills section into (category, value) pairs."""
    skills: list[tuple[str, str]] = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if ":" in line:
            cat, val = line.split(":", 1)
            skills.append((cat.strip(), val.strip()))
    return skills


def parse_entries(text: str) -> list[dict]:
    """Parse experience/project entries from section text."""
    entries: list[dict] = []
    lines = text.strip().split("\n")
    current: dict | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- ") or stripped.startswith("\u2022 "):
            if current:
                current["bullets"].append(stripped[2:].strip())
        elif current is None or (
            not stripped.startswith("-")
            and not stripped.startswith("\u2022")
            and len(current.get("bullets", [])) > 0
        ):
            if current:
                entries.append(current)
            current = {"title": stripped, "subtitle": "", "bullets": []}
        elif current and not current["subtitle"]:
            current["subtitle"] = stripped
        else:
            if current:
                current["bullets"].append(stripped)

    if current:
        entries.append(current)

    return entries


# ── HTML Templates (Resume & Cover Letter) ───────────────────────────────

def build_html(resume: dict, theme: str = "modern") -> str:
    """Build professional resume HTML from parsed data with full HTML escaping and theme support.

    Supported themes:
      - 'modern': Crisp Calibri/Segoe UI layout (default).
      - 'executive_uk_eu': A4 executive typography tailored for UK/EU Chartered Accountancy (ACA/ACCA) & Senior Finance.
      - 'compact_ats': Dense single-page ATS layout.
    """
    sections = resume["sections"]

    # Skills
    skills_html = ""
    for sk_header in ("TECHNICAL SKILLS", "CORE COMPETENCIES & TECHNICAL SKILLS", "CORE COMPETENCIES", "SKILLS"):
        if sk_header in sections and sections[sk_header].strip():
            skills = parse_skills(sections[sk_header])
            rows = ""
            for cat, val in skills:
                rows += f'<div class="skill-row"><span class="skill-cat">{escape(cat)}:</span> {escape(val)}</div>\n'
            sec_title = "Core Competencies &amp; Technical Skills" if "COMPETENCIES" in sk_header else "Technical Skills"
            skills_html = f'<div class="section"><div class="section-title">{sec_title}</div>{rows}</div>'
            break

    # Experience
    exp_html = ""
    if "EXPERIENCE" in sections:
        entries = parse_entries(sections["EXPERIENCE"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{escape(b)}</li>" for b in e["bullets"])
            subtitle = f'<div class="entry-subtitle">{escape(e["subtitle"])}</div>' if e["subtitle"] else ""
            items += f'<div class="entry"><div class="entry-title">{escape(e["title"])}</div>{subtitle}<ul>{bullets}</ul></div>'
        exp_html = f'<div class="section"><div class="section-title">Experience</div>{items}</div>'

    # Projects
    proj_html = ""
    if "PROJECTS" in sections:
        entries = parse_entries(sections["PROJECTS"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{escape(b)}</li>" for b in e["bullets"])
            subtitle = f'<div class="entry-subtitle">{escape(e["subtitle"])}</div>' if e["subtitle"] else ""
            items += f'<div class="entry"><div class="entry-title">{escape(e["title"])}</div>{subtitle}<ul>{bullets}</ul></div>'
        proj_html = f'<div class="section"><div class="section-title">Projects</div>{items}</div>'

    # Certifications / Qualifications
    cert_html = ""
    for cert_header in ("CERTIFICATIONS", "QUALIFICATIONS", "QUALIFICATIONS & CERTIFICATIONS"):
        if cert_header in sections and sections[cert_header].strip():
            cert_text = escape(sections[cert_header].strip())
            cert_html = f'<div class="section"><div class="section-title">Qualifications &amp; Certifications</div><div class="edu">{cert_text}</div></div>'
            break

    # Education
    edu_html = ""
    if "EDUCATION" in sections:
        edu_text = escape(sections["EDUCATION"].strip())
        edu_html = f'<div class="section"><div class="section-title">Education</div><div class="edu">{edu_text}</div></div>'

    # Summary
    summary_html = ""
    if "SUMMARY" in sections:
        summary_html = f'<div class="section"><div class="section-title">Summary</div><div class="summary">{escape(sections["SUMMARY"].strip())}</div></div>'

    # Contact line parsing
    contact = resume["contact"]
    contact_parts = [escape(p.strip()) for p in contact.split("|")] if contact else []
    contact_html = " &nbsp;|&nbsp; ".join(contact_parts)

    location_html = f'<div class="location">{escape(resume["location"])}</div>' if resume["location"] else ""

    clean_theme = (theme or "modern").strip().lower()
    if clean_theme == "executive_uk_eu":
        page_size = "A4"
        page_margin = "12mm 15mm"
        font_family = "'Georgia', 'Cambria', 'Times New Roman', serif"
        base_size = "10pt"
        line_height = "1.38"
        accent_color = "#0f172a"
        border_color = "#1e3a8a"
    elif clean_theme == "compact_ats":
        page_size = "letter"
        page_margin = "0.28in 0.4in"
        font_family = "'Calibri', 'Segoe UI', Arial, sans-serif"
        base_size = "8.6pt"
        line_height = "1.2"
        accent_color = "#1a3a5c"
        border_color = "#2a7ab5"
    else:
        page_size = "letter"
        page_margin = "0.35in 0.5in"
        font_family = "'Calibri', 'Segoe UI', Arial, sans-serif"
        base_size = "10pt"
        line_height = "1.35"
        accent_color = "#1a3a5c"
        border_color = "#2a7ab5"

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{
    size: {page_size};
    margin: {page_margin};
}}
* {{
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}}
body {{
    font-family: {font_family};
    font-size: {base_size};
    line-height: {line_height};
    color: #1a1a1a;
}}
.header {{
    text-align: center;
    margin-bottom: 4px;
    padding-bottom: 4px;
    border-bottom: 1.5px solid {border_color};
}}
.name {{
    font-size: 18pt;
    font-weight: 700;
    color: {accent_color};
    letter-spacing: 0.5px;
}}
.title {{
    font-size: 10.5pt;
    color: #3a6b8c;
    margin: 1px 0;
}}
.location {{
    font-size: 9pt;
    color: #555;
}}
.contact {{
    font-size: 9pt;
    color: #444;
    margin-top: 1px;
}}
.contact a {{
    color: #2c3e50;
    text-decoration: none;
}}
.section {{
    margin-top: 5px;
}}
.section-title {{
    font-size: 10pt;
    font-weight: 700;
    color: {accent_color};
    text-transform: uppercase;
    letter-spacing: 0.8px;
    border-bottom: 1.5px solid {border_color};
    padding-bottom: 1px;
    margin-bottom: 3px;
}}
.summary {{
    font-size: 9.5pt;
    color: #333;
    line-height: 1.4;
}}
.skill-row {{
    font-size: 9.5pt;
    margin: 0;
    line-height: 1.35;
}}
.skill-cat {{
    font-weight: 600;
    color: #1a3a5c;
}}
.entry {{
    margin-bottom: 4px;
    break-inside: avoid;
}}
.entry-title {{
    font-weight: 600;
    font-size: 10pt;
    color: #1a3a5c;
}}
.entry-subtitle {{
    font-size: 9pt;
    color: #4a7a9b;
    font-style: italic;
    margin-bottom: 1px;
}}
ul {{
    margin-left: 14px;
    padding: 0;
}}
li {{
    font-size: 9.5pt;
    margin-bottom: 1px;
    line-height: 1.35;
}}
.edu {{
    font-size: 10pt;
}}
</style>
</head>
<body>
<div class="header">
    <div class="name">{escape(resume['name'])}</div>
    <div class="title">{escape(resume['title'])}</div>
    {location_html}
    <div class="contact">{contact_html}</div>
</div>
{summary_html}
{skills_html}
{exp_html}
{proj_html}
{cert_html}
{edu_html}
</body>
</html>"""


def is_cover_letter(text: str, text_path: Path | None = None) -> bool:
    """Detect whether the input text is a cover letter rather than a structured resume."""
    if text_path and (text_path.name.endswith("_CL.txt") or "cover_letter" in str(text_path).lower()):
        return True
    stripped = text.strip()
    if stripped.lower().startswith("dear "):
        return True
    # Resumes always have SUMMARY and EXPERIENCE headers
    upper = stripped.upper()
    return "SUMMARY" not in upper and "EXPERIENCE" not in upper


def build_cover_letter_html(text: str, profile: dict | None = None) -> str:
    """Build clean business-letter HTML for a cover letter."""
    if profile is None:
        try:
            profile = config.load_profile()
        except Exception:
            profile = {}

    personal = profile.get("personal", {}) if isinstance(profile, dict) else {}
    full_name = personal.get("full_name", "")
    email = personal.get("email", "")
    phone = personal.get("phone", "")
    linkedin = personal.get("linkedin_url", "")
    city = personal.get("city", "")
    country = personal.get("country", "")

    contact_items = [escape(x) for x in (email, phone, linkedin, ", ".join(p for p in (city, country) if p)) if x]
    contact_line = " &nbsp;|&nbsp; ".join(contact_items)

    header_block = ""
    if full_name:
        header_block = f"""
<div class="header">
    <div class="name">{escape(full_name)}</div>
    {f'<div class="contact">{contact_line}</div>' if contact_line else ''}
</div>"""

    date_str = datetime.now().strftime("%d %B %Y")

    # Split into paragraphs by blank lines
    raw_paragraphs = [p.strip() for p in text.strip().split("\n\n") if p.strip()]
    para_html = "\n".join(
        f"<p>{escape(p).replace(chr(10), '<br>')}</p>"
        for p in raw_paragraphs
    )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{
    size: letter;
    margin: 0.75in 0.85in;
}}
* {{
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}}
body {{
    font-family: 'Calibri', 'Segoe UI', Arial, sans-serif;
    font-size: 11pt;
    line-height: 1.55;
    color: #1a1a1a;
}}
.header {{
    margin-bottom: 24px;
    padding-bottom: 10px;
    border-bottom: 1.5px solid #2a7ab5;
}}
.name {{
    font-size: 18pt;
    font-weight: 700;
    color: #1a3a5c;
}}
.contact {{
    font-size: 9.5pt;
    color: #444;
    margin-top: 4px;
}}
.date {{
    font-size: 10.5pt;
    color: #444;
    margin-bottom: 18px;
}}
.letter-body p {{
    margin-bottom: 14px;
    text-align: left;
}}
</style>
</head>
<body>
{header_block}
<div class="date">{date_str}</div>
<div class="letter-body">
{para_html}
</div>
</body>
</html>"""


# ── PDF Renderer ─────────────────────────────────────────────────────────

def render_pdf(html: str, output_path: str, page=None) -> None:
    """Render HTML to PDF using Playwright's headless Chromium."""
    if page is not None:
        page.set_content(html, wait_until="networkidle")
        page.pdf(
            path=output_path,
            format="Letter",
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            print_background=True,
        )
        return

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page()
        pg.set_content(html, wait_until="networkidle")
        pg.pdf(
            path=output_path,
            format="Letter",
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            print_background=True,
        )
        browser.close()


# ── Public API ───────────────────────────────────────────────────────────

def convert_to_pdf(
    text_path: Path,
    output_path: Path | None = None,
    html_only: bool = False,
    page=None,
    theme: str | None = None,
) -> Path:
    """Convert a text resume or cover letter to PDF (or HTML)."""
    text_path = Path(text_path)
    text = text_path.read_text(encoding="utf-8")

    resolved_theme = theme
    if not resolved_theme:
        try:
            prof = config.load_profile()
            resolved_theme = str(prof.get("pdf_theme") or "modern")
        except Exception:
            resolved_theme = "modern"

    if is_cover_letter(text, text_path):
        html = build_cover_letter_html(text)
    else:
        resume = parse_resume(text)
        html = build_html(resume, theme=resolved_theme)

    if html_only:
        out = output_path or text_path.with_suffix(".html")
        out = Path(out)
        out.write_text(html, encoding="utf-8")
        log.info("HTML generated: %s", out)
        return out

    out = output_path or text_path.with_suffix(".pdf")
    out = Path(out)
    render_pdf(html, str(out), page=page)
    log.info("PDF generated: %s", out)
    return out


def batch_convert(limit: int = 50) -> int:
    """Convert .txt files in TAILORED_DIR and COVER_LETTER_DIR that lack PDFs.

    Reuses a single Playwright browser instance across the entire batch and
    marks `pdf_generated_at` in SQLite so streaming mode completes cleanly.
    """
    candidates: list[Path] = []
    if config.TAILORED_DIR.exists():
        candidates.extend(
            f for f in sorted(config.TAILORED_DIR.glob("*.txt"))
            if not f.name.endswith("_JOB.txt")
        )
    if config.COVER_LETTER_DIR.exists():
        candidates.extend(sorted(config.COVER_LETTER_DIR.glob("*.txt")))

    to_convert: list[Path] = []
    for f in candidates:
        pdf_path = f.with_suffix(".pdf")
        if not pdf_path.exists():
            to_convert.append(f)
        if len(to_convert) >= limit:
            break

    now = datetime.now(UTC).isoformat()
    conn = get_connection()

    if not to_convert:
        log.info("All text files already have PDFs.")
        conn.execute(
            "UPDATE jobs SET pdf_generated_at = ? "
            "WHERE tailored_resume_path IS NOT NULL AND pdf_generated_at IS NULL",
            (now,),
        )
        conn.commit()
        return 0

    log.info("Converting %d files to PDF...", len(to_convert))
    converted = 0

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        for f in to_convert:
            try:
                convert_to_pdf(f, page=page)
                converted += 1
                conn.execute(
                    "UPDATE jobs SET pdf_generated_at = ? WHERE tailored_resume_path = ?",
                    (now, str(f)),
                )
            except Exception as e:
                log.error("Failed to convert %s: %s", f.name, e)
        browser.close()

    conn.execute(
        "UPDATE jobs SET pdf_generated_at = ? "
        "WHERE tailored_resume_path IS NOT NULL AND pdf_generated_at IS NULL",
        (now,),
    )
    conn.commit()

    log.info("Done: %d/%d PDFs generated", converted, len(to_convert))
    return converted
