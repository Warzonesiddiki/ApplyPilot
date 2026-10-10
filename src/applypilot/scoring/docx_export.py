"""Pure-Python Native Word (.docx) Resume & Cover Letter Exporter.

Generates valid OpenXML (.docx) documents for both tailored resumes and cover
letters using Python's standard `zipfile` and XML escaping — zero external binary
or C library dependencies required. Essential for UK/EU recruitment agencies
(Reed, Hays, Robert Walters, Michael Page, eFinancialCareers) that request
editable Word CVs.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape
import zipfile

from applypilot import config
from applypilot.database import ensure_columns, get_connection
from applypilot.scoring.pdf import is_cover_letter, parse_entries, parse_resume, parse_skills

_CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>
"""

_ROOT_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""

_DOC_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
"""

_STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults>
    <w:rPrDefault>
      <w:rPr>
        <w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>
        <w:sz w:val="21"/>
        <w:szCs w:val="21"/>
        <w:color w:val="1A1A1A"/>
      </w:rPr>
    </w:rPrDefault>
  </w:docDefaults>
</w:styles>
"""


def _w_run(
    text: str,
    bold: bool = False,
    italic: bool = False,
    size_half_pt: int = 21,
    color_hex: str = "1A1A1A",
) -> str:
    """Build a `<w:r>` XML fragment."""
    rpr = [
        '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/>',
        f'<w:sz w:val="{size_half_pt}"/>',
        f'<w:szCs w:val="{size_half_pt}"/>',
        f'<w:color w:val="{color_hex}"/>',
    ]
    if bold:
        rpr.append("<w:b/>")
    if italic:
        rpr.append("<w:i/>")
    rpr_xml = "".join(rpr)
    escaped = xml_escape(text or "")
    return f'<w:r><w:rPr>{rpr_xml}</w:rPr><w:t xml:space="preserve">{escaped}</w:t></w:r>'


def _w_para(
    runs_xml: str,
    space_before: int = 0,
    space_after: int = 60,
    align: str = "left",
    bottom_border: bool = False,
    left_indent: int = 0,
) -> str:
    """Build a `<w:p>` XML fragment."""
    ppr = [
        f'<w:spacing w:before="{space_before}" w:after="{space_after}" w:line="256" w:lineRule="auto"/>',
        f'<w:jc w:val="{align}"/>',
    ]
    if left_indent > 0:
        ppr.append(f'<w:ind w:left="{left_indent}" w:hanging="180"/>')
    if bottom_border:
        ppr.append(
            '<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="2" w:color="1F4E79"/></w:pBdr>'
        )
    ppr_xml = "".join(ppr)
    return f"<w:p><w:pPr>{ppr_xml}</w:pPr>{runs_xml}</w:p>"


