"""ApplyPilot configuration: paths, multi-profile management, platform detection, and filters."""

from __future__ import annotations

import json
import os
import platform
import shutil
from pathlib import Path
from typing import Any

import yaml

# Base application directory — holds shared .env and profiles/ subdirectory
BASE_APP_DIR = Path(os.environ.get("APPLYPILOT_DIR", Path.home() / ".applypilot"))
PROFILES_DIR = BASE_APP_DIR / "profiles"
ACTIVE_PROFILE_FILE = BASE_APP_DIR / "active_profile"
GLOBAL_ENV_PATH = BASE_APP_DIR / ".env"

# Package-shipped config (YAML registries)
PACKAGE_DIR = Path(__file__).parent
CONFIG_DIR = PACKAGE_DIR / "config"

# Active profile state
_ACTIVE_PROFILE: str | None = os.environ.get("APPLYPILOT_PROFILE") or None

# Dynamic module-level paths (updated by set_active_profile / _refresh_paths)
APP_DIR: Path = BASE_APP_DIR
DB_PATH: Path = APP_DIR / "applypilot.db"
PROFILE_PATH: Path = APP_DIR / "profile.json"
RESUME_PATH: Path = APP_DIR / "resume.txt"
RESUME_PDF_PATH: Path = APP_DIR / "resume.pdf"
SEARCH_CONFIG_PATH: Path = APP_DIR / "searches.yaml"
ENV_PATH: Path = APP_DIR / ".env"
TAILORED_DIR: Path = APP_DIR / "tailored_resumes"
COVER_LETTER_DIR: Path = APP_DIR / "cover_letters"
PREP_DIR: Path = APP_DIR / "interview_prep"
OUTREACH_DIR: Path = APP_DIR / "outreach"
PROOFS_DIR: Path = APP_DIR / "proofs"
LOG_DIR: Path = APP_DIR / "logs"
CHROME_WORKER_DIR: Path = APP_DIR / "chrome-workers"
APPLY_WORKER_DIR: Path = APP_DIR / "apply-workers"

LLM_ENV_KEYS: tuple[str, ...] = (
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "DEEPSEEK_API_KEY",
    "LLM_URL",
)


def _get_base_dir() -> Path:
    """Return the base ApplyPilot directory, respecting APPLYPILOT_DIR or BASE_APP_DIR."""
    if "APPLYPILOT_DIR" in os.environ:
        return Path(os.environ["APPLYPILOT_DIR"])
    return BASE_APP_DIR


def _resolve_app_dir(profile_name: str | None = None) -> Path:
    """Resolve the active workspace directory based on profile selection."""
    base = _get_base_dir()
    name = profile_name if profile_name is not None else _ACTIVE_PROFILE
    if not name:
        env_prof = os.environ.get("APPLYPILOT_PROFILE", "").strip()
        if env_prof:
            name = env_prof
        elif (base / "active_profile").exists():
            try:
                saved = (base / "active_profile").read_text(encoding="utf-8").strip()
                if saved:
                    name = saved
            except OSError:
                pass
    if name:
        return base / "profiles" / name
    return base


