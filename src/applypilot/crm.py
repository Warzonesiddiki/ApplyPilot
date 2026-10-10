"""Interactive Career CRM Web Server & Stage Manager (`applypilot serve`).

Serves the interactive Career CRM dashboard on `0.0.0.0` (compatible with both
local desktop browsers and remote sandboxed previews) and exposes REST endpoints
for updating CRM stages (`discovered`, `scored`, `tailored`, `ready`, `applied`,
`assessment`, `interviewing`, `offer`, `rejected`), saving candidate notes,
generating Interview Prep & Outreach packs on demand, and exporting CSV.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlparse

from rich.console import Console

from applypilot import config
from applypilot.database import close_connection, ensure_columns, get_stats, init_db
from applypilot.export import export_jobs_csv_string, fetch_export_rows
from applypilot.prep import find_target_job, generate_interview_prep, generate_outreach_pack
from applypilot.view import generate_dashboard

console = Console()

VALID_CRM_STAGES: tuple[str, ...] = (
    "discovered",
    "enriched",
    "scored",
    "tailored",
    "ready",
    "ready_to_apply",
    "applied",
    "assessment",
    "interviewing",
    "offer",
    "rejected",
    "archived",
)


def update_job_crm_stage(
    url: str,
    crm_stage: str,
    user_notes: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Update a job's Career CRM stage and optional candidate notes."""
    clean_stage = crm_stage.strip().lower()
    if clean_stage not in VALID_CRM_STAGES:
        raise ValueError(f"Invalid CRM stage '{crm_stage}'. Must be one of: {', '.join(VALID_CRM_STAGES)}")

    own_conn = conn is None
    if own_conn:
        conn = init_db()
    else:
        ensure_columns(conn)

    try:
        if user_notes is not None:
            cur = conn.execute(
                "UPDATE jobs SET crm_stage = ?, user_notes = ? WHERE url = ?",
                (clean_stage, user_notes, url),
            )
        else:
            cur = conn.execute(
                "UPDATE jobs SET crm_stage = ? WHERE url = ?",
                (clean_stage, url),
            )
        conn.commit()
        return cur.rowcount > 0
    finally:
        if own_conn:
            close_connection()


class _CRMRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler for the local Career CRM web application."""

    def log_message(self, format: str, *args) -> None:
        return  # Keep console output clean

    def _send_json(self, status: int, data: dict | list) -> None:
        raw = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if "profile" in qs and qs["profile"]:
            config.set_active_profile(qs["profile"][0])

        if parsed.path in ("/", "/index.html", "/dashboard"):
            conn = init_db()
            close_connection()
            html_path = Path(generate_dashboard(interactive_api=True))
            raw = html_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if parsed.path == "/api/jobs":
            conn = init_db()
            try:
                stats = get_stats(conn)
                jobs = fetch_export_rows(conn=conn)
                profiles = config.list_profiles()
                self._send_json(
                    200,
                    {
                        "active_profile": config.get_active_profile() or "default",
                        "profiles": profiles,
                        "stats": stats,
                        "jobs": jobs,
                    },
                )
            finally:
                close_connection()
            return

        if parsed.path in ("/api/export.csv", "/api/export"):
            conn = init_db()
            try:
                csv_str = export_jobs_csv_string(conn=conn)
            finally:
                close_connection()
            raw = csv_str.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="applypilot_jobs.csv"')
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body_raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            payload = json.loads(body_raw)
        except Exception:
            payload = {}

        if parsed.path == "/api/jobs/stage":
            url = str(payload.get("url") or "").strip()
            stage = str(payload.get("crm_stage") or "").strip()
            notes = payload.get("user_notes")
            if not url or not stage:
                self._send_json(400, {"error": "url and crm_stage are required"})
                return
            try:
                ok = update_job_crm_stage(url, stage, user_notes=notes)
                self._send_json(200, {"updated": ok, "url": url, "crm_stage": stage})
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if parsed.path == "/api/jobs/notes":
            url = str(payload.get("url") or "").strip()
            notes = str(payload.get("user_notes") or "")
            if not url:
                self._send_json(400, {"error": "url is required"})
                return
            conn = init_db()
            try:
                cur = conn.execute("UPDATE jobs SET user_notes = ? WHERE url = ?", (notes, url))
                conn.commit()
                self._send_json(200, {"updated": cur.rowcount > 0, "url": url, "user_notes": notes})
            finally:
                close_connection()
            return

        if parsed.path in ("/api/jobs/prep", "/api/jobs/outreach"):
            target = str(payload.get("url") or payload.get("query") or "").strip()
            conn = init_db()
            try:
                job = find_target_job(conn=conn, url_or_query=target)
                if not job:
                    self._send_json(404, {"error": "No matching job found"})
                    return
                prep_md, prep_path = generate_interview_prep(job, use_llm=False)
                out_md, out_path = generate_outreach_pack(job, use_llm=False)
                self._send_json(
                    200,
                    {
                        "url": job["url"],
                        "prep_path": str(prep_path),
                        "outreach_path": str(out_path),
                        "prep_markdown": prep_md,
                        "outreach_markdown": out_md,
                    },
                )
            finally:
                close_connection()
            return

        if parsed.path == "/api/jobs/proof":
            from applypilot.apply.native_agent import build_application_payload

            target = str(payload.get("url") or payload.get("query") or "").strip()
            conn = init_db()
            try:
                job = find_target_job(conn=conn, url_or_query=target)
                if not job:
                    self._send_json(404, {"error": "No matching job found"})
                    return
                proof = build_application_payload(job, conn=conn)
                self._send_json(200, proof)
            finally:
                close_connection()
            return

        if parsed.path == "/api/sync-email":
            from applypilot.email_sync import sync_application_emails

            messages = payload.get("messages") if isinstance(payload.get("messages"), list) else None
            summary = sync_application_emails(messages=messages)
            self._send_json(200, summary)
            return

        self._send_json(404, {"error": "Endpoint not found"})


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def serve_crm(host: str = "0.0.0.0", port: int = 8080) -> None:
    """Start the local interactive Career CRM web server."""
    init_db()
    close_connection()
    server = _ReusableThreadingHTTPServer((host, port), _CRMRequestHandler)
    prof = config.get_active_profile() or "default"
    console.print(
        f"[bold green]ApplyPilot Career CRM Server running at http://{host}:{port}[/bold green] "
        f"(active profile: [cyan]{prof}[/cyan])\n"
        "[dim]Press Ctrl+C to stop.[/dim]"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopping Career CRM server...[/yellow]")
    finally:
        server.server_close()
