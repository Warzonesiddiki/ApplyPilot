"""Worker-state unit tests for the Rich apply dashboard."""

from __future__ import annotations

import applypilot.apply.dashboard as dashboard
from applypilot.apply.dashboard import (
    MAX_EVENTS,
    WorkerState,
    add_event,
    get_state,
    get_totals,
    init_worker,
    render_dashboard,
    render_full,
    update_state,
)
from rich.console import Group
from rich.table import Table


def setup_function(_function: object) -> None:
    dashboard._worker_states.clear()
    dashboard._events.clear()


def test_worker_lifecycle_register_update_and_read() -> None:
    init_worker(0)
    state = get_state(0)
    assert state is not None
    assert state.status == "starting"

    update_state(0, status="applying", job_title="Audit Manager", company="PwC UK", actions=3)
    updated = get_state(0)
    assert updated is not None
    assert updated.status == "applying"
    assert updated.job_title == "Audit Manager"
    assert updated.actions == 3

    # Unknown workers are ignored on update and absent on read.
    update_state(99, status="applying")
    assert get_state(99) is None


def test_event_log_is_bounded_to_the_last_max_events() -> None:
    for index in range(MAX_EVENTS * 3):
        add_event(f"event-{index}")

    assert len(dashboard._events) == MAX_EVENTS
    assert "event-0" not in " ".join(dashboard._events)
    assert any("event-23" in entry for entry in dashboard._events)


def test_render_dashboard_lists_workers_and_totals() -> None:
    init_worker(0)
    init_worker(1)
    update_state(0, status="applied", job_title="Audit Manager", company="PwC UK", jobs_applied=4, jobs_failed=1, total_cost=0.25)
    update_state(1, status="failed", job_title="AI Engineer", company="DeepTech", jobs_applied=1, jobs_failed=2, total_cost=0.125)

    table = render_dashboard()
    assert isinstance(table, Table)
    from rich.console import Console

    capture = Console(record=True, width=200)
    capture.print(table)
    rendered = capture.export_text()
    assert "Audit Manager @ PwC UK" in rendered

    totals = get_totals()
    assert totals == {"applied": 5, "failed": 3, "cost": 0.375}


def test_render_full_includes_events_panel_only_when_events_exist() -> None:
    init_worker(0)
    plain = render_full()
    assert isinstance(plain, Table)

    add_event("worker applied to a role")
    full = render_full()
    assert isinstance(full, Group)
    assert len(full.renderables) == 2


def test_totals_with_no_workers_registered() -> None:
    assert get_totals() == {"applied": 0, "failed": 0, "cost": 0}
    assert render_dashboard() is not None


def test_worker_state_defaults() -> None:
    state = WorkerState(worker_id=7)
    assert state.worker_id == 7
    assert state.status == "starting"
    assert state.jobs_applied == 0
    assert state.log_file is None
