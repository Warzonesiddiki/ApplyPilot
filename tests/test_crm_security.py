"""Security regression tests for the Career CRM server and the dashboard HTML.

These tests assert the *security properties* themselves rather than incidental
behaviour: profile traversal must be refused with 400 before it can create a
directory anywhere on disk, the server must never hand the CRM to a foreign
origin, and an attacker-influenced job URL must not be able to break out of an
inline JS handler.
"""

from __future__ import annotations

from collections.abc import Iterator
from email.message import Message
import json
from pathlib import Path
import threading
import time
from typing import Any

import httpx
import pytest

import applypilot.config as config
import applypilot.database as database
from applypilot import crm as crm_module
from applypilot import view as view_module
from applypilot.crm import (
    _CRMRequestHandler,
    _is_valid_profile_slug,
    _profile_dir_is_contained,
    _ReusableThreadingHTTPServer,
)
from applypilot.view import generate_dashboard

_PROFILE: dict[str, Any] = {
    "personal": {
        "full_name": "Neha Tariq",
        "email": "neha@example.com",
        "phone": "+44 7700 900123",
        "city": "London",
        "country": "United Kingdom",
        "linkedin_url": "https://linkedin.com/in/neha-tariq",
    },
    "experience": {
        "years": "8",
        "education_level": "ACA Chartered Accountant",
        "target_role": "Audit Manager",
    },
    "skills_boundary": {"Audit": ["IFRS", "UK GAAP", "External Audit"]},
}


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str = "crm-sec-tests") -> Path:
    """Point config at ``tmp_path`` and create a profile workspace holding one job."""
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile(profile)
    config.ensure_dirs()
    config.RESUME_PATH.write_text("Neha Tariq, ACA. Audit experience.", encoding="utf-8")
    config.PROFILE_PATH.write_text(json.dumps(_PROFILE), encoding="utf-8")

    conn = database.init_db()
    database.ensure_columns(conn)
    conn.execute(
        "INSERT INTO jobs (url, title, company, location, fit_score, full_description, site, strategy, "
        "discovered_at, crm_stage, user_notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "https://jobs.example.com/audit-1",
            "Audit Manager",
            "PwC UK",
            "Leeds, UK",
            9,
            "Statutory audit under UK GAAP and IFRS.",
            "ICAEW Jobs",
            "ats",
            "2026-10-10T10:00:00Z",
            "tailored",
            "Recruiter pinged on LinkedIn",
        ),
    )
    conn.commit()
    database.close_connection()
    return tmp_path


def _settled_profile(timeout: float = 5.0) -> str:
    """Return the process-wide active profile once the handler thread has settled.

    A request restores the previous profile in its ``finally``, which runs just
    after the response bytes are flushed, so an immediate read from the test
    thread would race with it.
    """
    deadline = time.monotonic() + timeout
    current = str(config.get_active_profile())
    while current != "crm-sec-tests" and time.monotonic() < deadline:
        time.sleep(0.02)
        current = str(config.get_active_profile())
    return current


def _insert_job(url: str, title: str, company: str, score: int, **extra: str) -> None:
    """Insert one job row using the already-configured active profile workspace."""
    conn = database.init_db()
    database.ensure_columns(conn)
    site = extra.get("site", "indeed")
    description = extra.get("full_description", "x" * 40)
    application_url = extra.get("application_url")
    columns = "url, title, company, location, fit_score, full_description, site, strategy, discovered_at, crm_stage"
    values = (url, title, company, extra.get("location", "London, UK"), score, description, site, "jobspy",
              "2026-10-10T10:00:00Z", extra.get("crm_stage", "discovered"))
    params: tuple[Any, ...] = values
    if application_url is not None:
        columns += ", application_url"
        params = params + (application_url,)
    conn.execute(f"INSERT INTO jobs ({columns}) VALUES ({', '.join('?' * params.__len__())})", params)
    conn.commit()
    database.close_connection()


@pytest.fixture()
def crm_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[httpx.Client, str]]:
    """Loopback CRM server backed by an isolated temp workspace."""
    _seed(tmp_path, monkeypatch)

    server = _ReusableThreadingHTTPServer(("127.0.0.1", 0), _CRMRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])

    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30.0) as client:
            yield client, f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)
        database.close_connection()
        config.set_active_profile(None)