def _refresh_paths(profile_name: str | None = None) -> Path:
    """Recompute all module-level path constants for the active profile."""
    global BASE_APP_DIR, PROFILES_DIR, ACTIVE_PROFILE_FILE, GLOBAL_ENV_PATH
    global APP_DIR, DB_PATH, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH
    global SEARCH_CONFIG_PATH, ENV_PATH, TAILORED_DIR, COVER_LETTER_DIR
    global PREP_DIR, OUTREACH_DIR, PROOFS_DIR
    global LOG_DIR, CHROME_WORKER_DIR, APPLY_WORKER_DIR

    BASE_APP_DIR = _get_base_dir()
    PROFILES_DIR = BASE_APP_DIR / "profiles"
    ACTIVE_PROFILE_FILE = BASE_APP_DIR / "active_profile"
    GLOBAL_ENV_PATH = BASE_APP_DIR / ".env"

    APP_DIR = _resolve_app_dir(profile_name)
    DB_PATH = APP_DIR / "applypilot.db"
    PROFILE_PATH = APP_DIR / "profile.json"
    RESUME_PATH = APP_DIR / "resume.txt"
    RESUME_PDF_PATH = APP_DIR / "resume.pdf"
    SEARCH_CONFIG_PATH = APP_DIR / "searches.yaml"
    ENV_PATH = APP_DIR / ".env"
    TAILORED_DIR = APP_DIR / "tailored_resumes"
    COVER_LETTER_DIR = APP_DIR / "cover_letters"
    PREP_DIR = APP_DIR / "interview_prep"
    OUTREACH_DIR = APP_DIR / "outreach"
    PROOFS_DIR = APP_DIR / "proofs"
    LOG_DIR = APP_DIR / "logs"
    CHROME_WORKER_DIR = APP_DIR / "chrome-workers"
    APPLY_WORKER_DIR = APP_DIR / "apply-workers"
    return APP_DIR


def set_active_profile(profile_name: str | None, persist: bool = False) -> Path:
    """Set the active candidate profile (e.g. 'tahir' or 'neha') and update paths.

    Args:
        profile_name: Profile slug (or None for root legacy directory).
        persist: If True, write the profile name to ~/.applypilot/active_profile.

    Returns:
        Resolved APP_DIR for the profile.
    """
    global _ACTIVE_PROFILE
    clean = profile_name.strip().lower() if profile_name else None
    _ACTIVE_PROFILE = clean
    if clean:
        os.environ["APPLYPILOT_PROFILE"] = clean
    else:
        os.environ.pop("APPLYPILOT_PROFILE", None)

    app_dir = _refresh_paths(clean)
    if persist and clean:
        BASE_APP_DIR.mkdir(parents=True, exist_ok=True)
        ACTIVE_PROFILE_FILE.write_text(clean + "\n", encoding="utf-8")
    return app_dir


def get_active_profile() -> str | None:
    """Return the currently active profile slug, or None if using root."""
    if _ACTIVE_PROFILE:
        return _ACTIVE_PROFILE
    env_prof = os.environ.get("APPLYPILOT_PROFILE", "").strip()
    if env_prof:
        return env_prof
    base = _get_base_dir()
    af = base / "active_profile"
    if af.exists():
        try:
            return af.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


def list_profiles() -> list[dict[str, Any]]:
    """List all configured candidate profiles and their workspace status."""
    base = _get_base_dir()
    pdir = base / "profiles"
    active = get_active_profile()

    items: list[dict[str, Any]] = [
        {
            "name": "default",
            "active": active is None,
            "app_dir": str(base),
            "has_profile": (base / "profile.json").exists(),
            "has_resume": (base / "resume.txt").exists() or (base / "resume.pdf").exists(),
            "has_searches": (base / "searches.yaml").exists(),
        }
    ]
    if pdir.exists():
        for d in sorted(pdir.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                items.append(
                    {
                        "name": d.name,
                        "active": active == d.name,
                        "app_dir": str(d),
                        "has_profile": (d / "profile.json").exists(),
                        "has_resume": (d / "resume.txt").exists() or (d / "resume.pdf").exists(),
                        "has_searches": (d / "searches.yaml").exists(),
                    }
                )
    return items


# Initialize paths on module load
_refresh_paths()


def get_chrome_path() -> str:
    """Auto-detect Chrome/Chromium executable path, cross-platform.

    Override with CHROME_PATH environment variable.
    """
    env_path = os.environ.get("CHROME_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    system = platform.system()

    if system == "Windows":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
            / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
    elif system == "Darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    else:  # Linux
        candidates = []
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))

    for c in candidates:
        if c and c.exists():
            return str(c)

    # Fall back to PATH search
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found

    raise FileNotFoundError(
        "Chrome/Chromium not found. Install Chrome or set CHROME_PATH environment variable."
    )


