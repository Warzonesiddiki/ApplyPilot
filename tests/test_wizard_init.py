"""Wizard initialization steps driven by scripted prompts."""

from __future__ import annotations

import json
import platform
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import applypilot.config as config
import applypilot.wizard.init as wizard_init


class _ScriptedPrompts:
    """Pops queued answers for Prompt.ask/Confirm.ask and records the questions."""

    def __init__(self, answers: list[str], confirm_answers: list[bool]) -> None:
        self._answers = list(answers)
        self._confirm_answers = list(confirm_answers)
        self.questions: list[str] = []

    def ask(self, prompt: str, **kwargs: Any) -> str:
        self.questions.append(prompt)
        return self._answers.pop(0)

    def confirm(self, prompt: str, **kwargs: Any) -> bool:
        self.questions.append(prompt)
        return self._confirm_answers.pop(0)


@pytest.fixture()
def wizard_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path))
    config.set_active_profile("wizard-tests")
    config.ensure_dirs()
    yield tmp_path
    config.set_active_profile(None)


def test_setup_resume_retries_missing_file_then_copies_txt(
    wizard_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = wizard_env / "source_resume.txt"
    source.write_text("Neha Tariq, ACA. Audit experience.", encoding="utf-8")
    scripted = _ScriptedPrompts(
        [str(wizard_env / "does-not-exist.txt"), str(source)],
        [],
    )
    monkeypatch.setattr(wizard_init, "Prompt", type("P", (), {"ask": staticmethod(scripted.ask)}))

    wizard_init._setup_resume()

    assert config.RESUME_PATH.read_text(encoding="utf-8") == "Neha Tariq, ACA. Audit experience."
    # The invalid path was rejected and re-asked before the copy happened.
    assert scripted.questions.count("Resume file path") == 2


def test_setup_profile_walks_the_finance_domain_and_saves_restricted_json(
    wizard_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripted = _ScriptedPrompts(
        [
            "finance_accounting",  # domain
            "Neha Tariq",  # full name
            "",  # preferred name
            "neha@example.com",  # email
            "",  # phone
            "London",  # city
            "",  # province
            "United Kingdom",  # country
            "",  # postal code
            "",  # address
            "",  # linkedin
            "",  # github
            "",  # portfolio
            "",  # website
            "",  # job-site password
            "UK Skilled Worker",  # work permit
            "70000",  # salary
            "GBP",  # currency
            "60000-90000",  # range
            "Audit Senior",  # current title
            "Audit Manager",  # target role
            "8",  # years of experience
            "ACA Chartered Accountant",  # education
            "IFRS, UK GAAP",  # standards
            "External Audit, ISA",  # audit
            "FP&A, Forecasting",  # fpa
            "Advanced Excel, Power BI",  # erp
            "PwC UK",  # companies
            "",  # projects
            "ACA",  # certifications
            "ICAEW",  # school
            "£50M audit scope",  # metrics
            "1 month",  # earliest start
            "executive_uk_eu",  # pdf theme
        ],
        [True, True, False],
    )
    monkeypatch.setattr(
        wizard_init, "Prompt", type("P", (), {"ask": staticmethod(scripted.ask)})
    )
    monkeypatch.setattr(
        wizard_init, "Confirm", type("C", (), {"ask": staticmethod(scripted.confirm)})
    )

    profile = wizard_init._setup_profile()

    assert profile["domain"] == "finance_accounting"
    assert profile["personal"]["full_name"] == "Neha Tariq"
    assert profile["personal"]["email"] == "neha@example.com"
    assert profile["work_authorization"] == {
        "legally_authorized_to_work": "Yes",
        "require_sponsorship": "Yes",
        "willing_to_relocate": "No",
        "work_permit_type": "UK Skilled Worker",
    }
    assert profile["compensation"] == {
        "salary_expectation": "70000",
        "salary_currency": "GBP",
        "salary_range_min": "60000",
        "salary_range_max": "90000",
    }
    assert profile["experience"]["target_role"] == "Audit Manager"
    assert profile["skills_boundary"]["Accounting & Reporting Standards"] == ["IFRS", "UK GAAP"]
    assert profile["resume_facts"]["certifications"] == ["ACA"]
    assert profile["resume_facts"]["real_metrics"] == ["£50M audit scope"]
    assert profile["pdf_theme"] == "executive_uk_eu"
    assert profile["eeo_voluntary"]["gender"] == "Decline to self-identify"

    saved = json.loads(config.PROFILE_PATH.read_text(encoding="utf-8"))
    assert saved == profile
    # Every queued answer was consumed by a matching question.
    assert scripted.questions and not scripted._answers


# -- Step 3: Search config ----------------------------------------------------


def test_setup_searches_writes_yaml_with_queries_and_tiers(
    wizard_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripted = _ScriptedPrompts(
        ["Amsterdam, Netherlands", "0", "AI Engineer, Chartered Accountant ACA"],
        [],
    )
    monkeypatch.setattr(wizard_init, "Prompt", type("P", (), {"ask": staticmethod(scripted.ask)}))

    wizard_init._setup_searches()

    import yaml

    saved = yaml.safe_load(config.SEARCH_CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["defaults"] == {
        "location": "Amsterdam, Netherlands", "distance": 0, "hours_old": 72, "results_per_site": 50,
    }
    assert saved["locations"] == [{"location": "Amsterdam, Netherlands", "remote": True}]
    assert [q["query"] for q in saved["queries"]] == ["AI Engineer", "Chartered Accountant ACA"]
    assert [q["tier"] for q in saved["queries"]] == [1, 2]


def test_setup_searches_falls_back_on_bad_distance_and_empty_roles(
    wizard_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripted = _ScriptedPrompts(["London, United Kingdom", "not-a-number", ""], [])
    monkeypatch.setattr(wizard_init, "Prompt", type("P", (), {"ask": staticmethod(scripted.ask)}))

    wizard_init._setup_searches()

    import yaml

    saved = yaml.safe_load(config.SEARCH_CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["defaults"]["distance"] == 25  # unparsable radius falls back
    assert saved["locations"][0]["remote"] is False
    assert saved["queries"] == [{"query": "Software Engineer", "tier": 1}]


# -- Step 4: AI features ------------------------------------------------------


def test_setup_ai_features_gemini_branch_writes_restricted_env(
    wizard_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripted = _ScriptedPrompts(
        ["gemini", "AIza-test-key-123", "gemini-2.0-flash"],
        [True],
    )
    monkeypatch.setattr(wizard_init, "Prompt", type("P", (), {"ask": staticmethod(scripted.ask)}))
    monkeypatch.setattr(wizard_init, "Confirm", type("C", (), {"ask": staticmethod(scripted.confirm)}))

    wizard_init._setup_ai_features()

    env = config.ENV_PATH.read_text(encoding="utf-8")
    assert "GEMINI_API_KEY=AIza-test-key-123" in env
    assert "LLM_MODEL=gemini-2.0-flash" in env
    if platform.system() != "Windows":
        assert (config.ENV_PATH.stat().st_mode & 0o777) == 0o600


def test_setup_ai_features_declined_leaves_env_untouched(
    wizard_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripted = _ScriptedPrompts([], [False])
    monkeypatch.setattr(wizard_init, "Confirm", type("C", (), {"ask": staticmethod(scripted.confirm)}))

    wizard_init._setup_ai_features()

    assert not config.ENV_PATH.exists()


def test_setup_ai_features_local_provider_records_endpoint(
    wizard_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripted = _ScriptedPrompts(["local", "http://localhost:11434/v1", "llama3"], [True])
    monkeypatch.setattr(wizard_init, "Prompt", type("P", (), {"ask": staticmethod(scripted.ask)}))
    monkeypatch.setattr(wizard_init, "Confirm", type("C", (), {"ask": staticmethod(scripted.confirm)}))

    wizard_init._setup_ai_features()

    env = config.ENV_PATH.read_text(encoding="utf-8")
    assert "LLM_URL=http://localhost:11434/v1" in env
    assert "LLM_MODEL=llama3" in env


# -- Step 5: Auto-apply -------------------------------------------------------


def test_setup_auto_apply_declined_writes_nothing(
    wizard_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripted = _ScriptedPrompts([], [False])
    monkeypatch.setattr(wizard_init, "Confirm", type("C", (), {"ask": staticmethod(scripted.confirm)}))

    wizard_init._setup_auto_apply()

    assert not config.ENV_PATH.exists()


def test_setup_auto_apply_saves_capsolver_key_into_existing_env(
    wizard_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config.secure_write_text(config.ENV_PATH, "# ApplyPilot configuration\n\nLLM_MODEL=gemini-2.0-flash\n")
    scripted = _ScriptedPrompts(["cap-key-abc"], [True, True])
    monkeypatch.setattr(wizard_init, "Prompt", type("P", (), {"ask": staticmethod(scripted.ask)}))
    monkeypatch.setattr(wizard_init, "Confirm", type("C", (), {"ask": staticmethod(scripted.confirm)}))
    monkeypatch.setattr("applypilot.wizard.init.shutil.which", lambda name: None)  # Claude CLI absent

    wizard_init._setup_auto_apply()

    env = config.ENV_PATH.read_text(encoding="utf-8")
    assert "CAPSOLVER_API_KEY=cap-key-abc" in env
    assert "LLM_MODEL=gemini-2.0-flash" in env  # existing content preserved

    # A second run with the same key must not duplicate the entry.
    scripted_again = _ScriptedPrompts(["cap-key-abc"], [True, True])
    monkeypatch.setattr(wizard_init, "Prompt", type("P", (), {"ask": staticmethod(scripted_again.ask)}))
    monkeypatch.setattr(wizard_init, "Confirm", type("C", (), {"ask": staticmethod(scripted_again.confirm)}))
    wizard_init._setup_auto_apply()
    assert config.ENV_PATH.read_text(encoding="utf-8").count("CAPSOLVER_API_KEY") == 1


def test_setup_auto_apply_creates_env_when_missing(
    wizard_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripted = _ScriptedPrompts(["fresh-key"], [True, True])
    monkeypatch.setattr(wizard_init, "Prompt", type("P", (), {"ask": staticmethod(scripted.ask)}))
    monkeypatch.setattr(wizard_init, "Confirm", type("C", (), {"ask": staticmethod(scripted.confirm)}))
    monkeypatch.setattr("applypilot.wizard.init.shutil.which", lambda name: "/usr/bin/claude")  # Claude CLI present

    wizard_init._setup_auto_apply()

    env = config.ENV_PATH.read_text(encoding="utf-8")
    assert "CAPSOLVER_API_KEY=fresh-key" in env
