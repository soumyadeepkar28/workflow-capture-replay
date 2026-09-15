from __future__ import annotations

import os
import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workflow_protocol import (
    ActionRunEvent,
    CheckRunEvent,
    OutcomeRunEvent,
    PhaseRunEvent,
    RunEventEnvelope,
    RunRequestSnapshot,
)


@dataclass(frozen=True)
class PendingPairing:
    pairing_id: str
    api_origin: str
    pending_proof: str
    runner_credential: str
    pairing_url: str
    expires_at: int


@dataclass(frozen=True)
class RunnerAssociation:
    api_origin: str
    runner_id: str
    runner_credential: str
    connected_at: int


@dataclass(frozen=True)
class PendingClaim:
    claim_id: str
    reporting_capability: str
    created_at: int


@dataclass(frozen=True)
class LocalRun:
    run_id: str
    snapshot: RunRequestSnapshot
    reporting_capability: str
    phase: str
    outcome: str | None
    accepted_at: int
    last_event_sequence: int


@dataclass(frozen=True)
class LocalArtifact:
    artifact_id: str
    run_id: str
    kind: Literal["baseline", "review_checkpoint", "final", "failure"]
    path: Path
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class LocalOperation:
    operation_id: str
    kind: Literal["prepare_capture"]
    status: Literal["pending", "running", "completed", "failed"]
    error_message: str | None


class CompanionStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS companion_pending_pairing (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    pairing_id TEXT NOT NULL,
                    api_origin TEXT NOT NULL,
                    pending_proof TEXT NOT NULL,
                    runner_credential TEXT NOT NULL,
                    pairing_url TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS companion_association (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    api_origin TEXT NOT NULL,
                    runner_id TEXT NOT NULL,
                    runner_credential TEXT NOT NULL,
                    connected_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS companion_pending_claim (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    claim_id TEXT NOT NULL UNIQUE,
                    reporting_capability TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS companion_run (
                    run_id TEXT PRIMARY KEY,
                    snapshot_hash TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    reporting_capability TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    outcome TEXT,
                    accepted_at INTEGER NOT NULL,
                    last_event_sequence INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS companion_run_event (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES companion_run(run_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    event_hash TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    sync_status TEXT NOT NULL CHECK (sync_status IN ('pending', 'synced', 'blocked')),
                    created_at INTEGER NOT NULL,
                    synced_at INTEGER,
                    UNIQUE (run_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS companion_run_artifact (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES companion_run(run_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK (kind IN (
                        'baseline', 'review_checkpoint', 'final', 'failure'
                    )),
                    path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sync_status TEXT NOT NULL CHECK (sync_status IN ('pending', 'synced', 'blocked')),
                    created_at INTEGER NOT NULL,
                    synced_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS companion_operation (
                    operation_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind = 'prepare_capture'),
                    status TEXT NOT NULL CHECK (status IN (
                        'pending', 'running', 'completed', 'failed'
                    )),
                    error_message TEXT,
                    created_at INTEGER NOT NULL,
                    started_at INTEGER,
                    finished_at INTEGER
                );
                """
            )
        os.chmod(self.path, 0o600)

    def save_pending(self, pairing: PendingPairing) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM companion_pending_pairing")
            connection.execute(
                """
                INSERT INTO companion_pending_pairing(
                    singleton, pairing_id, api_origin, pending_proof,
                    runner_credential, pairing_url, expires_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pairing.pairing_id,
                    pairing.api_origin,
                    pairing.pending_proof,
                    pairing.runner_credential,
                    pairing.pairing_url,
                    pairing.expires_at,
                ),
            )
            connection.commit()

    def pending(self) -> PendingPairing | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT pairing_id, api_origin, pending_proof, runner_credential,
                       pairing_url, expires_at
                FROM companion_pending_pairing
                WHERE singleton = 1
                """
            ).fetchone()
        return PendingPairing(**dict(row)) if row is not None else None

    def complete_pairing(self, runner_id: str, connected_at: int) -> RunnerAssociation:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            pending = connection.execute(
                "SELECT * FROM companion_pending_pairing WHERE singleton = 1"
            ).fetchone()
            if pending is None:
                connection.rollback()
                raise RuntimeError("no pending pairing to complete")
            connection.execute("DELETE FROM companion_association")
            connection.execute(
                """
                INSERT INTO companion_association(
                    singleton, api_origin, runner_id, runner_credential, connected_at
                ) VALUES (1, ?, ?, ?, ?)
                """,
                (
                    pending["api_origin"],
                    runner_id,
                    pending["runner_credential"],
                    connected_at,
                ),
            )
            connection.execute("DELETE FROM companion_pending_pairing")
            connection.commit()
        association = self.association()
        if association is None:
            raise RuntimeError("pairing completion was not persisted")
        return association

    def association(self) -> RunnerAssociation | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM companion_association WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return None
        return RunnerAssociation(
            api_origin=row["api_origin"],
            runner_id=row["runner_id"],
            runner_credential=row["runner_credential"],
            connected_at=row["connected_at"],
        )

    def clear_association(self) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM companion_pending_pairing")
            connection.execute("DELETE FROM companion_association")
            connection.commit()

    def pending_claim(self) -> PendingClaim | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT claim_id, reporting_capability, created_at
                FROM companion_pending_claim
                WHERE singleton = 1
                """
            ).fetchone()
        return PendingClaim(**dict(row)) if row is not None else None

    def prepare_claim(self, created_at: int) -> PendingClaim:
        existing = self.pending_claim()
        if existing is not None:
            return existing
        proposal = PendingClaim(
            claim_id=f"claim_{secrets.token_hex(16)}",
            reporting_capability=f"report_{secrets.token_urlsafe(32)}",
            created_at=created_at,
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT claim_id, reporting_capability, created_at "
                "FROM companion_pending_claim WHERE singleton = 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO companion_pending_claim(
                        singleton, claim_id, reporting_capability, created_at
                    ) VALUES (1, ?, ?, ?)
                    """,
                    (
                        proposal.claim_id,
                        proposal.reporting_capability,
                        proposal.created_at,
                    ),
                )
            else:
                proposal = PendingClaim(**dict(row))
            connection.commit()
        return proposal

    def clear_pending_claim(self, claim_id: str) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM companion_pending_claim WHERE claim_id = ?",
                (claim_id,),
            )
            connection.commit()

    @staticmethod
    def _canonical_hash(value: Any) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def accept_claim(
        self,
        snapshot: RunRequestSnapshot,
        proposal: PendingClaim,
        accepted_at: int,
    ) -> LocalRun:
        snapshot_value = snapshot.model_dump(mode="json")
        snapshot_hash = self._canonical_hash(snapshot_value)
        observed_at = datetime.fromtimestamp(accepted_at, tz=timezone.utc)
        accepted_event = RunEventEnvelope(
            event_id=f"event_{secrets.token_hex(16)}",
            sequence=0,
            event=PhaseRunEvent(
                kind="phase",
                phase="accepted",
                observed_at=observed_at,
            ),
        )
        event_value = accepted_event.model_dump(mode="json")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            pending = connection.execute(
                "SELECT * FROM companion_pending_claim WHERE claim_id = ?",
                (proposal.claim_id,),
            ).fetchone()
            if pending is None or pending["reporting_capability"] != proposal.reporting_capability:
                connection.rollback()
                raise RuntimeError("claim proposal is not durably pending")
            existing = connection.execute(
                "SELECT * FROM companion_run WHERE run_id = ?",
                (snapshot.run_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["snapshot_hash"] != snapshot_hash
                    or existing["reporting_capability"] != proposal.reporting_capability
                ):
                    connection.rollback()
                    raise RuntimeError("run identity conflicts with local accepted state")
            else:
                connection.execute(
                    """
                    INSERT INTO companion_run(
                        run_id, snapshot_hash, snapshot_json, reporting_capability,
                        phase, accepted_at, last_event_sequence
                    ) VALUES (?, ?, ?, ?, 'accepted', ?, 0)
                    """,
                    (
                        snapshot.run_id,
                        snapshot_hash,
                        snapshot.model_dump_json(),
                        proposal.reporting_capability,
                        accepted_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO companion_run_event(
                        event_id, run_id, sequence, event_hash, event_json,
                        sync_status, created_at
                    ) VALUES (?, ?, 0, ?, ?, 'pending', ?)
                    """,
                    (
                        accepted_event.event_id,
                        snapshot.run_id,
                        self._canonical_hash(event_value),
                        accepted_event.model_dump_json(),
                        accepted_at,
                    ),
                )
            connection.execute(
                "DELETE FROM companion_pending_claim WHERE claim_id = ?",
                (proposal.claim_id,),
            )
            connection.commit()
        accepted = self.run(snapshot.run_id)
        if accepted is None:
            raise RuntimeError("accepted run was not persisted")
        return accepted

    def run(self, run_id: str) -> LocalRun | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM companion_run WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return LocalRun(
            run_id=row["run_id"],
            snapshot=RunRequestSnapshot.model_validate_json(row["snapshot_json"]),
            reporting_capability=row["reporting_capability"],
            phase=row["phase"],
            outcome=row["outcome"],
            accepted_at=row["accepted_at"],
            last_event_sequence=row["last_event_sequence"],
        )

    def append_event(
        self,
        run_id: str,
        event: PhaseRunEvent | ActionRunEvent | CheckRunEvent | OutcomeRunEvent,
        created_at: int,
    ) -> RunEventEnvelope:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM companion_run WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                connection.rollback()
                raise RuntimeError("run is not accepted locally")
            sequence = run["last_event_sequence"] + 1
            envelope = RunEventEnvelope(
                event_id=f"event_{secrets.token_hex(16)}",
                sequence=sequence,
                event=event,
            )
            event_value = envelope.model_dump(mode="json")
            phase = event.phase if event.kind == "phase" else run["phase"]
            outcome = event.outcome if event.kind == "outcome" else run["outcome"]
            connection.execute(
                """
                INSERT INTO companion_run_event(
                    event_id, run_id, sequence, event_hash, event_json,
                    sync_status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    envelope.event_id,
                    run_id,
                    sequence,
                    self._canonical_hash(event_value),
                    envelope.model_dump_json(),
                    created_at,
                ),
            )
            connection.execute(
                """
                UPDATE companion_run
                SET phase = ?, outcome = ?, last_event_sequence = ?
                WHERE run_id = ?
                """,
                (phase, outcome, sequence, run_id),
            )
            connection.commit()
        return envelope

    def pending_events(self, run_id: str) -> list[RunEventEnvelope]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT event_json FROM companion_run_event
                WHERE run_id = ? AND sync_status = 'pending'
                ORDER BY sequence
                """,
                (run_id,),
            ).fetchall()
        return [RunEventEnvelope.model_validate_json(row["event_json"]) for row in rows]

    def mark_event_synced(self, event_id: str, synced_at: int) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE companion_run_event
                SET sync_status = 'synced', synced_at = ?
                WHERE event_id = ? AND sync_status = 'pending'
                """,
                (synced_at, event_id),
            )
            if updated.rowcount != 1:
                existing = connection.execute(
                    "SELECT sync_status FROM companion_run_event WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if existing is None or existing["sync_status"] != "synced":
                    connection.rollback()
                    raise RuntimeError("event acknowledgement does not match local outbox")
            connection.commit()

    def save_artifact(
        self,
        run_id: str,
        kind: Literal["baseline", "review_checkpoint", "final", "failure"],
        path: Path,
        created_at: int,
    ) -> LocalArtifact:
        resolved = path.resolve()
        content = resolved.read_bytes()
        if not content or len(content) > 2 * 1024 * 1024:
            raise RuntimeError("local artifact must be between 1 byte and 2 MiB")
        artifact = LocalArtifact(
            artifact_id=f"artifact_{secrets.token_hex(16)}",
            run_id=run_id,
            kind=kind,
            path=resolved,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM companion_run WHERE run_id = ?",
                (run_id,),
            ).fetchone() is None:
                connection.rollback()
                raise RuntimeError("cannot attach evidence to an unknown local run")
            connection.execute(
                """
                INSERT INTO companion_run_artifact(
                    artifact_id, run_id, kind, path, sha256, size_bytes,
                    sync_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.run_id,
                    artifact.kind,
                    str(artifact.path),
                    artifact.sha256,
                    artifact.size_bytes,
                    created_at,
                ),
            )
            connection.commit()
        return artifact

    def pending_artifacts(self, run_id: str) -> list[LocalArtifact]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT artifact_id, run_id, kind, path, sha256, size_bytes
                FROM companion_run_artifact
                WHERE run_id = ? AND sync_status = 'pending'
                ORDER BY created_at, artifact_id
                """,
                (run_id,),
            ).fetchall()
        return [
            LocalArtifact(
                artifact_id=row["artifact_id"],
                run_id=row["run_id"],
                kind=row["kind"],
                path=Path(row["path"]),
                sha256=row["sha256"],
                size_bytes=row["size_bytes"],
            )
            for row in rows
        ]

    def mark_artifact_synced(self, artifact_id: str, synced_at: int) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE companion_run_artifact
                SET sync_status = 'synced', synced_at = ?
                WHERE artifact_id = ? AND sync_status = 'pending'
                """,
                (synced_at, artifact_id),
            )
            if updated.rowcount != 1:
                existing = connection.execute(
                    "SELECT sync_status FROM companion_run_artifact WHERE artifact_id = ?",
                    (artifact_id,),
                ).fetchone()
                if existing is None or existing["sync_status"] != "synced":
                    connection.rollback()
                    raise RuntimeError("artifact acknowledgement does not match local outbox")
            connection.commit()

    def unfinished_runs(self) -> list[LocalRun]:
        terminal = ("finished", "interrupted", "cancelled", "rejected", "expired")
        placeholders = ",".join("?" for _ in terminal)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT run_id FROM companion_run WHERE phase NOT IN ({placeholders}) "
                "ORDER BY accepted_at",
                terminal,
            ).fetchall()
        return [local for row in rows if (local := self.run(row["run_id"])) is not None]

    @staticmethod
    def _local_operation(row: sqlite3.Row) -> LocalOperation:
        return LocalOperation(
            operation_id=row["operation_id"],
            kind=row["kind"],
            status=row["status"],
            error_message=row["error_message"],
        )

    def request_operation(self, kind: Literal["prepare_capture"], created_at: int) -> LocalOperation:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM companion_operation
                WHERE status IN ('pending', 'running')
                ORDER BY created_at LIMIT 1
                """
            ).fetchone()
            if existing is not None:
                connection.rollback()
                raise RuntimeError("another local operation is already pending or running")
            operation_id = f"operation_{secrets.token_hex(16)}"
            connection.execute(
                """
                INSERT INTO companion_operation(
                    operation_id, kind, status, created_at
                ) VALUES (?, ?, 'pending', ?)
                """,
                (operation_id, kind, created_at),
            )
            row = connection.execute(
                "SELECT * FROM companion_operation WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            connection.commit()
        return self._local_operation(row)

    def claim_operation(self, started_at: int) -> LocalOperation | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM companion_operation
                WHERE status = 'pending'
                ORDER BY created_at, operation_id LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            updated = connection.execute(
                """
                UPDATE companion_operation
                SET status = 'running', started_at = ?
                WHERE operation_id = ? AND status = 'pending'
                """,
                (started_at, row["operation_id"]),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return None
            claimed = connection.execute(
                "SELECT * FROM companion_operation WHERE operation_id = ?",
                (row["operation_id"],),
            ).fetchone()
            connection.commit()
        return self._local_operation(claimed)

    def finish_operation(
        self,
        operation_id: str,
        *,
        error_message: str | None,
        finished_at: int,
    ) -> None:
        status = "failed" if error_message else "completed"
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE companion_operation
                SET status = ?, error_message = ?, finished_at = ?
                WHERE operation_id = ? AND status = 'running'
                """,
                (status, error_message, finished_at, operation_id),
            )
            if updated.rowcount != 1:
                connection.rollback()
                raise RuntimeError("local operation is not running")
            connection.commit()

    def operation(self, operation_id: str) -> LocalOperation | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM companion_operation WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return self._local_operation(row) if row is not None else None