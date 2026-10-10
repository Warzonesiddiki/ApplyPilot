"""Regression tests for defensive loaders and typed boundary helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from applypilot import config
from applypilot.email_sync import _decode_mime_payload
from applypilot.wizard.importer import read_resume_file


def test_resume_reader_supports_wizard_text_inputs(tmp_path: Path) -> None:
    resume = tmp_path / "resume.md"
    resume.write_text("Candidate\nSenior Accountant", encoding="utf-8")

    assert read_resume_file(resume) == "Candidate\nSenior Accountant"


def test_mime_payload_decoder_handles_bytes_text_and_nested_payloads() -> None:
    assert _decode_mime_payload(b"Hiring update") == "Hiring update"
    assert _decode_mime_payload("Interview invitation") == "Interview invitation"
    assert _decode_mime_payload(object()) == ""
    assert _decode_mime_payload(b"invalid: \xff") == "invalid: \ufffd"


def test_invalid_yaml_registries_return_empty_typed_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    (tmp_path / "employers.yaml").write_text(
        "employers: not-a-mapping\nats_employers: also-not-a-mapping\n",
        encoding="utf-8",
    )
    (tmp_path / "sites.yaml").write_text("- invalid\n- root\n", encoding="utf-8")

    assert config.load_employers() == []
    assert config.load_ats_employers() == {}
    assert config.load_sites_config() == {}


def test_location_filter_single_strings_are_not_split_into_characters() -> None:
    accept, reject = config.load_location_filters(
        {"location_accept": "London", "location_reject_non_remote": "US citizen only"}
    )

    assert accept == ["London"]
    assert reject == ["US citizen only"]