def get_chrome_user_data() -> Path:
    """Default Chrome user data directory, cross-platform."""
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    return Path.home() / ".config" / "google-chrome"


def ensure_dirs() -> None:
    """Create all required directories for the active profile."""
    _refresh_paths()
    for d in [
        BASE_APP_DIR,
        APP_DIR,
        TAILORED_DIR,
        COVER_LETTER_DIR,
        PREP_DIR,
        OUTREACH_DIR,
        PROOFS_DIR,
        LOG_DIR,
        CHROME_WORKER_DIR,
        APPLY_WORKER_DIR,
    ]:
        d.mkdir(parents=True, exist_ok=True)


def secure_write_text(path: Path, content: str) -> None:
    """Write sensitive config/env file and set 0o600 permissions on POSIX."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if platform.system() != "Windows":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _string_keyed_mapping(value: Any) -> dict[str, Any]:
    """Return a mapping containing only string-keyed entries."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(key, str):
            result[key] = item
    return result


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    """Load a YAML file as a validated string-keyed mapping."""
    return _string_keyed_mapping(yaml.safe_load(path.read_text(encoding="utf-8")))


def _string_list(value: Any) -> list[str]:
    """Normalize a YAML string or list of strings into a clean list."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def load_profile() -> dict[str, Any]:
    """Load user profile from the active profile's profile.json."""
    _refresh_paths()
    if not PROFILE_PATH.exists():
        prof = get_active_profile()
        hint = f"applypilot -p {prof} init" if prof else "applypilot init"
        raise FileNotFoundError(
            f"Profile not found at {PROFILE_PATH}. Run `{hint}` first."
        )
    profile: Any = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    if not isinstance(profile, dict):
        raise ValueError(f"Profile at {PROFILE_PATH} must contain a JSON object")
    return _string_keyed_mapping(profile)


def load_search_config() -> dict[str, Any]:
    """Load search configuration from the active profile's searches.yaml."""
    _refresh_paths()
    if not SEARCH_CONFIG_PATH.exists():
        example = CONFIG_DIR / "searches.example.yaml"
        if example.exists():
            return _read_yaml_mapping(example)
        return {}
    return _read_yaml_mapping(SEARCH_CONFIG_PATH)


def load_sites_config() -> dict[str, Any]:
    """Load sites.yaml configuration (sites list, manual_ats, blocked, etc.)."""
    path = CONFIG_DIR / "sites.yaml"
    if not path.exists():
        return {}
    return _read_yaml_mapping(path)


def load_employers() -> list[dict[str, Any]] | dict[str, Any]:
    """Load employers registry from bundled employers.yaml."""
    path = CONFIG_DIR / "employers.yaml"
    if not path.exists():
        return []
    data = _read_yaml_mapping(path)
    employers = data.get("employers", [])
    if isinstance(employers, dict):
        return {
            key: _string_keyed_mapping(value)
            for key, value in employers.items()
            if isinstance(key, str) and isinstance(value, dict)
        }
    if isinstance(employers, list):
        return [_string_keyed_mapping(value) for value in employers if isinstance(value, dict)]
    return []


def load_ats_employers() -> dict[str, list[dict[str, Any]]]:
    """Load direct ATS employer registries (greenhouse, lever, ashby, smartrecruiters) from employers.yaml."""
    path = CONFIG_DIR / "employers.yaml"
    if not path.exists():
        return {}
    data = _read_yaml_mapping(path)
    registries = data.get("ats_employers", {})
    if not isinstance(registries, dict):
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    for board, entries in registries.items():
        if isinstance(board, str) and isinstance(entries, list):
            result[board] = [_string_keyed_mapping(entry) for entry in entries if isinstance(entry, dict)]
    return result