# ---------------------------------------------------------------------------
# C1 - profile traversal / arbitrary file write via ?profile=
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "..",
        "../..",
        "../../etc",
        "%2e%2e%2f%2e%2e",
        "a/b",
        "a\\b",
        "sub/prof",
        "-leading-dash",
        "_leading_underscore",
        "",
        " ",
        "with space",
        "null\x00byte",
        "x" * 65,
        "café",
        "C:\\Windows\\Temp",
    ],
)
def test_is_valid_profile_slug_rejects_traversal_and_separators(bad: str) -> None:
    assert _is_valid_profile_slug(bad) is False


@pytest.mark.parametrize("good", ["default", "tahir", "crm-tests", "crm_sec_tests", "a", "9lives", "x" * 64])
def test_is_valid_profile_slug_accepts_ordinary_slugs(good: str) -> None:
    assert _is_valid_profile_slug(good) is True


def test_is_valid_profile_slug_normalises_case_and_whitespace() -> None:
    assert _is_valid_profile_slug("  Tahir  ") is True


@pytest.mark.parametrize("attack", ["../..", "../../etc", "..", "%2e%2e%2f%2e%2e", "....//....//etc/passwd"])
def test_get_with_traversal_profile_is_rejected_with_400(
    crm_server: tuple[httpx.Client, str], attack: str
) -> None:
    client, _ = crm_server
    response = client.get("/", params={"profile": attack})
    assert response.status_code == 400, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert "profile" in response.json()["error"]


def test_traversal_profile_creates_nothing_on_disk(
    crm_server: tuple[httpx.Client, str], tmp_path: Path
) -> None:
    """A rejected profile must not create a directory, DB, or dashboard anywhere."""
    client, _ = crm_server
    workspace = Path(config.BASE_APP_DIR)
    before = sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*"))

    for attack in ("../..", "../../etc", "..", "....//....//escaped"):
        assert client.get("/", params={"profile": attack}).status_code == 400
        assert client.get("/api/jobs", params={"profile": attack}).status_code == 400

    after = sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*"))
    assert after == before, "a rejected profile still touched the filesystem"

    # And nothing leaked outside the workspace root either.
    assert not (tmp_path / "escaped").exists()
    assert not (tmp_path.parent / "escaped").exists()


def test_traversal_profile_does_not_mutate_the_active_profile(crm_server: tuple[httpx.Client, str]) -> None:
    client, _ = crm_server
    before = config.get_active_profile()
    app_dir_before = config.APP_DIR

    assert client.get("/api/jobs", params={"profile": "../../etc"}).status_code == 400

    assert config.get_active_profile() == before
    assert config.APP_DIR == app_dir_before


def test_legitimate_profile_selection_is_scoped_to_the_request(
    crm_server: tuple[httpx.Client, str], tmp_path: Path
) -> None:
    """A valid slug still works, but it does not leak into the process afterwards."""
    client, _ = crm_server
    other = tmp_path / "profiles" / "second-candidate"
    other.mkdir(parents=True, exist_ok=True)
    (other / "profile.json").write_text(json.dumps(_PROFILE), encoding="utf-8")

    response = client.get("/api/jobs", params={"profile": "second-candidate"})
    assert response.status_code == 200
    assert response.json()["active_profile"] == "second-candidate"

    # Process-wide state is restored once the request finishes.
    assert _settled_profile() == "crm-sec-tests"
    assert config.APP_DIR == tmp_path / "profiles" / "crm-sec-tests"

    assert client.get("/api/jobs").json()["active_profile"] == "crm-sec-tests"