def build_resume_docx_xml(resume_text: str) -> str:
    """Build `word/document.xml` for a structured resume."""
    parsed = parse_resume(resume_text)
    paragraphs: list[str] = []

    if parsed.get("name"):
        paragraphs.append(
            _w_para(
                _w_run(parsed["name"], bold=True, size_half_pt=34, color_hex="0F172A"),
                space_before=0,
                space_after=40,
            )
        )
    if parsed.get("title"):
        paragraphs.append(
            _w_para(
                _w_run(parsed["title"], bold=True, size_half_pt=22, color_hex="1F4E79"),
                space_before=0,
                space_after=40,
            )
        )

    contact_parts = [p for p in (parsed.get("location"), parsed.get("contact")) if p]
    if contact_parts:
        paragraphs.append(
            _w_para(
                _w_run(" | ".join(contact_parts), size_half_pt=19, color_hex="475569"),
                space_before=0,
                space_after=120,
            )
        )

    def _add_section_header(title: str) -> None:
        paragraphs.append(
            _w_para(
                _w_run(title.upper(), bold=True, size_half_pt=21, color_hex="1F4E79"),
                space_before=160,
                space_after=60,
                bottom_border=True,
            )
        )

    sections = parsed.get("sections", {})

    summary_txt = sections.get("SUMMARY") or parsed.get("summary")
    if summary_txt:
        _add_section_header("SUMMARY")
        paragraphs.append(
            _w_para(_w_run(str(summary_txt), size_half_pt=20), space_after=80)
        )

    skills_sec = (
        sections.get("TECHNICAL SKILLS")
        or sections.get("CORE COMPETENCIES")
        or sections.get("CORE COMPETENCIES & TECHNICAL SKILLS")
        or sections.get("SKILLS")
    )
    if skills_sec:
        _add_section_header("CORE COMPETENCIES & TECHNICAL SKILLS")
        for cat, items in parse_skills(skills_sec):
            runs = _w_run(f"{cat}: ", bold=True, size_half_pt=20) + _w_run(
                items, size_half_pt=20
            )
            paragraphs.append(_w_para(runs, space_after=40))

    exp_sec = sections.get("EXPERIENCE") or sections.get("PROFESSIONAL EXPERIENCE")
    if exp_sec:
        _add_section_header("EXPERIENCE")
        for entry in parse_entries(exp_sec):
            hdr = entry.get("title") or entry.get("header", "")
            sub = entry.get("subtitle", "")
            if hdr:
                paragraphs.append(
                    _w_para(
                        _w_run(hdr, bold=True, size_half_pt=21, color_hex="0F172A"),
                        space_before=100,
                        space_after=20,
                    )
                )
            if sub:
                paragraphs.append(
                    _w_para(
                        _w_run(sub, italic=True, size_half_pt=19, color_hex="475569"),
                        space_before=0,
                        space_after=40,
                    )
                )
            for bullet in entry.get("bullets", []):
                paragraphs.append(
                    _w_para(
                        _w_run(f"• {bullet}", size_half_pt=20),
                        space_after=35,
                        left_indent=300,
                    )
                )

    proj_sec = sections.get("PROJECTS") or sections.get("KEY ENGAGEMENTS")
    if proj_sec:
        _add_section_header("PROJECTS & KEY ENGAGEMENTS")
        for entry in parse_entries(proj_sec):
            hdr = entry.get("title") or entry.get("header", "")
            sub = entry.get("subtitle", "")
            if hdr:
                paragraphs.append(
                    _w_para(
                        _w_run(hdr, bold=True, size_half_pt=21, color_hex="0F172A"),
                        space_before=90,
                        space_after=20,
                    )
                )
            if sub:
                paragraphs.append(
                    _w_para(
                        _w_run(sub, italic=True, size_half_pt=19, color_hex="475569"),
                        space_before=0,
                        space_after=35,
                    )
                )
            for bullet in entry.get("bullets", []):
                paragraphs.append(
                    _w_para(
                        _w_run(f"• {bullet}", size_half_pt=20),
                        space_after=35,
                        left_indent=300,
                    )
                )

    cert_sec = (
        sections.get("CERTIFICATIONS")
        or sections.get("QUALIFICATIONS")
        or sections.get("QUALIFICATIONS & CERTIFICATIONS")
    )
    if cert_sec:
        _add_section_header("QUALIFICATIONS & CERTIFICATIONS")
        for line in cert_sec.splitlines():
            if line.strip():
                paragraphs.append(_w_para(_w_run(line.strip(), size_half_pt=20), space_after=40))

    edu_sec = sections.get("EDUCATION")
    if edu_sec:
        _add_section_header("EDUCATION")
        for line in edu_sec.splitlines():
            if line.strip():
                paragraphs.append(_w_para(_w_run(line.strip(), size_half_pt=20), space_after=40))

    body_xml = "".join(paragraphs)
    sect_pr = (
        '<w:sectPr>'
        '<w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="720" w:right="850" w:bottom="720" w:left="850" w:header="450" w:footer="450" w:gutter="0"/>'
        '</w:sectPr>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body_xml}{sect_pr}</w:body></w:document>"
    )


