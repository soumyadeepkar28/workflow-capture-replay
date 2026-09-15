import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from workflow_protocol import PageIdentity, WorkflowDefinition


FIXTURE_DIR = Path(__file__).parents[1] / "fixtures"


def load_json(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def apply_changes(base: dict[str, Any], changes: list[dict[str, Any]]) -> dict[str, Any]:
    result = deepcopy(base)
    for change in changes:
        path = change["path"]
        cursor: Any = result
        for segment in path[:-1]:
            cursor = cursor[segment]
        cursor[path[-1]] = change["value"]
    return result


CASES = load_json("workflow_cases.json")["cases"]


def test_valid_workflow_is_immutable_and_normalizes_name() -> None:
    data = load_json("workflow_base.json")
    data["name"] = "  Resolve linked tickets  "

    workflow = WorkflowDefinition.model_validate(data)

    assert workflow.name == "Resolve linked tickets"
    assert workflow.actions[0].kind == "fill"
    with pytest.raises(ValidationError):
        workflow.name = "Changed"


def test_ticket_list_query_is_canonical_and_bounded() -> None:
    identity = PageIdentity(
        target_alias="helpdesk-demo",
        path="/tickets/",
        query="status=1&status=2&q=Access+request&search_type=header",
    )
    assert identity.query == "status=1&status=2&q=Access+request&search_type=header"

    with pytest.raises(ValidationError, match="unsupported Helpdesk filter"):
        PageIdentity(
            target_alias="helpdesk-demo",
            path="/tickets/",
            query="next=https%3A%2F%2Fexample.invalid",
        )
    with pytest.raises(ValidationError, match="only on the Helpdesk ticket list"):
        PageIdentity(
            target_alias="helpdesk-demo",
            path="/tickets/1/",
            query="q=review",
        )


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_shared_conformance_cases(case: dict[str, Any]) -> None:
    data = apply_changes(load_json("workflow_base.json"), case["changes"])

    if case["expected_valid"]:
        WorkflowDefinition.model_validate(data)
        return

    with pytest.raises(ValidationError, match=case["python_error"]):
        WorkflowDefinition.model_validate(data)