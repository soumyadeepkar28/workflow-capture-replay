from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlencode

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PageIdentity(ProtocolModel):
    target_alias: Literal["helpdesk-demo"]
    path: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^/[A-Za-z0-9_./-]*$",
    )
    query: str = Field(default="", max_length=1_000)
    record_ref: Literal["review", "completion"] | None = None

    @field_validator("path")
    @classmethod
    def reject_ambiguous_path_segments(cls, value: str) -> str:
        segments = value.split("/")
        if "//" in value or any(segment in {".", ".."} for segment in segments):
            raise ValueError("path must be a normalized relative target path")
        return value

    @field_validator("query")
    @classmethod
    def normalize_ticket_list_query(cls, value: str) -> str:
        if not value:
            return ""
        try:
            pairs = parse_qsl(
                value,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=30,
            )
        except ValueError as error:
            raise ValueError("query must be a valid bounded form query") from error
        allowed_keys = {
            "assigned_to",
            "date_from",
            "date_to",
            "kbitem",
            "priority",
            "q",
            "queue",
            "saved_query",
            "search_type",
            "sort",
            "sortreverse",
            "status",
        }
        if any(key not in allowed_keys or len(item) > 200 for key, item in pairs):
            raise ValueError("query contains an unsupported Helpdesk filter")
        return urlencode(pairs)

    @model_validator(mode="after")
    def constrain_query_to_ticket_list(self) -> PageIdentity:
        if self.query and self.path != "/tickets/":
            raise ValueError("query is supported only on the Helpdesk ticket list")
        return self


class LocatorScope(ProtocolModel):
    role: Literal["form", "tabpanel", "region"]
    name: str | None = Field(default=None, min_length=1, max_length=120)
    element_id: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def require_scope_identity(self) -> LocatorScope:
        if self.name is None and self.element_id is None:
            raise ValueError("locator scope needs an accessible name or element ID")
        return self


class ElementFingerprint(ProtocolModel):
    tag: str = Field(min_length=1, max_length=40, pattern=r"^[a-z][a-z0-9-]*$")
    input_type: str | None = Field(default=None, min_length=1, max_length=40)
    element_id: str | None = Field(default=None, min_length=1, max_length=120)
    field_name: str | None = Field(default=None, min_length=1, max_length=120)
    observed_role: str | None = Field(default=None, min_length=1, max_length=40)


class RoleLocator(ProtocolModel):
    strategy: Literal["role"]
    role: Literal["button", "link", "tab", "menuitem", "radio", "checkbox"]
    name: str = Field(min_length=1, max_length=160)
    scope: LocatorScope | None = None
    fallback: ElementFingerprint | None = None


class LabelLocator(ProtocolModel):
    strategy: Literal["label"]
    label: str = Field(min_length=1, max_length=160)
    scope: LocatorScope | None = None
    fallback: ElementFingerprint | None = None


Locator = Annotated[RoleLocator | LabelLocator, Field(discriminator="strategy")]


class ActionBase(ProtocolModel):
    action_id: str = Field(
        min_length=16,
        max_length=68,
        pattern=r"^act_[a-z0-9_-]+$",
    )
    sequence: int = Field(ge=0, lt=200)


class NavigateAction(ActionBase):
    kind: Literal["navigate"]
    destination: PageIdentity


class ReloadAction(ActionBase):
    kind: Literal["reload"]
    page: PageIdentity


class ElementAction(ActionBase):
    page: PageIdentity
    locator: Locator


class ClickAction(ElementAction):
    kind: Literal["click"]
    expected_page_after: PageIdentity | None = None


class FillAction(ElementAction):
    kind: Literal["fill"]
    value: str = Field(max_length=2_000)


class SelectAction(ElementAction):
    kind: Literal["select"]
    option_label: str = Field(min_length=1, max_length=160)
    observed_value: str | None = Field(default=None, max_length=500)
    fixture_ref: Literal["actor"] | None = None


