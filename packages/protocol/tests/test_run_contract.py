from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from workflow_protocol import ActionRunEvent, RunRequestSnapshot
from workflow_protocol.models import WorkflowDefinition


def workflow() -> WorkflowDefinition:
    fixture = Path(__file__).parents[1] / "fixtures" / "workflow_base.json"
    return WorkflowDefinition.model_validate(
        json.loads(fixture.read_text(encoding="utf-8"))
    )


def test_run_snapshot_freezes_matching_profile() -> None:
    requested = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    saved_workflow = workflow()
    snapshot = RunRequestSnapshot.model_validate(
        {
            "schema_version": 1,
            "run_id": "run_example000001",
            "workflow": saved_workflow,
            "verification_profile": {
                "profile_id": "helpdesk-linked-tickets-v1",
                "version": 1,
                "content_hash": saved_workflow.verification_profile.content_hash,
                "review_note": "Demo review completed.",
                "completion_note": "Demo completion recorded.",
                "actor_username": "workflow_actor",
            },
            "requested_at": requested,
            "expires_at": requested + timedelta(seconds=60),
        }
    )

    assert snapshot.workflow.actions[0].kind == "fill"
    with pytest.raises(ValidationError):
        snapshot.run_id = "changed"


def test_run_snapshot_rejects_profile_hash_mismatch() -> None:
    requested = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    saved_workflow = workflow()
    with pytest.raises(ValidationError, match="does not match workflow"):
        RunRequestSnapshot.model_validate(
            {
                "schema_version": 1,
                "run_id": "run_example000001",
                "workflow": saved_workflow,
                "verification_profile": {
                    "profile_id": "helpdesk-linked-tickets-v1",
                    "version": 1,
                    "content_hash": "f" * 64,
                    "review_note": "Demo review completed.",
                    "completion_note": "Demo completion recorded.",
                    "actor_username": "workflow_actor",
                },
                "requested_at": requested,
                "expires_at": requested + timedelta(seconds=60),
            }
        )


def test_general_workflow_snapshot_has_no_business_verifier() -> None:
    requested = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    workflow_data = workflow().model_dump(mode="json")
    workflow_data.update(
        schema_version=2,
        workflow_mode="general",
        category="Ticket intake",
        verification_profile=None,
    )
    general_workflow = WorkflowDefinition.model_validate(workflow_data)

    snapshot = RunRequestSnapshot.model_validate(
        {
            "schema_version": 1,
            "run_id": "run_general0000001",
            "workflow": general_workflow,
            "verification_profile": None,
            "requested_at": requested,
            "expires_at": requested + timedelta(seconds=60),
        }
    )

    assert snapshot.workflow.category == "Ticket intake"
    assert snapshot.workflow.category_id is None
    assert "category_id" not in snapshot.workflow.model_dump(mode="json")
    assert snapshot.verification_profile is None


def test_schema_v3_freezes_category_identity() -> None:
    workflow_data = workflow().model_dump(mode="json")
    workflow_data.update(
        schema_version=3,
        workflow_mode="general",
        category_id="cat_ticket_intake",
        category="Ticket intake",
        verification_profile=None,
    )

    saved = WorkflowDefinition.model_validate(workflow_data)

    assert saved.category_id == "cat_ticket_intake"
    with pytest.raises(ValidationError, match="requires a category ID"):
        WorkflowDefinition.model_validate({**workflow_data, "category_id": None})


def test_workflow_mode_cannot_misrepresent_verification() -> None:
    workflow_data = workflow().model_dump(mode="json")
    workflow_data["schema_version"] = 2
    workflow_data["workflow_mode"] = "general"
    workflow_data["category"] = "Ticket intake"
    with pytest.raises(ValidationError, match="general workflow cannot declare"):
        WorkflowDefinition.model_validate(workflow_data)

    workflow_data["verification_profile"] = None
    workflow_data["category"] = "Linked-ticket resolution"
    with pytest.raises(ValidationError, match="reserved preset category"):
        WorkflowDefinition.model_validate(workflow_data)

    workflow_data.update(
        schema_version=3,
        category="Ticket intake",
        category_id="cat_linked_tickets_v1",
    )
    with pytest.raises(ValidationError, match="reserved preset category ID"):
        WorkflowDefinition.model_validate(workflow_data)


def test_legacy_workflow_wire_shape_remains_immutable() -> None:
    legacy_data = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "workflow_base.json").read_text(
            encoding="utf-8"
        )
    )
    serialized = workflow().model_dump(mode="json")

    assert serialized["content_hash"] == legacy_data["content_hash"]
    assert "workflow_mode" not in serialized
    assert "category_id" not in serialized
    assert "category" not in serialized
    for action in serialized["actions"]:
        page = action.get("destination", action.get("page"))
        assert "query" not in page


def test_action_event_requires_pending_intent_and_terminal_result() -> None:
    base = {
        "kind": "action",
        "action_id": "act_example000001",
        "action_sequence": 0,
        "stage": "intent",
        "status": "pending",
        "observed_at": "2026-09-14T12:00:00Z",
    }
    ActionRunEvent.model_validate(base)

    invalid = deepcopy(base)
    invalid["status"] = "completed"
    with pytest.raises(ValidationError, match="intent must have pending"):
        ActionRunEvent.model_validate(invalid)

    invalid = deepcopy(base)
    invalid.update(stage="result", status="pending")
    with pytest.raises(ValidationError, match="result cannot have pending"):
        ActionRunEvent.model_validate(invalid)