def is_manual_ats(url: str | None) -> bool:
    """Check if a URL routes through an ATS that requires manual application."""
    if not url:
        return False
    sites_cfg = load_sites_config()
    domains = _string_list(sites_cfg.get("manual_ats", []))
    url_lower = url.lower()
    return any(domain.lower() in url_lower for domain in domains)


def load_blocked_sites() -> tuple[set[str], list[str]]:
    """Load blocked sites and URL patterns from sites.yaml.

    Returns:
        (blocked_site_names, blocked_url_patterns)
    """
    cfg = load_sites_config()
    blocked = _string_keyed_mapping(cfg.get("blocked", {}))
    sites = set(_string_list(blocked.get("sites", [])))
    patterns = _string_list(blocked.get("url_patterns", []))
    return sites, patterns


def load_blocked_sso() -> list[str]:
    """Load blocked SSO domains from sites.yaml."""
    cfg = load_sites_config()
    return _string_list(cfg.get("blocked_sso", []))


def load_base_urls() -> dict[str, str | None]:
    """Load site base URLs for URL resolution from sites.yaml."""
    cfg = load_sites_config()
    raw_base_urls = cfg.get("base_urls", {})
    if not isinstance(raw_base_urls, dict):
        return {}
    return {
        key: value
        for key, value in raw_base_urls.items()
        if isinstance(key, str) and (isinstance(value, str) or value is None)
    }


# ---------------------------------------------------------------------------
# Unified Location, Title & EU/UK Country Resolution Helpers
# ---------------------------------------------------------------------------

def load_location_filters(search_cfg: dict[str, Any] | None = None) -> tuple[list[str], list[str]]:
    """Extract (accept_patterns, reject_patterns) from search config.

    Supports top-level keys (`location_accept` / `location_reject_non_remote`),
    nested `location` (`accept_patterns` / `reject_patterns`), nested `filters`
    (`location_accept` / `location_reject`), and falls back to extracting city/country
    names from the `locations` list when no explicit accept list is provided.
    """
    if search_cfg is None:
        search_cfg = load_search_config()

    loc_block = _string_keyed_mapping(search_cfg.get("location"))
    filters_block = _string_keyed_mapping(search_cfg.get("filters"))
    nested_accept = _string_list(loc_block.get("accept_patterns", []))
    nested_reject = _string_list(loc_block.get("reject_patterns", []))
    filter_accept = _string_list(filters_block.get("location_accept", []))
    filter_reject = _string_list(
        filters_block.get("location_reject", [])
        or filters_block.get("location_reject_non_remote", [])
    )

    accept = (
        _string_list(search_cfg.get("location_accept", []))
        or nested_accept
        or filter_accept
    )
    locations = search_cfg.get("locations")
    if not accept and isinstance(locations, list):
        derived: list[str] = []
        for entry in locations:
            loc_value = entry.get("location", "") if isinstance(entry, dict) else entry
            if not isinstance(loc_value, str):
                continue
            for part in loc_value.split(","):
                token = part.strip().lower()
                if token and token not in derived:
                    derived.append(token)
        accept = derived

    reject = (
        _string_list(search_cfg.get("location_reject_non_remote", []))
        or nested_reject
        or filter_reject
    )
    return accept, reject


def is_location_allowed(
    location: str | None,
    accept: list[str] | None = None,
    reject: list[str] | None = None,
) -> bool:
    """Check if a job location passes the user's location filter.

    - Reject patterns are always checked first (even if 'remote' appears in the text,
      e.g. 'Remote - US Citizen only').
    - Remote jobs ('remote', 'anywhere', 'wfh', 'work from home', 'distributed',
      'home-based', 'telecommute', 'emea') are accepted.
    - If `accept` is empty, all non-rejected locations are accepted (fixes the bug
      where an unconfigured accept list dropped 100% of non-remote jobs).
    - If `accept` is non-empty, the location must match at least one accept pattern.
    """
    if not location:
        return True

    loc = location.lower()
    reject_list = reject or []
    accept_list = accept or []

    # 1. Explicit reject patterns always win
    for r in reject_list:
        if r and r.lower() in loc:
            return False

    # 2. Remote / pan-regional keywords are always accepted
    remote_keywords = (
        "remote", "anywhere", "work from home", "wfh", "distributed",
        "home-based", "home based", "telecommute", "europe", "emea", "eu-wide",
    )
    if any(k in loc for k in remote_keywords):
        return True

    # 3. If no explicit accept filter is configured, accept all non-rejected locations
    if not accept_list:
        return True

    # 4. Otherwise match against accept patterns
    return any(a and a.lower() in loc for a in accept_list)