class SetCheckedAction(ElementAction):
    kind: Literal["set_checked"]
    checked: bool


Action = Annotated[
    NavigateAction | ReloadAction | ClickAction | FillAction | SelectAction | SetCheckedAction,
    Field(discriminator="kind"),
]


class CaptureProvenance(ProtocolModel):
    extension_version: str = Field(min_length=1, max_length=40)
    captured_at: AwareDatetime
    document_count: int = Field(ge=1, le=50)
    event_count: int = Field(ge=1, le=1_000)
    completeness: Literal["complete"]


class VerificationProfileRef(ProtocolModel):
    profile_id: Literal["helpdesk-linked-tickets-v1"]
    version: Literal[1]
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


WorkflowMode = Literal["verified_preset", "general"]
VERIFIED_PRESET_CATEGORY = "Linked-ticket resolution"
VERIFIED_PRESET_CATEGORY_ID = "cat_linked_tickets_v1"


def _strip_legacy_defaults(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _strip_legacy_defaults(child)
            for key, child in value.items()
            if not (key == "query" and child == "")
        }
    if isinstance(value, list):
        return [_strip_legacy_defaults(child) for child in value]
    return value


class WorkflowDefinition(ProtocolModel):
    schema_version: Literal[1, 2, 3]
    workflow_id: str = Field(
        min_length=15,
        max_length=67,
        pattern=r"^wf_[a-z0-9_-]+$",
    )
    created_at: AwareDatetime
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    target_alias: Literal["helpdesk-demo"]
    workflow_mode: WorkflowMode = "verified_preset"
    category_id: str | None = Field(
        default=None,
        min_length=12,
        max_length=68,
        pattern=r"^cat_[a-z0-9_-]+$",
    )
    category: str = Field(default=VERIFIED_PRESET_CATEGORY, min_length=1, max_length=80)
    capture: CaptureProvenance
    verification_profile: VerificationProfileRef | None
    actions: list[Action] = Field(min_length=1, max_length=200)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("workflow name cannot be blank")
        return normalized

    @field_validator("category")
    @classmethod
    def normalize_category(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("workflow category cannot be blank")
        return normalized

    @model_validator(mode="after")
    def validate_action_identity_and_order(self) -> WorkflowDefinition:
        if self.schema_version < 3 and self.category_id is not None:
            raise ValueError("category ID requires schema version 3")
        if self.schema_version == 3 and self.category_id is None:
            raise ValueError("schema version 3 requires a category ID")
        if self.schema_version == 1 and (
            self.workflow_mode != "verified_preset"
            or self.category != VERIFIED_PRESET_CATEGORY
        ):
            raise ValueError("schema version 1 supports only the verified preset")
        if self.workflow_mode == "verified_preset":
            if self.category != VERIFIED_PRESET_CATEGORY:
                raise ValueError("verified preset must use its fixed category")
            if (
                self.schema_version == 3
                and self.category_id != VERIFIED_PRESET_CATEGORY_ID
            ):
                raise ValueError("verified preset must use its fixed category ID")
            if self.verification_profile is None:
                raise ValueError("verified preset requires a verification profile")
        else:
            if self.category.casefold() == VERIFIED_PRESET_CATEGORY.casefold():
                raise ValueError("general workflow cannot use the reserved preset category")
            if self.category_id == VERIFIED_PRESET_CATEGORY_ID:
                raise ValueError("general workflow cannot use the reserved preset category ID")
            if self.verification_profile is not None:
                raise ValueError("general workflow cannot declare a verification profile")

        sequences = [action.sequence for action in self.actions]
        if sequences != list(range(len(self.actions))):
            raise ValueError("action sequences must be contiguous and start at zero")

        action_ids = [action.action_id for action in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("action IDs must be unique within a workflow")

        return self

    @model_serializer(mode="wrap")
    def preserve_legacy_wire_shape(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> dict[str, object]:
        serialized = handler(self)
        if self.schema_version < 3:
            serialized.pop("category_id", None)
        if self.schema_version == 1:
            serialized.pop("workflow_mode", None)
            serialized.pop("category", None)
            serialized = _strip_legacy_defaults(serialized)
        if not isinstance(serialized, dict):
            raise TypeError("workflow serialization must produce an object")
        return serialized


RunPhase = Literal[
    "pending",
    "claimed",
    "accepted",
    "preparing",
    "running",
    "verifying",
    "finished",
    "interrupted",
    "cancelled",
    "rejected",
    "expired",
]
BusinessOutcome = Literal["succeeded", "partially_succeeded", "failed", "uncertain"]


class HelpdeskVerificationProfile(ProtocolModel):
    profile_id: Literal["helpdesk-linked-tickets-v1"]
    version: Literal[1]
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    review_note: Literal["Demo review completed."]
    completion_note: Literal["Demo completion recorded."]
    actor_username: Literal["workflow_actor"]


class RunRequestSnapshot(ProtocolModel):
    schema_version: Literal[1]
    run_id: str = Field(min_length=16, max_length=68, pattern=r"^run_[a-z0-9_-]+$")
    workflow: WorkflowDefinition
    verification_profile: HelpdeskVerificationProfile | None
    requested_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def profile_matches_workflow(self) -> RunRequestSnapshot:
        reference = self.workflow.verification_profile
        profile = self.verification_profile
        if self.workflow.workflow_mode == "verified_preset":
            if reference is None or profile is None:
                raise ValueError("verified preset run requires a verification profile")
            if (
                reference.profile_id != profile.profile_id
                or reference.version != profile.version
                or reference.content_hash != profile.content_hash
            ):
                raise ValueError("run verification profile does not match workflow reference")
        elif reference is not None or profile is not None:
            raise ValueError("general workflow run cannot include a verification profile")
        if self.expires_at <= self.requested_at:
            raise ValueError("run expiry must be after its request time")
        return self


class PhaseRunEvent(ProtocolModel):
    kind: Literal["phase"]
    phase: RunPhase
    observed_at: AwareDatetime


class ActionRunEvent(ProtocolModel):
    kind: Literal["action"]
    action_id: str = Field(min_length=16, max_length=68, pattern=r"^act_[a-z0-9_-]+$")
    action_sequence: int = Field(ge=0, lt=200)
    stage: Literal["intent", "result"]
    status: Literal["pending", "completed", "failed", "uncertain"]
    observed_at: AwareDatetime
    locator_used: str | None = Field(default=None, max_length=300)
    error_code: str | None = Field(default=None, max_length=80)
    error_message: str | None = Field(default=None, max_length=500)
    possible_side_effect: bool = False

    @model_validator(mode="after")
    def stage_matches_status(self) -> ActionRunEvent:
        if self.stage == "intent" and self.status != "pending":
            raise ValueError("action intent must have pending status")
        if self.stage == "result" and self.status == "pending":
            raise ValueError("action result cannot have pending status")
        return self


class CheckRunEvent(ProtocolModel):
    kind: Literal["check"]
    check_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_-]+$")
    status: Literal["passed", "failed", "unknown", "not_evaluated"]
    expected: str = Field(min_length=1, max_length=500)
    actual: str = Field(min_length=1, max_length=1_000)
    critical: bool
    observed_at: AwareDatetime


class OutcomeRunEvent(ProtocolModel):
    kind: Literal["outcome"]
    outcome: BusinessOutcome
    summary: str = Field(min_length=1, max_length=1_000)
    observed_at: AwareDatetime


RunEvent = Annotated[
    PhaseRunEvent | ActionRunEvent | CheckRunEvent | OutcomeRunEvent,
    Field(discriminator="kind"),
]


class RunEventEnvelope(ProtocolModel):
    event_id: str = Field(min_length=18, max_length=70, pattern=r"^event_[a-z0-9_-]+$")
    sequence: int = Field(ge=0)
    event: RunEvent