def test_post_body_profile_is_validated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`profile` inside a POST body is guarded exactly like the query parameter."""
    _seed(tmp_path / "post-profile", monkeypatch)

    server = _ReusableThreadingHTTPServer(("127.0.0.1", 0), _CRMRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30.0) as client:
            bad = client.post(
                "/api/jobs/stage",
                json={"url": "https://jobs.example.com/audit-1", "crm_stage": "applied", "profile": "../.."},
            )
            assert bad.status_code == 400
            assert "profile" in bad.json()["error"]

            wrong_type = client.post(
                "/api/jobs/stage",
                json={"url": "https://jobs.example.com/audit-1", "crm_stage": "applied", "profile": ["../.."]},
            )
            assert wrong_type.status_code == 400

            good = client.post(
                "/api/jobs/stage",
                json={"url": "https://jobs.example.com/audit-1", "crm_stage": "applied", "profile": "crm-sec-tests"},
            )
            assert good.status_code == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)
        database.close_connection()
        config.set_active_profile(None)


def test_profile_dir_containment_check_rejects_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The containment guard is real defence in depth, not just the slug regex."""
    _seed(tmp_path, monkeypatch)

    profiles_root = config.PROFILES_DIR.resolve()
    base_root = config.BASE_APP_DIR.resolve()

    assert _profile_dir_is_contained(profiles_root / "ok") is True
    assert _profile_dir_is_contained(base_root) is True
    assert _profile_dir_is_contained(tmp_path / "outside") is False
    assert _profile_dir_is_contained(profiles_root / ".." / ".." / "etc") is False


def test_request_profile_is_thread_local_and_cleared(crm_server: tuple[httpx.Client, str]) -> None:
    client, _ = crm_server
    assert crm_module._current_request_profile() is None
    assert client.get("/api/jobs", params={"profile": "crm-sec-tests"}).status_code == 200
    assert crm_module._current_request_profile() is None


# ---------------------------------------------------------------------------
# C5 - no wildcard CORS, cross-origin POST refused
# ---------------------------------------------------------------------------


def test_responses_never_carry_a_wildcard_allow_origin(crm_server: tuple[httpx.Client, str]) -> None:
    client, base = crm_server
    for path in ("/api/jobs", "/api/export.csv", "/", "/api/does-not-exist"):
        response = client.get(path)
        headers = {k.lower() for k in response.headers}
        assert "access-control-allow-origin" not in headers, path
    assert base


def test_same_origin_requests_get_their_own_origin_echoed(crm_server: tuple[httpx.Client, str]) -> None:
    client, base = crm_server
    response = client.get("/api/jobs", headers={"Origin": base})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == base
    assert response.headers["vary"] == "Origin"


