from __future__ import annotations

import secrets
import sqlite3
import time
import json
import hashlib
import os
import re
import tempfile
from pathlib import Path
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse

from workflow_api.config import ApiConfig
from workflow_api.database import Database
from workflow_api.models import (
    CaptureBatchResponse,
    CaptureBatchUpload,
    CaptureDraftResponse,
    CaptureFinalizeRequest,
    CaptureInterruptRequest,
    CaptureRecoveryResponse,
    CaptureStatusResponse,
    CaptureReviewResponse,
    PairingConnectResponse,
    PairingInitiateRequest,
    PairingInitiateResponse,
    PairingPreviewResponse,
    PairingStatusResponse,
    PairingTicketRequest,
    ReplayRequest,
    RunClaimRequest,
    RunClaimResponse,
    RunCreateResponse,
    RunDetailResponse,
    RunArtifactResponse,
    RunArtifactSummary,
    RunEventResponse,
    RunListResponse,
    RunSummary,
    RunnerHeartbeatRequest,
    RunnerHeartbeatResponse,
    RunnerSummary,
    SessionResponse,
    WorkflowDetailResponse,
    WorkflowCategoryListResponse,
    WorkflowCategorySummary,
    WorkflowListResponse,
    WorkflowSaveRequest,
    WorkflowSummary,
)
from workflow_api.security import bearer_token, digest_secret, new_public_id, new_secret
from workflow_api.workflows import (
    build_run_snapshot,
    build_workflow_definition,
    canonical_json,
    content_hash,
)
from workflow_protocol import (
    CaptureProvenance,
    HelpdeskVerificationProfile,
    RunEventEnvelope,
    RunRequestSnapshot,
    WorkflowDefinition,
)

SESSION_COOKIE = "workflow_portal_session"
CSRF_COOKIE = "workflow_portal_csrf"

PHASE_TRANSITIONS = {
    "claimed": {"accepted", "rejected"},
    "accepted": {"preparing", "interrupted", "cancelled"},
    "preparing": {"running", "finished", "interrupted", "cancelled"},
    "running": {"verifying", "finished", "interrupted", "cancelled"},
    "verifying": {"finished", "interrupted", "cancelled"},
}


def api_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _runner_summary(
    row: sqlite3.Row,
    *,
    observed_at: int | None = None,
    freshness_seconds: int = 10,
) -> RunnerSummary:
    readiness = row["readiness"]
    if (
        observed_at is not None
        and (row["last_seen_at"] is None or row["last_seen_at"] < observed_at - freshness_seconds)
    ):
        readiness = "offline"
    return RunnerSummary(
        runner_id=row["id"],
        label=row["label"],
        readiness=readiness,
        last_seen_at=row["last_seen_at"],
        companion_version=row["companion_version"],
        protocol_version=row["protocol_version"],
    )


