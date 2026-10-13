"""Interactive Career CRM Web Server & Stage Manager (`applypilot serve`).

Serves the interactive Career CRM dashboard on loopback by default (pass
`--host 0.0.0.0` to expose it to other machines) and exposes REST endpoints
for updating CRM stages (`discovered`, `scored`, `tailored`, `ready`, `applied`,
`assessment`, `interviewing`, `offer`, `rejected`), saving candidate notes,
generating Interview Prep & Outreach packs on demand, and exporting CSV.
"""

from __future__ import annotations

from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlparse
from typing import Any

from rich.console import Console

from applypilot import config
from applypilot.database import close_connection, ensure_columns, get_stats, init_db
from applypilot.export import export_jobs_csv_string, fetch_export_rows
from applypilot.prep import find_target_job, generate_interview_prep, generate_outreach_pack
from applypilot.view import generate_dashboard

console = Console()


class _RequestBodyTooLargeError(Exception):
    """A CRM request declared a body beyond the configured cap."""

    def __init__(self, length: int | None) -> None:
        super().__init__(length)
        self.length = length

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

# Local CRM requests are small JSON documents (stage updates, notes, prep
# triggers). Anything larger is rejected before the body is read so a
# misbehaving client cannot pin handler memory.
_MAX_REQUEST_BODY_BYTES = 1_000_000
# When refusing an oversized body the remaining bytes are discarded in bounded
# chunks (see _drain_request_body) so the client can still read the 413.
_DRAIN_CHUNK_BYTES = 64 * 1024
_MAX_DRAIN_BYTES = 8 * 1024 * 1024


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

    if conn is None:
        conn = init_db()
        own_conn = True
    else:
        ensure_columns(conn)
        own_conn = False

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

    def log_message(self, format: str, *args: object) -> None:
        return  # Keep console output clean

    def _send_json(self, status: int, data: Mapping[str, Any] | list[Any]) -> None:
        payload: dict[str, Any] | list[Any] = dict(data) if isinstance(data, Mapping) else data
        raw = json.dumps(payload, indent=2).encode("utf-8")
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

    def _drain_request_body(self, length: int | None) -> None:
        """Discard an unread request body so the client can read our response.

        Closing a socket while unread bytes are still queued makes the OS send
        RST, which discards the pending response and surfaces as a read error on
        the client. Reading and discarding in bounded chunks keeps memory flat
        while letting the status line through.
        """
        if not length or length <= 0:
            return
        remaining = min(length, _MAX_DRAIN_BYTES)
        while remaining > 0:
            chunk = self.rfile.read(min(_DRAIN_CHUNK_BYTES, remaining))
            if not chunk:
                return
            remaining -= len(chunk)

    def _read_json_object_body(self) -> dict[str, Any]:
        """Read the request body as a small JSON object.

        Missing, empty, unparsable, or non-object bodies return ``{}`` so a
        malformed request degrades to a validation error instead of crashing
        the handler thread. Oversized bodies are rejected once the declared
        length is known, without buffering them in memory; the caller drains the
        remaining bytes before replying so the 413 is actually delivered.
        """
        length_header = self.headers.get("Content-Length", "0")
        try:
            length = int(length_header)
        except (TypeError, ValueError):
            # Unsafe/inequivalent Content-Length gets refused before we touch
            # the body so a misbehaving client cannot pin handler memory.
            raise _RequestBodyTooLargeError(None) from None
        if length <= 0:
            return {}
        if length > _MAX_REQUEST_BODY_BYTES:
            raise _RequestBodyTooLargeError(length)
        raw = self.rfile.read(length)
        try:
            decoded: Any = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            payload = self._read_json_object_body()
        except _RequestBodyTooLargeError as exc:
            # Discard what the client is still sending before replying, otherwise
            # the connection is reset and the 413 never reaches it.
            self._drain_request_body(exc.length)
            self._send_json(413, {"error": f"Request body exceeds {_MAX_REQUEST_BODY_BYTES} bytes"})
            return

        if parsed.path == "/api/jobs/stage":
            url = str(payload.get("url") or "").strip()
            stage = str(payload.get("crm_stage") or "").strip()
            notes = payload.get("user_notes")
            if not url or not stage:
                self._send_json(400, {"error": "url and crm_stage are required"})
                return
            if notes is not None and not isinstance(notes, str):
                self._send_json(400, {"error": "user_notes must be a string when provided"})
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


# Hosts that keep the CRM reachable only from this machine.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def exposure_warning(host: str) -> str | None:
    """Return a warning when ``host`` publishes the CRM beyond this machine.

    The Career CRM holds personal data (contact details, notes, recruiter
    emails, application history), so binding to a routable interface is opt-in:
    loopback is the default and anything wider tells the user what they chose.
    """
    if host.strip().lower() in _LOOPBACK_HOSTS:
        return None
    return (
        f"[yellow]Warning:[/yellow] binding to [bold]{host}[/bold] exposes your Career CRM "
        "(personal data, notes, and application history) to other machines on this network.\n"
        "[dim]Use --host 127.0.0.1 for local-only access.[/dim]"
    )


def serve_crm(host: str = "127.0.0.1", port: int = 8080) -> None:
    """Start the local interactive Career CRM web server."""
    init_db()
    close_connection()
    warning = exposure_warning(host)
    if warning is not None:
        console.print(warning)
    server = _ReusableThreadingHTTPServer((host, port), _CRMRequestHandler)
    prof = config.get_active_profile() or "default"
    # 0.0.0.0/:: are bind-any addresses, not browsable ones.
    browsable = "localhost" if host in {"0.0.0.0", "::"} else host
    console.print(
        f"[bold green]ApplyPilot Career CRM Server running at http://{browsable}:{port}[/bold green] "
        f"(active profile: [cyan]{prof}[/cyan])\n"
        "[dim]Press Ctrl+C to stop.[/dim]"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopping Career CRM server...[/yellow]")
    finally:
        server.server_close()
