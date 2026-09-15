import asyncio
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from workflow_companion.replay import (
    accessible_name_pattern,
    CheckResult,
    ReplayExecutionError,
    ReplayExecutor,
    classify_outcome,
    resolve_page_path,
    resolve_page_url,
    select_option_label,
    target_request_allowed,
)
from workflow_protocol import SelectAction
from workflow_protocol import PageIdentity


def check(
    check_id: str,
    status: str,
    *,
    critical: bool = False,
    milestone: bool = False,
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        status=status,  # type: ignore[arg-type]
        expected="expected",
        actual="actual",
        critical=critical,
        milestone=milestone,
    )


def test_outcome_precedence_is_conservative() -> None:
    assert classify_outcome([check("baseline", "passed")]) == "failed"
    assert classify_outcome(
        [
            check("final_review", "passed"),
            check("final_completion_status", "passed"),
            check("final_completion_note", "passed"),
            check("final_completion_owner", "passed"),
            check("final_dependency", "passed", critical=True),
        ]
    ) == "succeeded"
    assert classify_outcome(
        [check("review", "passed", milestone=True), check("completion", "failed")]
    ) == "partially_succeeded"
    assert classify_outcome(
        [check("review", "passed", milestone=True), check("identity", "failed", critical=True)]
    ) == "failed"
    assert classify_outcome(
        [check("review", "passed", milestone=True)],
        outcome_determining_uncertainty=True,
    ) == "uncertain"


def test_logical_ticket_mapping_overrides_captured_local_id() -> None:
    identity = PageIdentity(
        target_alias="helpdesk-demo",
        path="/tickets/999/",
        record_ref="review",
    )
    assert resolve_page_path(identity, {"review": 4, "completion": 5}) == "/tickets/4/"


def test_general_ticket_routes_and_search_query_are_bounded() -> None:
    identity = PageIdentity(
        target_alias="helpdesk-demo",
        path="/tickets/",
        query="status=1&q=Access+request",
    )
    assert resolve_page_url(identity, {}) == "/tickets/?status=1&q=Access+request"
    assert target_request_allowed("GET", "http://127.0.0.1:8765/tickets/?q=Access")
    assert target_request_allowed(
        "GET",
        "http://127.0.0.1:8765/tickets/?q=Access&csrfmiddlewaretoken=secret",
    )
    assert not target_request_allowed(
        "GET",
        "http://127.0.0.1:8765/tickets/?csrfmiddlewaretoken=one&csrfmiddlewaretoken=two",
    )
    assert not target_request_allowed(
        "GET",
        f"http://127.0.0.1:8765/tickets/?csrfmiddlewaretoken={'x' * 201}",
    )
    assert target_request_allowed("POST", "http://127.0.0.1:8765/tickets/submit/")
    assert target_request_allowed("POST", "http://127.0.0.1:8765/tickets/3/update/")
    assert target_request_allowed(
        "GET",
        "http://127.0.0.1:8765/datatables_ticket_list/ZXhhbXBsZQ==?draw=1",
    )
    assert not target_request_allowed("POST", "http://127.0.0.1:8765/tickets/?q=Access")
    assert not target_request_allowed("GET", "http://127.0.0.1:8765/tickets/3/delete/")
    assert not target_request_allowed("GET", "http://127.0.0.1:8765/admin/")


def test_recorded_accessible_names_tolerate_only_whitespace_variation() -> None:
    pattern = accessible_name_pattern("New Ticket")
    assert pattern.fullmatch(" New  \n Ticket ")
    assert pattern.fullmatch("\uf055 New Ticket")
    assert not pattern.fullmatch("Create New Ticket Now")


def test_actor_fixture_binding_does_not_require_a_verification_profile() -> None:
    action = SelectAction.model_validate(
        {
            "action_id": "act_actorbinding01",
            "sequence": 0,
            "kind": "select",
            "page": {
                "target_alias": "helpdesk-demo",
                "path": "/tickets/1/",
            },
            "locator": {
                "strategy": "label",
                "label": "Assigned to",
            },
            "option_label": "workflow_actor",
            "fixture_ref": "actor",
        }
    )

    assert select_option_label(action, None) == "workflow_actor"


def test_target_command_failure_preserves_bounded_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    diagnostic = "x" * 1_400
    monkeypatch.setattr(
        "workflow_companion.replay.subprocess.run",
        lambda *args, **kwargs: CompletedProcess(args[0], 1, "", diagnostic),
    )
    executor = ReplayExecutor(None, tmp_path)  # type: ignore[arg-type]

    with pytest.raises(ReplayExecutionError) as caught:
        executor.target._command("stop")

    assert "target stop failed with exit code 1" in str(caught.value)
    assert str(caught.value).endswith("x" * 1_200)
    assert len(str(caught.value)) < 1_300


def test_replay_pacing_applies_only_to_headed_browser(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    delays: list[float] = []

    async def record_delay(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("workflow_companion.replay.asyncio.sleep", record_delay)
    headed = ReplayExecutor(
        None,  # type: ignore[arg-type]
        tmp_path,
        headless=False,
        step_delay_seconds=0.9,
    )
    headless = ReplayExecutor(
        None,  # type: ignore[arg-type]
        tmp_path,
        headless=True,
        step_delay_seconds=0.9,
    )

    asyncio.run(headed._pace(headed.step_delay_seconds))
    asyncio.run(headless._pace(headless.step_delay_seconds))

    assert delays == [0.9]
    with pytest.raises(ValueError, match="step delay"):
        ReplayExecutor(None, tmp_path, step_delay_seconds=3.1)  # type: ignore[arg-type]