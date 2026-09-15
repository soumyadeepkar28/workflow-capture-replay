from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from workflow_protocol import (
    Action,
    BusinessOutcome,
    CaptureProvenance,
    HelpdeskVerificationProfile,
    RunEventEnvelope,
    RunPhase,
    RunRequestSnapshot,
    VERIFIED_PRESET_CATEGORY,
    WorkflowDefinition,
    WorkflowMode,
)

Digest = Field(pattern=r"^[a-f0-9]{64}$")


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunnerSummary(ApiModel):
    runner_id: str
    label: str
    readiness: Literal["offline", "ready", "busy", "recovery_required"]
    last_seen_at: int | None
    companion_version: str
    protocol_version: int


class SessionResponse(ApiModel):
    csrf_token: str
    expires_at: int
    runner: RunnerSummary | None


class PairingInitiateRequest(ApiModel):
    label: str = Field(min_length=1, max_length=80)
    pending_proof_digest: str = Digest
    runner_credential_digest: str = Digest
    companion_version: str = Field(min_length=1, max_length=40)
    protocol_version: Literal[1]


class PairingInitiateResponse(ApiModel):
    pairing_id: str
    pairing_url: str
    expires_at: int


class PairingTicketRequest(ApiModel):
    ticket: str = Field(min_length=32, max_length=200)


class PairingPreviewResponse(ApiModel):
    pairing_id: str
    label: str
    companion_version: str
    expires_at: int


class PairingConnectResponse(ApiModel):
    runner: RunnerSummary


class PairingStatusResponse(ApiModel):
    pairing_id: str
    state: Literal["pending", "connected", "expired"]
    runner_id: str | None = None


class RunnerHeartbeatRequest(ApiModel):
    readiness: Literal["ready", "busy", "recovery_required"]
    companion_version: str = Field(min_length=1, max_length=40)
    protocol_version: Literal[1]


class RunnerHeartbeatResponse(ApiModel):
    runner: RunnerSummary


class CaptureDraftResponse(ApiModel):
    draft_id: str
    upload_capability: str
    expires_at: int
    status: Literal["active"]


class CaptureRecoveryResponse(ApiModel):
    interrupted_count: int = Field(ge=0, le=1)


class CaptureStatusResponse(ApiModel):
    draft_id: str
    status: Literal["active", "review", "saved", "interrupted"]
    error_message: str | None


class CaptureBatchUpload(ApiModel):
    batch_id: str = Field(min_length=16, max_length=80, pattern=r"^batch_[a-z0-9_-]+$")
    batch_index: int = Field(ge=0, lt=200)
    actions: list[Action] = Field(min_length=1, max_length=25)


class CaptureBatchResponse(ApiModel):
    draft_id: str
    batch_index: int
    duplicate: bool
    received_batch_count: int


class CaptureFinalizeRequest(ApiModel):
    expected_batch_count: int = Field(ge=1, le=200)
    expected_action_count: int = Field(ge=1, le=200)
    capture: CaptureProvenance


class CaptureInterruptRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=500)


class CaptureReviewResponse(ApiModel):
    draft_id: str
    status: Literal["active", "review", "saved", "interrupted"]
    actions: list[Action]
    capture: CaptureProvenance | None
    workflow_id: str | None
    error_message: str | None = None


class WorkflowSaveRequest(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    category_id: str | None = Field(
        default=None,
        min_length=12,
        max_length=68,
        pattern=r"^cat_[a-z0-9_-]+$",
    )
    new_category_name: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("new_category_name")
    @classmethod
    def normalize_new_category_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("new category name cannot be blank")
        if normalized.casefold() == VERIFIED_PRESET_CATEGORY.casefold():
            raise ValueError("the built-in verified category already exists")
        return normalized

    @model_validator(mode="after")
    def require_one_category_choice(self) -> WorkflowSaveRequest:
        if (self.category_id is None) == (self.new_category_name is None):
            raise ValueError("choose one existing category or create one new category")
        return self


class WorkflowCategorySummary(ApiModel):
    category_id: str
    name: str
    verification_profile: HelpdeskVerificationProfile | None
    built_in: bool
    created_at: int


class WorkflowCategoryListResponse(ApiModel):
    categories: list[WorkflowCategorySummary]


class WorkflowSummary(ApiModel):
    workflow_id: str
    name: str
    description: str
    action_count: int
    target_alias: Literal["helpdesk-demo"]
    workflow_mode: WorkflowMode
    category_id: str
    category: str
    created_at: int
    latest_outcome: None = None


class WorkflowListResponse(ApiModel):
    workflows: list[WorkflowSummary]


class WorkflowDetailResponse(ApiModel):
    workflow: WorkflowDefinition


class ReplayRequest(ApiModel):
    client_request_key: str = Field(
        min_length=20,
        max_length=80,
        pattern=r"^request_[a-z0-9_-]+$",
    )


class RunSummary(ApiModel):
    run_id: str
    workflow_id: str
    workflow_name: str
    workflow_mode: WorkflowMode
    category: str
    phase: RunPhase
    outcome: BusinessOutcome | None
    outcome_summary: str | None
    created_at: int
    claimed_at: int | None
    accepted_at: int | None
    started_at: int | None
    finished_at: int | None
    last_event_sequence: int


class RunCreateResponse(ApiModel):
    run: RunSummary


class RunClaimRequest(ApiModel):
    claim_id: str = Field(min_length=18, max_length=70, pattern=r"^claim_[a-z0-9_-]+$")
    reporting_capability_digest: str = Digest


class RunClaimResponse(ApiModel):
    snapshot: RunRequestSnapshot


class RunEventResponse(ApiModel):
    duplicate: bool
    last_event_sequence: int
    phase: RunPhase
    outcome: BusinessOutcome | None


class RunArtifactSummary(ApiModel):
    artifact_id: str
    kind: Literal["baseline", "review_checkpoint", "final", "failure"]
    sha256: str = Digest
    size_bytes: int
    content_type: Literal["image/png"]
    url: str


class RunArtifactResponse(ApiModel):
    artifact: RunArtifactSummary
    duplicate: bool


class RunDetailResponse(ApiModel):
    run: RunSummary
    snapshot: RunRequestSnapshot
    events: list[RunEventEnvelope]
    artifacts: list[RunArtifactSummary]


class RunListResponse(ApiModel):
    runs: list[RunSummary]