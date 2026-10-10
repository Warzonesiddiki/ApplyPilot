"""Tests for bundled YAML configs (employers.yaml, sites.yaml) and candidate profile templates."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from applypilot import config

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_employers_yaml_includes_uk_and_eu_employers() -> None:
    employers = config.load_employers()
    emp_list = list(employers.values()) if isinstance(employers, dict) else list(employers)
    assert len(emp_list) >= 30
    names = {e["name"] for e in emp_list}
    assert "Barclays" in names
    assert "NatWest Group" in names
    assert "AstraZeneca" in names
    assert "ING" in names
    assert "ASML" in names
    assert "Unilever" in names
    assert "Grant Thornton" in names


def test_sites_yaml_includes_uk_and_eu_boards() -> None:
    sites_cfg = config.load_sites_config()
    sites = sites_cfg.get("sites", [])
    names = {s["name"] for s in sites}
    assert "Reed UK" in names
    assert "eFinancialCareers UK" in names
    assert "ICAEW Jobs" in names
    assert "ACCA Careers" in names
    assert "Relocate.me" in names
    assert "EuroTechJobs" in names


def test_candidate_templates_and_search_configs_valid() -> None:
    profiles_dir = REPO_ROOT / "docs" / "profiles"

    tahir_json = json.loads((profiles_dir / "tahir_siddique.template.json").read_text(encoding="utf-8"))
    neha_json = json.loads((profiles_dir / "neha_tariq_aca.template.json").read_text(encoding="utf-8"))
    tahir_yaml = yaml.safe_load((profiles_dir / "searches_tahir_eu_uk.yaml").read_text(encoding="utf-8"))
    neha_yaml = yaml.safe_load((profiles_dir / "searches_neha_aca_eu_uk.yaml").read_text(encoding="utf-8"))

    assert tahir_json["personal"]["full_name"] == "Tahir Siddique"
    assert "Neha Tariq" in neha_json["personal"]["full_name"]
    assert any("ACA" in c for c in neha_json["resume_facts"]["certifications"])

    assert len(tahir_yaml["locations"]) >= 10
    assert len(tahir_yaml["queries"]) >= 12
    assert len(neha_yaml["locations"]) >= 10
    assert len(neha_yaml["queries"]) >= 14