def is_title_allowed(title: str | None, search_cfg: dict[str, Any] | None = None) -> bool:
    """Check if a job title passes the exclude_titles filter from searches.yaml."""
    if not title:
        return True
    if search_cfg is None:
        search_cfg = load_search_config()
    exclude = _string_list(search_cfg.get("exclude_titles", []))
    if not exclude:
        return True
    title_lower = title.lower()
    return not any(ex.lower() in title_lower for ex in exclude)


# Mapping of UK & European country/city substrings to JobSpy's `country_indeed` parameter
_INDEED_COUNTRY_MAP: list[tuple[tuple[str, ...], str]] = [
    (("united kingdom", "uk", "london", "manchester", "edinburgh", "birmingham", "bristol", "cambridge", "oxford", "leeds", "glasgow", "belfast", "england", "scotland", "wales"), "uk"),
    (("ireland", "dublin", "cork", "galway", "limerick"), "ireland"),
    (("netherlands", "amsterdam", "rotterdam", "eindhoven", "utrecht", "the hague", "den haag"), "netherlands"),
    (("germany", "deutschland", "berlin", "munich", "münchen", "frankfurt", "hamburg", "düsseldorf", "dusseldorf", "stuttgart", "cologne", "köln"), "germany"),
    (("luxembourg", "luxemburg"), "luxembourg"),
    (("switzerland", "schweiz", "zurich", "zürich", "geneva", "genève", "basel", "zug", "bern", "lausanne"), "switzerland"),
    (("france", "paris", "lyon", "toulouse", "marseille", "lille"), "france"),
    (("spain", "españa", "madrid", "barcelona", "málaga", "malaga", "valencia"), "spain"),
    (("portugal", "lisbon", "lisboa", "porto"), "portugal"),
    (("sweden", "stockholm", "gothenburg", "göteborg", "malmö", "malmo"), "sweden"),
    (("denmark", "copenhagen", "københavn", "aarhus"), "denmark"),
    (("norway", "oslo", "bergen"), "norway"),
    (("finland", "helsinki", "espoo", "tampere"), "finland"),
    (("belgium", "brussels", "bruxelles", "antwerp", "ghent"), "belgium"),
    (("austria", "österreich", "vienna", "wien", "graz"), "austria"),
    (("italy", "italia", "milan", "milano", "rome", "roma", "turin"), "italy"),
    (("poland", "polska", "warsaw", "warszawa", "krakow", "kraków", "wroclaw", "wrocław", "gdansk"), "poland"),
    (("czech", "prague", "praha", "brno"), "czech republic"),
    (("romania", "bucharest", "cluj"), "romania"),
    (("hungary", "budapest"), "hungary"),
    (("greece", "athens"), "greece"),
    (("canada", "toronto", "vancouver", "montreal", "ottawa", "calgary"), "canada"),
    (("united states", "usa", "san francisco", "new york", "seattle", "austin"), "usa"),
]


def infer_country_indeed(location: str | None, default: str = "uk") -> str:
    """Infer JobSpy's `country_indeed` parameter from a location string."""
    if not location:
        return default
    loc = location.lower()
    for keywords, country_code in _INDEED_COUNTRY_MAP:
        if any(k in loc for k in keywords):
            return country_code
    return default