def test_cross_origin_requests_get_no_allow_origin(crm_server: tuple[httpx.Client, str]) -> None:
    client, _ = crm_server
    response = client.get("/api/jobs", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200  # no CORS grant, so a browser cannot read it
    assert "access-control-allow-origin" not in {k.lower() for k in response.headers}


def test_cross_origin_post_is_rejected_with_403(crm_server: tuple[httpx.Client, str]) -> None:
    client, _ = crm_server
    evil = client.post(
        "/api/jobs/stage",
        json={"url": "https://jobs.example.com/audit-1", "crm_stage": "applied"},
        headers={"Origin": "https://evil.example"},
    )
    assert evil.status_code == 403
    assert evil.json()["error"] == "cross-origin requests are not allowed"
    assert "access-control-allow-origin" not in {k.lower() for k in evil.headers}

    # The refused request must not have mutated anything.
    conn = database.init_db()
    try:
        row = conn.execute(
            "SELECT crm_stage FROM jobs WHERE url = ?", ("https://jobs.example.com/audit-1",)
        ).fetchone()
    finally:
        database.close_connection()
    assert row is not None
    assert row["crm_stage"] == "tailored"


def test_same_origin_post_is_allowed(crm_server: tuple[httpx.Client, str]) -> None:
    client, base = crm_server
    response = client.post(
        "/api/jobs/stage",
        json={"url": "https://jobs.example.com/audit-1", "crm_stage": "interviewing"},
        headers={"Origin": base},
    )
    assert response.status_code == 200
    assert response.json()["updated"] is True
    assert response.headers["access-control-allow-origin"] == base


def test_post_without_origin_is_allowed_for_the_cli(crm_server: tuple[httpx.Client, str]) -> None:
    client, _ = crm_server
    response = client.post(
        "/api/jobs/stage",
        json={"url": "https://jobs.example.com/audit-1", "crm_stage": "applied"},
    )
    assert response.status_code == 200
    assert response.json()["updated"] is True


def test_options_preflight_refuses_foreign_origin(crm_server: tuple[httpx.Client, str]) -> None:
    client, base = crm_server

    bad = client.request("OPTIONS", "/api/jobs/stage", headers={"Origin": "https://evil.example"})
    assert bad.status_code == 403
    assert "access-control-allow-origin" not in {k.lower() for k in bad.headers}

    good = client.request(
        "OPTIONS",
        "/api/jobs/stage",
        headers={"Origin": base, "Access-Control-Request-Method": "POST"},
    )
    assert good.status_code == 204
    assert good.headers["access-control-allow-origin"] == base
    assert "POST" in good.headers["access-control-allow-methods"]


def test_origin_allowed_helper_only_accepts_exact_host_port() -> None:
    handler = object.__new__(_CRMRequestHandler)
    headers = Message()
    headers["Host"] = "127.0.0.1:8123"
    handler.headers = headers

    assert handler._origin_allowed("http://127.0.0.1:8123") is True
    assert handler._origin_allowed("http://127.0.0.1:8123/") is True
    assert handler._origin_allowed("HTTP://127.0.0.1:8123") is True

    for bad in (
        "http://evil.example",
        "http://127.0.0.1:9999",
        "http://127.0.0.1",
        "http://user@127.0.0.1:8123",
        "null",
        "",
        "file://127.0.0.1:8123",
        "http://127.0.0.1:8123.evil.example",
    ):
        assert handler._origin_allowed(bad) is False, bad


# ---------------------------------------------------------------------------
# H8 - unhandled handler exceptions must not leak tracebacks or server paths
# ---------------------------------------------------------------------------


def test_internal_error_returns_generic_500_without_traceback(
    crm_server: tuple[httpx.Client, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = crm_server
    workspace = Path(config.BASE_APP_DIR)

    def boom(*args: object, **kwargs: object) -> bool:
        raise RuntimeError(f"kaboom at {workspace / 'applypilot.db'}")

    monkeypatch.setattr(crm_module, "update_job_crm_stage", boom)

    response = client.post(
        "/api/jobs/stage",
        json={"url": "https://jobs.example.com/audit-1", "crm_stage": "applied"},
    )
    assert response.status_code == 500
    assert response.json() == {"error": "internal server error"}

    body = response.text
    assert "Traceback" not in body
    assert "RuntimeError" not in body
    assert "kaboom" not in body
    assert str(workspace) not in body
    assert str(tmp_path) not in body
    assert "applypilot.db" not in body
    assert "site-packages" not in body


def test_get_internal_error_returns_generic_500(
    crm_server: tuple[httpx.Client, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = crm_server

    def boom(*args: object, **kwargs: object) -> str:
        raise FileNotFoundError("C:\\Users\\victim\\.applypilot\\profiles\\x\\profile.json")

    monkeypatch.setattr(crm_module, "generate_dashboard", boom)

    response = client.get("/")
    assert response.status_code == 500
    assert response.json() == {"error": "internal server error"}
    assert "Traceback" not in response.text
    assert "FileNotFoundError" not in response.text
    assert "victim" not in response.text


# ---------------------------------------------------------------------------
# C4 / F20 - a job URL must never reach an inline JS handler
# ---------------------------------------------------------------------------

_RAW_QUOTE_URL = "https://jobs.example.com/quote'-alert(1)-'"
_ENTITY_QUOTE_URL = "https://jobs.example.com/a&#x27;-alert(2)-&#x27;"


def test_job_url_cannot_break_out_of_inline_handlers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(tmp_path / "xss", monkeypatch)
    _insert_job(
        _RAW_QUOTE_URL,
        "Attacker's <b>title</b>",
        "Evil & Co",
        5,
        site="inde<script>ed</script>",
    )
    _insert_job(_ENTITY_QUOTE_URL, "Entity URL", "Entity Co", 4)

    out = tmp_path / "xss-dashboard.html"
    generate_dashboard(output_path=str(out), interactive_api=True)
    html = out.read_text(encoding="utf-8")

    # 1. No inline handler ever interpolates the raw URL any more.
    assert "onchange=\"saveJobNote('" not in html
    assert "onchange=\"updateJobStage('" not in html
    assert "onclick=\"generatePrepAndOutreach('" not in html
    assert "onclick=\"generateProofPacket('" not in html

    # 2. The breakout payload itself (quote + payload + quote) is nowhere.
    assert "'-alert(1)-'" not in html
    assert "'-alert(2)-'" not in html

    # 3. The handlers take the element and read `dataset.url`.
    assert 'onchange="saveJobNote(this)"' in html
    assert 'onchange="updateJobStage(this, this.value)"' in html
    assert 'onclick="generatePrepAndOutreach(this)"' in html
    assert 'onclick="generateProofPacket(this)"' in html
    assert "function jobUrl(el)" in html

    # 4. The URL is carried in an HTML-escaped data-url attribute instead.
    assert "data-url=\"https://jobs.example.com/quote&#x27;-alert(1)-&#x27;\"" in html
    assert "data-url=\"https://jobs.example.com/a&amp;#x27;-alert(2)-&amp;#x27;\"" in html
    assert "data-url=\"https://jobs.example.com/quote'-alert(1)-'\"" not in html

    # 5. Injected markup in the other columns stayed inert.
    assert "Attacker's <b>title</b>" not in html
    assert "inde<script>ed</script>" not in html

    # 6. The dashboard script block is still structurally intact and wired up.
    assert html.count("<script>") == 1
    assert html.count("</script>") == 1
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    assert script.rstrip().endswith("applyFilters();")
    for fn in ("updateJobStage", "saveJobNote", "generateProofPacket", "generatePrepAndOutreach", "renderKanban"):
        assert f"function {fn}" in script, fn


def test_dashboard_never_renders_an_active_script_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(tmp_path / "scheme", monkeypatch)
    _insert_job(
        "https://jobs.example.com/ok",
        "Legit",
        "Good Co",
        6,
        application_url="javascript:alert(document.cookie)",
    )

    out = tmp_path / "scheme-dashboard.html"
    generate_dashboard(output_path=str(out), interactive_api=True)
    html = out.read_text(encoding="utf-8")

    assert 'href="javascript:' not in html.lower()
    assert "javascript:alert" not in html


# ---------------------------------------------------------------------------
# F6 / F19 / F30 - dashboard lifecycle
# ---------------------------------------------------------------------------


def test_dashboard_renders_on_a_brand_new_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F6: no profile.json and no DB file - `dashboard` must still render."""
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile("fresh-install")
    config.ensure_dirs()
    db_file = tmp_path / "profiles" / "fresh-install" / "applypilot.db"
    try:
        assert not db_file.exists()

        out = tmp_path / "fresh.html"
        path = generate_dashboard(output_path=str(out))
        assert Path(path) == out.resolve()
        assert "<!DOCTYPE html>" in out.read_text(encoding="utf-8")
        assert db_file.exists(), "init_db() should have created the schema"
    finally:
        database.close_connection()
        config.set_active_profile(None)


def test_failed_render_cleans_up_the_temp_file_and_keeps_the_previous_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F19: a mid-render failure must not leak dashboard.html.tmp."""
    _seed(tmp_path / "render", monkeypatch)
    out = tmp_path / "render" / "dashboard.html"
    generate_dashboard(output_path=str(out), interactive_api=True)
    good = out.read_text(encoding="utf-8")

    def boom(_url: str) -> str:
        raise RuntimeError("render blew up")

    # _safe_href is only reached inside the job loop, i.e. after the temp file
    # has been opened and partially written.
    monkeypatch.setattr(view_module, "_safe_href", boom)

    with pytest.raises(RuntimeError, match="render blew up"):
        generate_dashboard(output_path=str(out), interactive_api=True)

    assert not out.with_name(out.name + ".tmp").exists(), "temp file leaked after a failed render"
    assert out.read_text(encoding="utf-8") == good, "previous dashboard must survive a failed render"
    database.close_connection()


def test_generate_dashboard_closes_its_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(tmp_path / "conn", monkeypatch)
    db_file = (tmp_path / "profiles" / "crm-sec-tests" / "applypilot.db").resolve()

    database.get_connection()  # warm the thread-local cache
    generate_dashboard(output_path=str(tmp_path / "closed.html"), interactive_api=True)

    cache = getattr(database._local, "connections", {})
    assert str(db_file) not in cache, "generate_dashboard leaked its SQLite connection"