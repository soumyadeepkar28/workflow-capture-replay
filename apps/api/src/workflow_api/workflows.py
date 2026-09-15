from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from workflow_protocol import (
    CaptureProvenance,
    HelpdeskVerificationProfile,
    RunRequestSnapshot,
    WorkflowDefinition,
)

PROFILE_ID = "helpdesk-linked-tickets-v1"
PROFILE_VERSION = 1
PROFILE_DATA = {
    "profile_id": PROFILE_ID,
    "version": PROFILE_VERSION,
    "starting_conditions": [
        "review_open_unassigned",
        "completion_open_unassigned",
        "dependency_unchanged",
        "expected_notes_absent",
    ],
    "prerequisite": "review_resolved_with_expected_note",
    "final_conditions": [
        "review_resolved_with_expected_note",
        "completion_resolved_with_expected_note",
        "completion_assigned_to_actor",
        "dependency_unchanged",
    ],
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


PROFILE_HASH = content_hash(PROFILE_DATA)


def verification_profile() -> HelpdeskVerificationProfile:
    return HelpdeskVerificationProfile(
        profile_id=PROFILE_ID,
        version=PROFILE_VERSION,
        content_hash=PROFILE_HASH,
        review_note="Demo review completed.",
        completion_note="Demo completion recorded.",
        actor_username="workflow_actor",
    )


def build_run_snapshot(
    *,
    run_id: str,
    workflow: WorkflowDefinition,
    requested_at: int,
    expires_at: int,
) -> RunRequestSnapshot:
    return RunRequestSnapshot(
        schema_version=1,
        run_id=run_id,
        workflow=workflow,
        verification_profile=(
            verification_profile() if workflow.verification_profile is not None else None
        ),
        requested_at=datetime.fromtimestamp(requested_at, tz=timezone.utc),
        expires_at=datetime.fromtimestamp(expires_at, tz=timezone.utc),
    )


def build_workflow_definition(
    *,
    workflow_id: str,
    created_at: int,
    name: str,
    description: str,
    capture: CaptureProvenance,
    actions: list[dict[str, Any]],
    category_id: str,
    category: str,
    category_verification_profile: HelpdeskVerificationProfile | None,
) -> WorkflowDefinition:
    created_at_value = datetime.fromtimestamp(created_at, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    workflow_mode = (
        "verified_preset" if category_verification_profile is not None else "general"
    )
    unsigned = {
        "schema_version": 3,
        "workflow_id": workflow_id,
        "created_at": created_at_value,
        "name": name,
        "description": description,
        "target_alias": "helpdesk-demo",
        "workflow_mode": workflow_mode,
        "category_id": category_id,
        "category": category,
        "capture": capture.model_dump(mode="json"),
        "verification_profile": (
            {
                "profile_id": category_verification_profile.profile_id,
                "version": category_verification_profile.version,
                "content_hash": category_verification_profile.content_hash,
            }
            if category_verification_profile is not None
            else None
        ),
        "actions": actions,
    }
    return WorkflowDefinition.model_validate(
        {**unsigned, "content_hash": content_hash(unsigned)}
    )