# ---------------------------------------------------------------------------
# Default values — referenced across modules instead of magic numbers
# ---------------------------------------------------------------------------

DEFAULTS = {
    "min_score": 7,
    "max_apply_attempts": 3,
    "max_tailor_attempts": 5,
    "poll_interval": 60,
    "apply_timeout": 300,
    "viewport": "1280x900",
}


def load_env() -> None:
    """Load environment variables from profile .env, global ~/.applypilot/.env, and CWD .env."""
    from dotenv import load_dotenv

    _refresh_paths()
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
    if GLOBAL_ENV_PATH.exists() and GLOBAL_ENV_PATH != ENV_PATH:
        load_dotenv(GLOBAL_ENV_PATH)
    load_dotenv()


# ---------------------------------------------------------------------------
# Tier system — feature gating by installed dependencies
# ---------------------------------------------------------------------------

TIER_LABELS = {
    1: "Discovery",
    2: "AI Scoring & Tailoring",
    3: "Full Auto-Apply",
}

TIER_COMMANDS: dict[int, list[str]] = {
    1: ["init", "run discover", "run enrich", "status", "dashboard", "serve", "profiles", "export", "prep", "outreach", "memory"],
    2: ["run score", "run tailor", "run cover", "run pdf", "run"],
    3: ["apply"],
}


def get_tier() -> int:
    """Detect the current tier based on available dependencies.

    Tier 1 (Discovery):            Python + pip
    Tier 2 (AI Scoring & Tailoring): + LLM API key
    Tier 3 (Full Auto-Apply):       + Claude Code CLI + Chrome
    """
    load_env()

    has_llm = any(os.environ.get(k) for k in LLM_ENV_KEYS)
    if not has_llm:
        return 1

    has_claude = shutil.which("claude") is not None
    try:
        get_chrome_path()
        has_chrome = True
    except FileNotFoundError:
        has_chrome = False

    if has_claude and has_chrome:
        return 3

    return 2


def check_tier(required: int, feature: str) -> None:
    """Raise SystemExit with a clear message if the current tier is too low.

    Args:
        required: Minimum tier needed (1, 2, or 3).
        feature: Human-readable description of the feature being gated.
    """
    current = get_tier()
    if current >= required:
        return

    from rich.console import Console
    _console = Console(stderr=True)

    missing: list[str] = []
    if required >= 2 and not any(os.environ.get(k) for k in LLM_ENV_KEYS):
        missing.append("LLM API key — run [bold]applypilot init[/bold] or set GEMINI_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY / GROQ_API_KEY")
    if required >= 3:
        if not shutil.which("claude"):
            missing.append("Claude Code CLI — install from [bold]https://claude.ai/code[/bold]")
        try:
            get_chrome_path()
        except FileNotFoundError:
            missing.append("Chrome/Chromium — install or set CHROME_PATH")

    _console.print(
        f"\n[red]'{feature}' requires {TIER_LABELS.get(required, f'Tier {required}')} (Tier {required}).[/red]\n"
        f"Current tier: {TIER_LABELS.get(current, f'Tier {current}')} (Tier {current})."
    )
    if missing:
        _console.print("\n[yellow]Missing:[/yellow]")
        for m in missing:
            _console.print(f"  - {m}")
    _console.print()
    raise SystemExit(1)


def is_initialized() -> bool:
    """Return True if the active profile has profile.json or resume.txt."""
    _refresh_paths()
    return PROFILE_PATH.exists() or RESUME_PATH.exists()


def require_init() -> None:
    """Ensure the active profile workspace directories exist."""
    ensure_dirs()


def has_llm_configured() -> bool:
    """Return True if an LLM API key or local endpoint URL is configured."""
    load_env()
    return any(os.environ.get(k) for k in LLM_ENV_KEYS)


def require_tier(required: int, feature: str = "this command") -> None:
    """Alias for check_tier()."""
    check_tier(required, feature)