def _run_summary(row: sqlite3.Row) -> RunSummary:
    return RunSummary(
        run_id=row["id"],
        workflow_id=row["workflow_id"],
        workflow_name=row["workflow_name"],
        phase=row["phase"],
        outcome=row["outcome"],
        outcome_summary=row["outcome_summary"],
        workflow_mode=row["workflow_mode"],
        category=row["workflow_category"],
        created_at=row["created_at"],
        claimed_at=row["claimed_at"],
        accepted_at=row["accepted_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        last_event_sequence=row["last_event_sequence"],
    )


def create_app(
    config: ApiConfig | None = None,
    *,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    selected_config = config or ApiConfig.from_environment()
    database = Database(selected_config.database_path)
    artifact_root = (
        selected_config.artifact_root
        if selected_config.artifact_root is not None
        else selected_config.database_path.parent / "artifacts"
    ).resolve()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        database.initialize()
        yield

    app = FastAPI(title="Workflow Capture and Replay API", version="0.1.0", lifespan=lifespan)
    app.state.config = selected_config
    app.state.database = database
    app.state.clock = clock

    def now() -> int:
        return int(clock())

    def valid_session(session_token: str | None) -> sqlite3.Row | None:
        if not session_token:
            return None
        with database.connect() as connection:
            return connection.execute(
                "SELECT * FROM portal_session WHERE token_digest = ? AND expires_at >= ?",
                (digest_secret(session_token), now()),
            ).fetchone()

    def require_portal_authority(
        request: Request,
        workflow_portal_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        workflow_portal_csrf: Annotated[str | None, Cookie(alias=CSRF_COOKIE)] = None,
        x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> sqlite3.Row:
        session = valid_session(workflow_portal_session)
        if session is None:
            raise api_error(status.HTTP_401_UNAUTHORIZED, "portal_session_required", "Portal session required.")
        if request.headers.get("origin") != selected_config.public_origin:
            raise api_error(status.HTTP_403_FORBIDDEN, "origin_rejected", "Request origin rejected.")
        if (
            workflow_portal_csrf is None
            or x_csrf_token is None
            or not secrets.compare_digest(workflow_portal_csrf, x_csrf_token)
            or not secrets.compare_digest(
                digest_secret(x_csrf_token),
                session["csrf_digest"],
            )
        ):
            raise api_error(status.HTTP_403_FORBIDDEN, "csrf_rejected", "CSRF validation failed.")
        return session

    def require_portal_session(
        workflow_portal_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> sqlite3.Row:
        session = valid_session(workflow_portal_session)
        if session is None:
            raise api_error(status.HTTP_401_UNAUTHORIZED, "portal_session_required", "Portal session required.")
        return session

    def capture_draft_for_capability(draft_id: str, authorization: str | None) -> sqlite3.Row:
        token = bearer_token(authorization)
        if token is None:
            raise api_error(status.HTTP_401_UNAUTHORIZED, "capture_capability_required", "Capture capability required.")
        with database.connect() as connection:
            draft = connection.execute(
                """
                SELECT * FROM capture_draft
                WHERE id = ? AND capability_digest = ? AND expires_at >= ?
                """,
                (draft_id, digest_secret(token), now()),
            ).fetchone()
        if draft is None:
            raise api_error(status.HTTP_401_UNAUTHORIZED, "capture_capability_invalid", "Capture capability invalid or expired.")
        return draft

    def capture_actions(connection: sqlite3.Connection, draft_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT payload_json FROM capture_batch WHERE draft_id = ? ORDER BY batch_index",
            (draft_id,),
        ).fetchall()
        return [
            action
            for row in rows
            for action in json.loads(row["payload_json"])["actions"]
        ]

    def run_for_reporting_capability(
        run_id: str,
        authorization: str | None,
    ) -> sqlite3.Row:
        token = bearer_token(authorization)
        if token is None:
            raise api_error(
                status.HTTP_401_UNAUTHORIZED,
                "run_reporting_capability_required",
                "Run reporting capability required.",
            )
        with database.connect() as connection:
            run = connection.execute(
                """
                SELECT workflow_run.*, workflow.name AS workflow_name,
                    workflow.workflow_mode AS workflow_mode,
                    workflow.category AS workflow_category
                FROM workflow_run
                JOIN workflow ON workflow.id = workflow_run.workflow_id
                WHERE workflow_run.id = ? AND reporting_capability_digest = ?
                """,
                (run_id, digest_secret(token)),
            ).fetchone()
        if run is None:
            raise api_error(
                status.HTTP_401_UNAUTHORIZED,
                "run_reporting_capability_invalid",
                "Run reporting capability invalid.",
            )
        return run

    def artifact_summary(row: sqlite3.Row) -> RunArtifactSummary:
        return RunArtifactSummary(
            artifact_id=row["artifact_id"],
            kind=row["kind"],
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            content_type=row["content_type"],
            url=f"/api/runs/{row['run_id']}/artifacts/{row['artifact_id']}",
        )

    def require_runner(
        authorization: Annotated[str | None, Header()] = None,
    ) -> sqlite3.Row:
        token = bearer_token(authorization)
        if token is None:
            raise api_error(status.HTTP_401_UNAUTHORIZED, "runner_credential_required", "Runner credential required.")
        with database.connect() as connection:
            runner = connection.execute(
                """
                SELECT * FROM runner
                WHERE credential_digest = ? AND revoked_at IS NULL AND expires_at >= ?
                """,
                (digest_secret(token), now()),
            ).fetchone()
        if runner is None:
            raise api_error(status.HTTP_401_UNAUTHORIZED, "runner_credential_invalid", "Runner credential invalid.")
        return runner

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/session", response_model=SessionResponse)
    def session(
        response: Response,
        workflow_portal_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        workflow_portal_csrf: Annotated[str | None, Cookie(alias=CSRF_COOKIE)] = None,
    ) -> SessionResponse:
        current = valid_session(workflow_portal_session)
        csrf_token = workflow_portal_csrf
        if current is None:
            session_token = new_secret("portal")
            csrf_token = new_secret("csrf")
            session_id = new_public_id("session")
            expires_at = now() + selected_config.portal_session_ttl_seconds
            with database.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO portal_session(
                        id, token_digest, csrf_digest, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        digest_secret(session_token),
                        digest_secret(csrf_token),
                        now(),
                        expires_at,
                    ),
                )
            response.set_cookie(
                SESSION_COOKIE,
                session_token,
                httponly=True,
                secure=selected_config.cookie_secure,
                samesite="lax",
                max_age=selected_config.portal_session_ttl_seconds,
                path="/",
            )
            current = valid_session(session_token)
        elif csrf_token is None or not secrets.compare_digest(
            digest_secret(csrf_token), current["csrf_digest"]
        ):
            csrf_token = new_secret("csrf")
            with database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE portal_session SET csrf_digest = ? WHERE id = ?",
                    (digest_secret(csrf_token), current["id"]),
                )

        response.set_cookie(
            CSRF_COOKIE,
            csrf_token,
            httponly=False,
            secure=selected_config.cookie_secure,
            samesite="lax",
            max_age=selected_config.portal_session_ttl_seconds,
            path="/",
        )
        with database.connect() as connection:
            runner = connection.execute(
                """
                SELECT * FROM runner
                WHERE controller_session_id = ? AND revoked_at IS NULL AND expires_at >= ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (current["id"], now()),
            ).fetchone()
        return SessionResponse(
            csrf_token=csrf_token,
            expires_at=current["expires_at"],
            runner=(
                _runner_summary(
                    runner,
                    observed_at=now(),
                    freshness_seconds=selected_config.readiness_freshness_seconds,
                )
                if runner is not None
                else None
            ),
        )

    @app.post("/api/pairings", response_model=PairingInitiateResponse, status_code=201)
    def initiate_pairing(payload: PairingInitiateRequest, request: Request) -> PairingInitiateResponse:
        created_at = now()
        source_ip = request.client.host if request.client else "unknown"
        with database.transaction(immediate=True) as connection:
            recent_count = connection.execute(
                "SELECT COUNT(*) AS count FROM pairing WHERE source_ip = ? AND created_at >= ?",
                (source_ip, created_at - 60),
            ).fetchone()["count"]
            if recent_count >= 10:
                raise api_error(status.HTTP_429_TOO_MANY_REQUESTS, "pairing_rate_limited", "Too many pairing attempts.")
            pairing_id = new_public_id("pair")
            ticket = new_secret("pair_ticket")
            expires_at = created_at + selected_config.pairing_ttl_seconds
            try:
                connection.execute(
                    """
                    INSERT INTO pairing(
                        id, label, ticket_digest, pending_proof_digest,
                        runner_credential_digest, companion_version, protocol_version,
                        source_ip, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pairing_id,
                        payload.label.strip(),
                        digest_secret(ticket),
                        payload.pending_proof_digest,
                        payload.runner_credential_digest,
                        payload.companion_version,
                        payload.protocol_version,
                        source_ip,
                        created_at,
                        expires_at,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise api_error(status.HTTP_409_CONFLICT, "pairing_secret_reused", "Pairing credentials were already registered.") from error
        return PairingInitiateResponse(
            pairing_id=pairing_id,
            pairing_url=f"{selected_config.public_origin}/pair#ticket={ticket}",
            expires_at=expires_at,
        )

    @app.post("/api/pairings/preview", response_model=PairingPreviewResponse)
    def preview_pairing(
        payload: PairingTicketRequest,
        portal_session: Any = Depends(require_portal_authority),
    ) -> PairingPreviewResponse:
        del portal_session
        with database.connect() as connection:
            pairing = connection.execute(
                """
                SELECT * FROM pairing
                WHERE ticket_digest = ? AND consumed_at IS NULL AND expires_at >= ?
                """,
                (digest_secret(payload.ticket), now()),
            ).fetchone()
        if pairing is None:
            raise api_error(status.HTTP_404_NOT_FOUND, "pairing_ticket_invalid", "Pairing ticket is invalid or expired.")
        return PairingPreviewResponse(
            pairing_id=pairing["id"],
            label=pairing["label"],
            companion_version=pairing["companion_version"],
            expires_at=pairing["expires_at"],
        )

    @app.post("/api/pairings/connect", response_model=PairingConnectResponse)
    def connect_pairing(
        payload: PairingTicketRequest,
        portal_session: Any = Depends(require_portal_authority),
    ) -> PairingConnectResponse:
        connected_at = now()
        with database.transaction(immediate=True) as connection:
            pairing = connection.execute(
                """
                SELECT * FROM pairing
                WHERE ticket_digest = ? AND consumed_at IS NULL AND expires_at >= ?
                """,
                (digest_secret(payload.ticket), connected_at),
            ).fetchone()
            if pairing is None:
                raise api_error(status.HTTP_409_CONFLICT, "pairing_ticket_consumed", "Pairing ticket is invalid, expired, or already consumed.")
            connection.execute(
                "UPDATE runner SET revoked_at = ?, readiness = 'offline' "
                "WHERE controller_session_id = ? AND revoked_at IS NULL",
                (connected_at, portal_session["id"]),
            )
            runner_id = new_public_id("runner")
            connection.execute(
                """
                INSERT INTO runner(
                    id, label, credential_digest, controller_session_id,
                    companion_version, protocol_version, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    runner_id,
                    pairing["label"],
                    pairing["runner_credential_digest"],
                    portal_session["id"],
                    pairing["companion_version"],
                    pairing["protocol_version"],
                    connected_at,
                    connected_at + selected_config.runner_credential_ttl_seconds,
                ),
            )
            updated = connection.execute(
                """
                UPDATE pairing
                SET consumed_at = ?, controller_session_id = ?, runner_id = ?
                WHERE id = ? AND consumed_at IS NULL
                """,
                (connected_at, portal_session["id"], runner_id, pairing["id"]),
            )
            if updated.rowcount != 1:
                raise api_error(status.HTTP_409_CONFLICT, "pairing_ticket_consumed", "Pairing ticket was already consumed.")
            runner = connection.execute("SELECT * FROM runner WHERE id = ?", (runner_id,)).fetchone()
        return PairingConnectResponse(runner=_runner_summary(runner))

    @app.get("/api/pairings/{pairing_id}/status", response_model=PairingStatusResponse)
    def pairing_status(
        pairing_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> PairingStatusResponse:
        proof = bearer_token(authorization)
        if proof is None:
            raise api_error(status.HTTP_401_UNAUTHORIZED, "pending_proof_required", "Pending pairing proof required.")
        with database.connect() as connection:
            pairing = connection.execute(
                "SELECT * FROM pairing WHERE id = ? AND pending_proof_digest = ?",
                (pairing_id, digest_secret(proof)),
            ).fetchone()
        if pairing is None:
            raise api_error(status.HTTP_401_UNAUTHORIZED, "pending_proof_invalid", "Pending pairing proof invalid.")
        if pairing["runner_id"] is not None:
            state = "connected"
        elif pairing["expires_at"] < now():
            state = "expired"
        else:
            state = "pending"
        return PairingStatusResponse(
            pairing_id=pairing_id,
            state=state,
            runner_id=pairing["runner_id"],
        )

    @app.post("/api/runner/heartbeat", response_model=RunnerHeartbeatResponse)
    def runner_heartbeat(
        payload: RunnerHeartbeatRequest,
        runner: Any = Depends(require_runner),
    ) -> RunnerHeartbeatResponse:
        if payload.protocol_version != runner["protocol_version"]:
            raise api_error(status.HTTP_409_CONFLICT, "protocol_version_mismatch", "Runner protocol version does not match its pairing.")
        with database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE runner
                SET readiness = ?, last_seen_at = ?, companion_version = ?
                WHERE id = ? AND revoked_at IS NULL
                """,
                (payload.readiness, now(), payload.companion_version, runner["id"]),
            )
            updated = connection.execute("SELECT * FROM runner WHERE id = ?", (runner["id"],)).fetchone()
        return RunnerHeartbeatResponse(runner=_runner_summary(updated))

    @app.post("/api/captures", response_model=CaptureDraftResponse, status_code=201)
    def create_capture(
        portal_session: Any = Depends(require_portal_authority),
    ) -> CaptureDraftResponse:
        created_at = now()
        with database.transaction(immediate=True) as connection:
            runner = connection.execute(
                """
                SELECT * FROM runner
                WHERE controller_session_id = ?
                  AND revoked_at IS NULL
                  AND expires_at >= ?
                  AND readiness = 'ready'
                  AND last_seen_at >= ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (
                    portal_session["id"],
                    created_at,
                    created_at - selected_config.readiness_freshness_seconds,
                ),
            ).fetchone()
            if runner is None:
                raise api_error(status.HTTP_409_CONFLICT, "runner_not_ready", "A recently ready local runner is required.")
            connection.execute(
                """
                UPDATE capture_draft
                SET status = 'interrupted', finalized_at = ?,
                    error_message = 'capture capability expired before finalization'
                WHERE runner_id = ? AND status = 'active' AND expires_at < ?
                """,
                (created_at, runner["id"], created_at),
            )
            active = connection.execute(
                """
                SELECT id FROM capture_draft
                WHERE runner_id = ? AND status = 'active' AND expires_at >= ?
                LIMIT 1
                """,
                (runner["id"], created_at),
            ).fetchone()
            if active is not None:
                raise api_error(
                    status.HTTP_409_CONFLICT,
                    "capture_already_active",
                    "This runner already has an active capture draft.",
                )
            draft_id = new_public_id("draft")
            capability = new_secret("capture")
            expires_at = created_at + selected_config.capture_capability_ttl_seconds
            connection.execute(
                """
                INSERT INTO capture_draft(
                    id, controller_session_id, runner_id, capability_digest,
                    status, created_at, expires_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    draft_id,
                    portal_session["id"],
                    runner["id"],
                    digest_secret(capability),
                    created_at,
                    expires_at,
                ),
            )
        return CaptureDraftResponse(
            draft_id=draft_id,
            upload_capability=capability,
            expires_at=expires_at,
            status="active",
        )

    @app.post("/api/capture-recovery", response_model=CaptureRecoveryResponse)
    def recover_active_capture(
        portal_session: Any = Depends(require_portal_authority),
    ) -> CaptureRecoveryResponse:
        recovered_at = now()
        with database.transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE capture_draft
                SET status = 'interrupted', finalized_at = ?,
                    error_message = 'portal discarded stale capture before extension start'
                WHERE controller_session_id = ? AND status = 'active'
                """,
                (recovered_at, portal_session["id"]),
            )
        return CaptureRecoveryResponse(interrupted_count=updated.rowcount)

    @app.get(
        "/api/captures/{draft_id}/status",
        response_model=CaptureStatusResponse,
    )
    def capture_capability_status(
        draft_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> CaptureStatusResponse:
        draft = capture_draft_for_capability(draft_id, authorization)
        return CaptureStatusResponse(
            draft_id=draft["id"],
            status=draft["status"],
            error_message=draft["error_message"],
        )

    @app.post(
        "/api/captures/{draft_id}/batches",
        response_model=CaptureBatchResponse,
        status_code=201,
    )
    def upload_capture_batch(
        draft_id: str,
        payload: CaptureBatchUpload,
        authorization: Annotated[str | None, Header()] = None,
    ) -> CaptureBatchResponse:
        capture_draft_for_capability(draft_id, authorization)
        serialized = canonical_json(payload.model_dump(mode="json"))
        payload_digest = content_hash(payload.model_dump(mode="json"))
        with database.transaction(immediate=True) as connection:
            draft = connection.execute(
                "SELECT * FROM capture_draft WHERE id = ?",
                (draft_id,),
            ).fetchone()
            if draft["status"] != "active":
                raise api_error(status.HTTP_409_CONFLICT, "capture_not_active", "Capture is no longer active.")
            existing = connection.execute(
                "SELECT * FROM capture_batch WHERE draft_id = ? AND batch_index = ?",
                (draft_id, payload.batch_index),
            ).fetchone()
            duplicate = existing is not None
            if existing is not None:
                if existing["batch_id"] != payload.batch_id or existing["payload_hash"] != payload_digest:
                    raise api_error(status.HTTP_409_CONFLICT, "capture_batch_conflict", "Batch index already contains different data.")
            else:
                try:
                    connection.execute(
                        """
                        INSERT INTO capture_batch(
                            draft_id, batch_index, batch_id, payload_hash,
                            payload_json, action_count, received_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            draft_id,
                            payload.batch_index,
                            payload.batch_id,
                            payload_digest,
                            serialized,
                            len(payload.actions),
                            now(),
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise api_error(status.HTTP_409_CONFLICT, "capture_batch_conflict", "Batch identity already contains different data.") from error
            received = connection.execute(
                "SELECT COUNT(*) AS count FROM capture_batch WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()["count"]
        return CaptureBatchResponse(
            draft_id=draft_id,
            batch_index=payload.batch_index,
            duplicate=duplicate,
            received_batch_count=received,
        )

    @app.post(
        "/api/captures/{draft_id}/finalize",
        response_model=CaptureReviewResponse,
    )
    def finalize_capture(
        draft_id: str,
        payload: CaptureFinalizeRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> CaptureReviewResponse:
        capture_draft_for_capability(draft_id, authorization)
        with database.transaction(immediate=True) as connection:
            draft = connection.execute("SELECT * FROM capture_draft WHERE id = ?", (draft_id,)).fetchone()
            if draft["status"] != "active":
                raise api_error(status.HTTP_409_CONFLICT, "capture_not_active", "Capture is no longer active.")
            batch_rows = connection.execute(
                "SELECT batch_index, action_count FROM capture_batch WHERE draft_id = ? ORDER BY batch_index",
                (draft_id,),
            ).fetchall()
            indices = [row["batch_index"] for row in batch_rows]
            action_count = sum(row["action_count"] for row in batch_rows)
            if (
                len(batch_rows) != payload.expected_batch_count
                or indices != list(range(payload.expected_batch_count))
                or action_count != payload.expected_action_count
            ):
                raise api_error(status.HTTP_409_CONFLICT, "capture_incomplete", "Capture batches or actions are incomplete.")
            actions = capture_actions(connection, draft_id)
            sequences = [action["sequence"] for action in actions]
            action_ids = [action["action_id"] for action in actions]
            if sequences != list(range(len(actions))) or len(action_ids) != len(set(action_ids)):
                raise api_error(status.HTTP_409_CONFLICT, "capture_action_sequence_invalid", "Captured actions are not contiguous and unique.")
            connection.execute(
                """
                UPDATE capture_draft
                SET status = 'review', finalized_at = ?, capture_json = ?,
                    expected_batch_count = ?, expected_action_count = ?
                WHERE id = ?
                """,
                (
                    now(),
                    canonical_json(payload.capture.model_dump(mode="json")),
                    payload.expected_batch_count,
                    payload.expected_action_count,
                    draft_id,
                ),
            )
        return CaptureReviewResponse(
            draft_id=draft_id,
            status="review",
            actions=actions,
            capture=payload.capture,
            workflow_id=None,
            error_message=None,
        )

    @app.post(
        "/api/captures/{draft_id}/interrupt",
        response_model=CaptureReviewResponse,
    )
    def interrupt_capture(
        draft_id: str,
        payload: CaptureInterruptRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> CaptureReviewResponse:
        capture_draft_for_capability(draft_id, authorization)
        with database.transaction(immediate=True) as connection:
            draft = connection.execute(
                "SELECT * FROM capture_draft WHERE id = ?",
                (draft_id,),
            ).fetchone()
            if draft["status"] not in {"active", "interrupted"}:
                raise api_error(
                    status.HTTP_409_CONFLICT,
                    "capture_not_interruptible",
                    "Capture can no longer be interrupted.",
                )
            if draft["status"] == "active":
                connection.execute(
                    """
                    UPDATE capture_draft
                    SET status = 'interrupted', finalized_at = ?, error_message = ?
                    WHERE id = ? AND status = 'active'
                    """,
                    (now(), payload.reason, draft_id),
                )
            actions = capture_actions(connection, draft_id)
        return CaptureReviewResponse(
            draft_id=draft_id,
            status="interrupted",
            actions=actions,
            capture=None,
            workflow_id=None,
            error_message=draft["error_message"] if draft["status"] == "interrupted" else payload.reason,
        )

    @app.get("/api/captures/{draft_id}", response_model=CaptureReviewResponse)
    def capture_review(
        draft_id: str,
        portal_session: Any = Depends(require_portal_session),
    ) -> CaptureReviewResponse:
        with database.connect() as connection:
            draft = connection.execute(
                "SELECT * FROM capture_draft WHERE id = ? AND controller_session_id = ?",
                (draft_id, portal_session["id"]),
            ).fetchone()
            if draft is None:
                raise api_error(status.HTTP_404_NOT_FOUND, "capture_not_found", "Capture draft not found.")
            actions = capture_actions(connection, draft_id)
        capture = (
            CaptureProvenance.model_validate_json(draft["capture_json"])
            if draft["capture_json"] is not None
            else None
        )
        return CaptureReviewResponse(
            draft_id=draft_id,
            status=draft["status"],
            actions=actions,
            capture=capture,
            workflow_id=draft["workflow_id"],
            error_message=draft["error_message"],
        )

    @app.post(
        "/api/captures/{draft_id}/save",
        response_model=WorkflowDetailResponse,
        status_code=201,
    )
    def save_capture(
        draft_id: str,
        payload: WorkflowSaveRequest,
        portal_session: Any = Depends(require_portal_authority),
    ) -> WorkflowDetailResponse:
        created_at = now()
        with database.transaction(immediate=True) as connection:
            draft = connection.execute(
                "SELECT * FROM capture_draft WHERE id = ? AND controller_session_id = ?",
                (draft_id, portal_session["id"]),
            ).fetchone()
            if draft is None:
                raise api_error(status.HTTP_404_NOT_FOUND, "capture_not_found", "Capture draft not found.")
            if draft["status"] != "review" or draft["capture_json"] is None:
                raise api_error(status.HTTP_409_CONFLICT, "capture_not_reviewable", "Capture is not ready to save.")
            actions = capture_actions(connection, draft_id)
            if payload.category_id is not None:
                category = connection.execute(
                    "SELECT * FROM workflow_category WHERE id = ?",
                    (payload.category_id,),
                ).fetchone()
                if category is None:
                    raise api_error(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        "workflow_category_not_found",
                        "The selected workflow category no longer exists.",
                    )
            else:
                category_id = new_public_id("cat")
                try:
                    connection.execute(
                        """
                        INSERT INTO workflow_category(
                            id, name, verification_profile_json, built_in, created_at
                        ) VALUES (?, ?, NULL, 0, ?)
                        """,
                        (category_id, payload.new_category_name, created_at),
                    )
                except sqlite3.IntegrityError as error:
                    raise api_error(
                        status.HTTP_409_CONFLICT,
                        "workflow_category_name_exists",
                        "A workflow category with this name already exists; select it instead.",
                    ) from error
                category = connection.execute(
                    "SELECT * FROM workflow_category WHERE id = ?",
                    (category_id,),
                ).fetchone()
            category_profile = (
                HelpdeskVerificationProfile.model_validate_json(
                    category["verification_profile_json"]
                )
                if category["verification_profile_json"] is not None
                else None
            )
            workflow_id = new_public_id("wf")
            workflow = build_workflow_definition(
                workflow_id=workflow_id,
                created_at=created_at,
                name=payload.name,
                description=payload.description,
                capture=CaptureProvenance.model_validate_json(draft["capture_json"]),
                actions=actions,
                category_id=category["id"],
                category=category["name"],
                category_verification_profile=category_profile,
            )
            connection.execute(
                """
                INSERT INTO workflow(
                    id, name, description, target_alias, action_count,
                    content_hash, definition_json, created_at, workflow_mode,
                    category, category_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow.workflow_id,
                    workflow.name,
                    workflow.description,
                    workflow.target_alias,
                    len(workflow.actions),
                    workflow.content_hash,
                    workflow.model_dump_json(),
                    created_at,
                    workflow.workflow_mode,
                    workflow.category,
                    workflow.category_id,
                ),
            )
            connection.execute(
                "UPDATE capture_draft SET status = 'saved', workflow_id = ? WHERE id = ?",
                (workflow.workflow_id, draft_id),
            )
        return WorkflowDetailResponse(workflow=workflow)

    @app.get(
        "/api/workflow-categories",
        response_model=WorkflowCategoryListResponse,
    )
    def list_workflow_categories() -> WorkflowCategoryListResponse:
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workflow_category
                ORDER BY built_in DESC, name COLLATE NOCASE, id
                """
            ).fetchall()
        return WorkflowCategoryListResponse(
            categories=[
                WorkflowCategorySummary(
                    category_id=row["id"],
                    name=row["name"],
                    verification_profile=(
                        HelpdeskVerificationProfile.model_validate_json(
                            row["verification_profile_json"]
                        )
                        if row["verification_profile_json"] is not None
                        else None
                    ),
                    built_in=bool(row["built_in"]),
                    created_at=row["created_at"],
                )
                for row in rows
            ]
        )

    @app.get("/api/workflows", response_model=WorkflowListResponse)
    def list_workflows() -> WorkflowListResponse:
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return WorkflowListResponse(
            workflows=[
                WorkflowSummary(
                    workflow_id=row["id"],
                    name=row["name"],
                    description=row["description"],
                    action_count=row["action_count"],
                    target_alias=row["target_alias"],
                    workflow_mode=row["workflow_mode"],
                    category_id=row["category_id"],
                    category=row["category"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]
        )

    @app.get("/api/workflows/{workflow_id}", response_model=WorkflowDetailResponse)
    def workflow_detail(workflow_id: str) -> WorkflowDetailResponse:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT definition_json FROM workflow WHERE id = ?",
                (workflow_id,),
            ).fetchone()
        if row is None:
            raise api_error(status.HTTP_404_NOT_FOUND, "workflow_not_found", "Workflow not found.")
        return WorkflowDetailResponse(
            workflow=WorkflowDefinition.model_validate_json(row["definition_json"])
        )

    @app.post(
        "/api/workflows/{workflow_id}/runs",
        response_model=RunCreateResponse,
        status_code=201,
    )
    def create_run(
        workflow_id: str,
        payload: ReplayRequest,
        portal_session: Any = Depends(require_portal_authority),
    ) -> RunCreateResponse:
        created_at = now()
        with database.transaction(immediate=True) as connection:
            workflow_row = connection.execute(
                "SELECT * FROM workflow WHERE id = ?",
                (workflow_id,),
            ).fetchone()
            if workflow_row is None:
                raise api_error(
                    status.HTTP_404_NOT_FOUND,
                    "workflow_not_found",
                    "Workflow not found.",
                )
            runner = connection.execute(
                """
                SELECT * FROM runner
                WHERE controller_session_id = ?
                  AND revoked_at IS NULL
                  AND expires_at >= ?
                  AND readiness = 'ready'
                  AND last_seen_at >= ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (
                    portal_session["id"],
                    created_at,
                    created_at - selected_config.readiness_freshness_seconds,
                ),
            ).fetchone()
            if runner is None:
                raise api_error(
                    status.HTTP_409_CONFLICT,
                    "runner_not_ready",
                    "A recently ready local runner is required.",
                )
            active_capture = connection.execute(
                """
                SELECT 1 FROM capture_draft
                WHERE runner_id = ? AND status = 'active' AND expires_at >= ?
                LIMIT 1
                """,
                (runner["id"], created_at),
            ).fetchone()
            if active_capture is not None:
                raise api_error(
                    status.HTTP_409_CONFLICT,
                    "runner_capturing",
                    "Replay cannot start while this runner has an active capture.",
                )
            request_digest = content_hash(
                {
                    "workflow_id": workflow_id,
                    "runner_id": runner["id"],
                }
            )
            existing = connection.execute(
                """
                SELECT workflow_run.*, workflow.name AS workflow_name,
                    workflow.workflow_mode AS workflow_mode,
                    workflow.category AS workflow_category
                FROM workflow_run
                JOIN workflow ON workflow.id = workflow_run.workflow_id
                WHERE controller_session_id = ? AND client_request_key = ?
                """,
                (portal_session["id"], payload.client_request_key),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_digest:
                    raise api_error(
                        status.HTTP_409_CONFLICT,
                        "request_key_conflict",
                        "Request key already identifies different replay data.",
                    )
                return RunCreateResponse(run=_run_summary(existing))

            run_id = new_public_id("run")
            expires_at = created_at + 60
            snapshot = build_run_snapshot(
                run_id=run_id,
                workflow=WorkflowDefinition.model_validate_json(workflow_row["definition_json"]),
                requested_at=created_at,
                expires_at=expires_at,
            )
            connection.execute(
                """
                INSERT INTO workflow_run(
                    id, controller_session_id, runner_id, workflow_id,
                    client_request_key, request_hash, snapshot_json, phase,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    run_id,
                    portal_session["id"],
                    runner["id"],
                    workflow_id,
                    payload.client_request_key,
                    request_digest,
                    snapshot.model_dump_json(),
                    created_at,
                    expires_at,
                ),
            )
            created = connection.execute(
                """
                SELECT workflow_run.*, workflow.name AS workflow_name,
                    workflow.workflow_mode AS workflow_mode,
                    workflow.category AS workflow_category
                FROM workflow_run
                JOIN workflow ON workflow.id = workflow_run.workflow_id
                WHERE workflow_run.id = ?
                """,
                (run_id,),
            ).fetchone()
        return RunCreateResponse(run=_run_summary(created))

    @app.post("/api/runner/claims", response_model=RunClaimResponse)
    def claim_run(
        payload: RunClaimRequest,
        response: Response,
        runner: Any = Depends(require_runner),
    ) -> RunClaimResponse | Response:
        claimed_at = now()
        with database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE workflow_run
                SET phase = 'expired', finished_at = ?
                WHERE runner_id = ? AND phase = 'pending' AND expires_at < ?
                """,
                (claimed_at, runner["id"], claimed_at),
            )
            existing = connection.execute(
                """
                SELECT * FROM workflow_run
                WHERE runner_id = ? AND claim_id = ?
                """,
                (runner["id"], payload.claim_id),
            ).fetchone()
            if existing is not None:
                if existing["reporting_capability_digest"] != payload.reporting_capability_digest:
                    raise api_error(
                        status.HTTP_409_CONFLICT,
                        "claim_key_conflict",
                        "Claim key already uses different reporting authority.",
                    )
                return RunClaimResponse(
                    snapshot=RunRequestSnapshot.model_validate_json(existing["snapshot_json"])
                )
            pending = connection.execute(
                """
                SELECT * FROM workflow_run
                WHERE runner_id = ? AND phase = 'pending' AND expires_at >= ?
                ORDER BY created_at, id LIMIT 1
                """,
                (runner["id"], claimed_at),
            ).fetchone()
            if pending is None:
                response.status_code = status.HTTP_204_NO_CONTENT
                return response
            updated = connection.execute(
                """
                UPDATE workflow_run
                SET phase = 'claimed', claim_id = ?, reporting_capability_digest = ?,
                    claimed_at = ?
                WHERE id = ? AND phase = 'pending'
                """,
                (
                    payload.claim_id,
                    payload.reporting_capability_digest,
                    claimed_at,
                    pending["id"],
                ),
            )
            if updated.rowcount != 1:
                raise api_error(
                    status.HTTP_409_CONFLICT,
                    "run_claim_conflict",
                    "Run was claimed concurrently.",
                )
            connection.execute(
                "UPDATE runner SET readiness = 'busy', last_seen_at = ? WHERE id = ?",
                (claimed_at, runner["id"]),
            )
        return RunClaimResponse(
            snapshot=RunRequestSnapshot.model_validate_json(pending["snapshot_json"])
        )

    @app.post(
        "/api/runs/{run_id}/events",
        response_model=RunEventResponse,
        status_code=201,
    )
    def append_run_event(
        run_id: str,
        envelope: RunEventEnvelope,
        authorization: Annotated[str | None, Header()] = None,
    ) -> RunEventResponse:
        run_for_reporting_capability(run_id, authorization)
        event_json = envelope.model_dump_json()
        event_digest = content_hash(envelope.model_dump(mode="json"))
        received_at = now()
        with database.transaction(immediate=True) as connection:
            run = connection.execute(
                """
                SELECT workflow_run.*, workflow.name AS workflow_name,
                    workflow.workflow_mode AS workflow_mode,
                    workflow.category AS workflow_category
                FROM workflow_run
                JOIN workflow ON workflow.id = workflow_run.workflow_id
                WHERE workflow_run.id = ?
                """,
                (run_id,),
            ).fetchone()
            existing = connection.execute(
                "SELECT * FROM run_event WHERE run_id = ? AND sequence = ?",
                (run_id, envelope.sequence),
            ).fetchone()
            if existing is not None:
                if existing["event_id"] != envelope.event_id or existing["event_hash"] != event_digest:
                    raise api_error(
                        status.HTTP_409_CONFLICT,
                        "run_event_conflict",
                        "Event sequence already contains different data.",
                    )
                return RunEventResponse(
                    duplicate=True,
                    last_event_sequence=run["last_event_sequence"],
                    phase=run["phase"],
                    outcome=run["outcome"],
                )
            if envelope.sequence != run["last_event_sequence"] + 1:
                raise api_error(
                    status.HTTP_409_CONFLICT,
                    "run_event_gap",
                    "Run events must be appended contiguously.",
                )

            event = envelope.event
            new_phase = run["phase"]
            new_outcome = run["outcome"]
            outcome_summary = run["outcome_summary"]
            accepted_at = run["accepted_at"]
            started_at = run["started_at"]
            finished_at = run["finished_at"]
            if event.kind == "phase":
                if event.phase not in PHASE_TRANSITIONS.get(run["phase"], set()):
                    raise api_error(
                        status.HTTP_409_CONFLICT,
                        "invalid_phase_transition",
                        f"Cannot transition from {run['phase']} to {event.phase}.",
                    )
                new_phase = event.phase
                if new_phase == "accepted":
                    accepted_at = received_at
                if new_phase == "running" and started_at is None:
                    started_at = received_at
                if new_phase in {
                    "finished",
                    "interrupted",
                    "cancelled",
                    "rejected",
                    "expired",
                }:
                    finished_at = received_at
            elif event.kind == "outcome":
                if run["phase"] not in {"accepted", "preparing", "running", "verifying"}:
                    raise api_error(
                        status.HTTP_409_CONFLICT,
                        "outcome_not_allowed",
                        "Outcome cannot be reported in the current phase.",
                    )
                new_outcome = event.outcome
                outcome_summary = event.summary

            connection.execute(
                """
                INSERT INTO run_event(
                    event_id, run_id, sequence, event_hash, event_json, received_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    envelope.event_id,
                    run_id,
                    envelope.sequence,
                    event_digest,
                    event_json,
                    received_at,
                ),
            )
            connection.execute(
                """
                UPDATE workflow_run
                SET phase = ?, outcome = ?, outcome_summary = ?,
                    accepted_at = ?, started_at = ?, finished_at = ?,
                    last_event_sequence = ?
                WHERE id = ?
                """,
                (
                    new_phase,
                    new_outcome,
                    outcome_summary,
                    accepted_at,
                    started_at,
                    finished_at,
                    envelope.sequence,
                    run_id,
                ),
            )
        return RunEventResponse(
            duplicate=False,
            last_event_sequence=envelope.sequence,
            phase=new_phase,
            outcome=new_outcome,
        )

    @app.get("/api/runs", response_model=RunListResponse)
    def list_runs() -> RunListResponse:
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT workflow_run.*, workflow.name AS workflow_name,
                    workflow.workflow_mode AS workflow_mode,
                    workflow.category AS workflow_category
                FROM workflow_run
                JOIN workflow ON workflow.id = workflow_run.workflow_id
                ORDER BY workflow_run.created_at DESC, workflow_run.id DESC
                """
            ).fetchall()
        return RunListResponse(runs=[_run_summary(row) for row in rows])

    @app.put(
        "/api/runs/{run_id}/artifacts/{artifact_id}",
        response_model=RunArtifactResponse,
        status_code=201,
    )
    async def upload_run_artifact(
        run_id: str,
        artifact_id: str,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_artifact_kind: Annotated[str | None, Header(alias="X-Artifact-Kind")] = None,
        x_artifact_sha256: Annotated[str | None, Header(alias="X-Artifact-Sha256")] = None,
    ) -> RunArtifactResponse:
        run_for_reporting_capability(run_id, authorization)
        if not re.fullmatch(r"artifact_[a-z0-9_-]{12,64}", artifact_id):
            raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "artifact_id_invalid", "Artifact ID is invalid.")
        if x_artifact_kind not in {"baseline", "review_checkpoint", "final", "failure"}:
            raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "artifact_kind_invalid", "Artifact kind is invalid.")
        if x_artifact_sha256 is None or not re.fullmatch(r"[a-f0-9]{64}", x_artifact_sha256):
            raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "artifact_hash_invalid", "Artifact hash is invalid.")
        if request.headers.get("content-type", "").split(";", 1)[0] != "image/png":
            raise api_error(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "artifact_type_invalid", "Only PNG evidence is supported.")
        content = await request.body()
        if not content or len(content) > 2 * 1024 * 1024:
            raise api_error(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "artifact_size_invalid", "Artifact must be between 1 byte and 2 MiB.")
        actual_hash = hashlib.sha256(content).hexdigest()
        if not secrets.compare_digest(actual_hash, x_artifact_sha256):
            raise api_error(status.HTTP_409_CONFLICT, "artifact_hash_mismatch", "Artifact content does not match its hash.")

        relative_path = f"{run_id}/{artifact_id}.png"
        final_path = (artifact_root / relative_path).resolve()
        if not final_path.is_relative_to(artifact_root):
            raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "artifact_path_invalid", "Artifact path is invalid.")
        with database.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM run_artifact WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if existing is not None:
            if (
                existing["run_id"] != run_id
                or existing["kind"] != x_artifact_kind
                or existing["sha256"] != actual_hash
                or existing["size_bytes"] != len(content)
            ):
                raise api_error(status.HTTP_409_CONFLICT, "artifact_conflict", "Artifact identity already contains different data.")
            return RunArtifactResponse(artifact=artifact_summary(existing), duplicate=True)

        final_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{artifact_id}-",
            suffix=".tmp",
            dir=final_path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, final_path)
            with database.transaction(immediate=True) as connection:
                try:
                    connection.execute(
                        """
                        INSERT INTO run_artifact(
                            artifact_id, run_id, kind, sha256, size_bytes,
                            content_type, relative_path, created_at
                        ) VALUES (?, ?, ?, ?, ?, 'image/png', ?, ?)
                        """,
                        (
                            artifact_id,
                            run_id,
                            x_artifact_kind,
                            actual_hash,
                            len(content),
                            relative_path,
                            now(),
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise api_error(status.HTTP_409_CONFLICT, "artifact_conflict", "Artifact identity already exists.") from error
                stored = connection.execute(
                    "SELECT * FROM run_artifact WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()
        finally:
            temporary_path.unlink(missing_ok=True)
        return RunArtifactResponse(artifact=artifact_summary(stored), duplicate=False)

    @app.get("/api/runs/{run_id}/artifacts/{artifact_id}", response_class=FileResponse)
    def get_run_artifact(run_id: str, artifact_id: str) -> FileResponse:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM run_artifact WHERE run_id = ? AND artifact_id = ?",
                (run_id, artifact_id),
            ).fetchone()
        if row is None:
            raise api_error(status.HTTP_404_NOT_FOUND, "artifact_not_found", "Artifact not found.")
        path = (artifact_root / row["relative_path"]).resolve()
        if not path.is_relative_to(artifact_root) or not path.is_file():
            raise api_error(status.HTTP_404_NOT_FOUND, "artifact_file_missing", "Artifact file is unavailable.")
        return FileResponse(path, media_type="image/png", filename=f"{row['kind']}.png")

    @app.get("/api/runs/{run_id}", response_model=RunDetailResponse)
    def run_detail(run_id: str) -> RunDetailResponse:
        with database.connect() as connection:
            run = connection.execute(
                """
                SELECT workflow_run.*, workflow.name AS workflow_name,
                    workflow.workflow_mode AS workflow_mode,
                    workflow.category AS workflow_category
                FROM workflow_run
                JOIN workflow ON workflow.id = workflow_run.workflow_id
                WHERE workflow_run.id = ?
                """,
                (run_id,),
            ).fetchone()
            if run is None:
                raise api_error(
                    status.HTTP_404_NOT_FOUND,
                    "run_not_found",
                    "Run not found.",
                )
            events = connection.execute(
                "SELECT event_json FROM run_event WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
            artifacts = connection.execute(
                "SELECT * FROM run_artifact WHERE run_id = ? ORDER BY created_at, artifact_id",
                (run_id,),
            ).fetchall()
        return RunDetailResponse(
            run=_run_summary(run),
            snapshot=RunRequestSnapshot.model_validate_json(run["snapshot_json"]),
            events=[RunEventEnvelope.model_validate_json(row["event_json"]) for row in events],
            artifacts=[artifact_summary(row) for row in artifacts],
        )

    portal_dist = selected_config.portal_dist
    if portal_dist is not None and (portal_dist / "index.html").is_file():
        portal_root = portal_dist.resolve()

        @app.get("/{portal_path:path}", include_in_schema=False)
        def portal(portal_path: str) -> FileResponse:
            if portal_path == "api" or portal_path.startswith("api/"):
                raise api_error(status.HTTP_404_NOT_FOUND, "api_route_not_found", "API route not found.")
            candidate = (portal_root / portal_path).resolve()
            if candidate.is_relative_to(portal_root) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(portal_root / "index.html")

    return app


app = create_app()