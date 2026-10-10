"""Tests for multi-profile workspace resolution, EU/UK location filtering, and country_indeed mapping."""

from __future__ import annotations

from pathlib import Path

import pytest

import applypilot.config as config


def test_profile_switching_and_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each profile slug resolves to an isolated workspace directory."""
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))

    # Default profile
    config.set_active_profile(None)
    assert config.get_active_profile() is None
    assert config.APP_DIR == tmp_path
    assert config.DB_PATH == tmp_path / "applypilot.db"

    # Switch to Tahir profile
    tahir_dir = config.set_active_profile("tahir")
    assert config.get_active_profile() == "tahir"
    assert tahir_dir == tmp_path / "profiles" / "tahir"
    assert config.PROFILE_PATH == tahir_dir / "profile.json"
    assert config.DB_PATH == tahir_dir / "applypilot.db"
    config.ensure_dirs()
    assert config.TAILORED_DIR.exists()

    # Switch to Neha profile
    neha_dir = config.set_active_profile("neha")
    assert config.get_active_profile() == "neha"
    assert neha_dir == tmp_path / "profiles" / "neha"
    assert config.PROFILE_PATH == neha_dir / "profile.json"
    assert config.DB_PATH != tahir_dir / "applypilot.db"
    config.ensure_dirs()

    # List profiles
    profiles = config.list_profiles()
    names = {p["name"] for p in profiles}
    assert {"default", "tahir", "neha"}.issubset(names)

    # Reset back to default
    config.set_active_profile(None)


def test_infer_country_indeed_uk_and_eu() -> None:
    """Locations across UK and EU resolve to their exact JobSpy country_indeed string."""
    assert config.infer_country_indeed("London, United Kingdom") == "uk"
    assert config.infer_country_indeed("Manchester, England") == "uk"
    assert config.infer_country_indeed("Edinburgh, Scotland") == "uk"
    assert config.infer_country_indeed("Dublin, Ireland") == "ireland"
    assert config.infer_country_indeed("Amsterdam, Netherlands") == "netherlands"
    assert config.infer_country_indeed("Berlin, Germany") == "germany"
    assert config.infer_country_indeed("Munich, Germany") == "germany"
    assert config.infer_country_indeed("Luxembourg") == "luxembourg"
    assert config.infer_country_indeed("Zurich, Switzerland") == "switzerland"
    assert config.infer_country_indeed("Paris, France") == "france"
    assert config.infer_country_indeed("Madrid, Spain") == "spain"
    assert config.infer_country_indeed("Stockholm, Sweden") == "sweden"
    assert config.infer_country_indeed("Copenhagen, Denmark") == "denmark"
    assert config.infer_country_indeed("Brussels, Belgium") == "belgium"
    assert config.infer_country_indeed("Warsaw, Poland") == "poland"


def test_location_filters_fallback_to_locations_list() -> None:
    """When filters.location_accept is omitted, locations list in searches.yaml is used."""
    search_cfg = {
        "locations": [
            {"location": "London, United Kingdom", "remote": False},
            {"location": "Amsterdam, Netherlands", "remote": False},
            {"location": "Dublin, Ireland", "remote": False},
        ],
        "filters": {
            "location_reject": ["US citizens only", "Security Clearance Required"],
        },
    }
    accept, reject = config.load_location_filters(search_cfg)
    assert "london" in accept
    assert "united kingdom" in accept
    assert "amsterdam" in accept
    assert "netherlands" in accept
    assert "dublin" in accept

    # Non-remote UK/EU jobs in target hubs MUST be accepted (fixing Blocker B4)
    assert config.is_location_allowed("London, United Kingdom (Hybrid)", accept, reject) is True
    assert config.is_location_allowed("Amsterdam, Netherlands", accept, reject) is True
    assert config.is_location_allowed("Remote - EMEA", accept, reject) is True
    # Rejected pattern
    assert config.is_location_allowed("London - Security Clearance Required", accept, reject) is False
    # Out-of-region onsite role
    assert config.is_location_allowed("Tokyo, Japan", accept, reject) is False