def build_cover_letter_docx_xml(text: str, profile: dict | None = None) -> str:
    """Build `word/document.xml` for a business cover letter."""
    personal = (profile or {}).get("personal", {}) if isinstance(profile, dict) else {}
    full_name = personal.get("full_name", "")
    email = personal.get("email", "")
    phone = personal.get("phone_formatted") or personal.get("phone", "")
    city = personal.get("city", "")
    country = personal.get("country", "")
    loc = ", ".join(p for p in (city, country) if p)

    paragraphs: list[str] = []
    if full_name:
        paragraphs.append(
            _w_para(
                _w_run(full_name, bold=True, size_half_pt=30, color_hex="0F172A"),
                space_after=30,
            )
        )
    contact_str = " | ".join(p for p in (loc, email, phone) if p)
    if contact_str:
        paragraphs.append(
            _w_para(
                _w_run(contact_str, size_half_pt=19, color_hex="475569"),
                space_after=160,
                bottom_border=True,
            )
        )

    date_str = datetime.now().strftime("%d %B %Y")
    paragraphs.append(
        _w_para(_w_run(date_str, size_half_pt=20, color_hex="475569"), space_before=120, space_after=160)
    )

    for block in [b.strip() for b in text.strip().split("\n\n") if b.strip()]:
        clean_block = " ".join(line.strip() for line in block.splitlines() if line.strip())
        paragraphs.append(_w_para(_w_run(clean_block, size_half_pt=22), space_after=140))

    body_xml = "".join(paragraphs)
    sect_pr = (
        '<w:sectPr>'
        '<w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1080" w:right="1080" w:bottom="1080" w:left="1080" w:header="540" w:footer="540" w:gutter="0"/>'
        '</w:sectPr>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body_xml}{sect_pr}</w:body></w:document>"
    )


def convert_to_docx(
    txt_path: Path | str,
    docx_path: Path | str | None = None,
    profile: dict | None = None,
) -> Path:
    """Convert a tailored resume or cover letter `.txt` file into a valid `.docx` file."""
    src = Path(txt_path)
    out = Path(docx_path) if docx_path else src.with_suffix(".docx")
    text = src.read_text(encoding="utf-8")

    if profile is None:
        try:
            profile = config.load_profile()
        except Exception:
            profile = {}

    if is_cover_letter(text, src):
        doc_xml = build_cover_letter_docx_xml(text, profile=profile)
    else:
        doc_xml = build_resume_docx_xml(text)

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _ROOT_RELS_XML)
        zf.writestr("word/_rels/document.xml.rels", _DOC_RELS_XML)
        zf.writestr("word/styles.xml", _STYLES_XML)
        zf.writestr("word/document.xml", doc_xml)

    return out


def batch_convert_docx() -> dict:
    """Convert all tailored resumes and cover letters into `.docx` format and update SQLite."""
    conn = get_connection()
    ensure_columns(conn)
    try:
        profile = config.load_profile()
    except Exception:
        profile = {}

    resume_converted = 0
    cover_converted = 0

    if config.TAILORED_DIR.exists():
        for txt_file in sorted(config.TAILORED_DIR.glob("*.txt")):
            if txt_file.name.endswith("_JOB.txt"):
                continue
            docx_file = txt_file.with_suffix(".docx")
            if not docx_file.exists():
                convert_to_docx(txt_file, docx_file, profile=profile)
                resume_converted += 1
            conn.execute(
                "UPDATE jobs SET docx_resume_path = ? WHERE tailored_resume_path = ?",
                (str(docx_file), str(txt_file)),
            )

    if config.COVER_LETTER_DIR.exists():
        for cl_file in sorted(config.COVER_LETTER_DIR.glob("*_CL.txt")):
            docx_file = cl_file.with_suffix(".docx")
            if not docx_file.exists():
                convert_to_docx(cl_file, docx_file, profile=profile)
                cover_converted += 1
            conn.execute(
                "UPDATE jobs SET docx_cover_path = ? WHERE cover_letter_path = ?",
                (str(docx_file), str(cl_file)),
            )

    conn.commit()
    return {"resumes_docx": resume_converted, "covers_docx": cover_converted}
