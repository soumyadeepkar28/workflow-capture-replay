from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from workflow_protocol import (
    ActionRunEvent,
    CheckRunEvent,
    OutcomeRunEvent,
    PhaseRunEvent,
    RunRequestSnapshot,
)

from workflow_companion.store import CompanionStore, PendingPairing, RunnerAssociation


@dataclass(frozen=True)
class PairingLaunch:
    pairing_id: str
    pairing_url: str
    expires_at: int


def _secret(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class CompanionClient:
    def __init__(
        self,
        api_origin: str,
        store: CompanionStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Any = time.time,
    ) -> None:
        parsed = urlsplit(api_origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("api_origin must be an absolute HTTP(S) origin")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("api_origin cannot contain a path, query, or fragment")
        self.api_origin = api_origin.rstrip("/")
        self.store = store
        self.transport = transport
        self.clock = clock

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.api_origin,
            transport=self.transport,
            timeout=httpx.Timeout(10),
        )

    async def initiate_pairing(self, label: str, companion_version: str) -> PairingLaunch:
        self.store.initialize()
        pending_proof = _secret("pending")
        runner_credential = _secret("runner")
        async with self._client() as client:
            response = await client.post(
                "/api/pairings",
                json={
                    "label": label,
                    "pending_proof_digest": _digest(pending_proof),
                    "runner_credential_digest": _digest(runner_credential),
                    "companion_version": companion_version,
                    "protocol_version": 1,
                },
            )
        response.raise_for_status()
        payload = response.json()
        pending = PendingPairing(
            pairing_id=payload["pairing_id"],
            api_origin=self.api_origin,
            pending_proof=pending_proof,
            runner_credential=runner_credential,
            pairing_url=payload["pairing_url"],
            expires_at=payload["expires_at"],
        )
        self.store.save_pending(pending)
        return PairingLaunch(
            pairing_id=pending.pairing_id,
            pairing_url=pending.pairing_url,
            expires_at=pending.expires_at,
        )

    async def refresh_pairing(self) -> str:
        pending = self.store.pending()
        if pending is None:
            raise RuntimeError("no pending pairing")
        async with self._client() as client:
            response = await client.get(
                f"/api/pairings/{pending.pairing_id}/status",
                headers={"Authorization": f"Bearer {pending.pending_proof}"},
            )
        response.raise_for_status()
        payload = response.json()
        if payload["state"] == "connected":
            self.store.complete_pairing(payload["runner_id"], int(self.clock()))
        return payload["state"]

    async def heartbeat(
        self,
        readiness: str,
        companion_version: str,
    ) -> RunnerAssociation:
        association = self.store.association()
        if association is None:
            raise RuntimeError("companion is not paired")
        async with self._client() as client:
            response = await client.post(
                "/api/runner/heartbeat",
                headers={"Authorization": f"Bearer {association.runner_credential}"},
                json={
                    "readiness": readiness,
                    "companion_version": companion_version,
                    "protocol_version": 1,
                },
            )
        response.raise_for_status()
        if response.json()["runner"]["runner_id"] != association.runner_id:
            raise RuntimeError("backend acknowledged a different runner")
        return association

    async def claim_run(self) -> RunRequestSnapshot | None:
        association = self.store.association()
        if association is None:
            raise RuntimeError("companion is not paired")
        proposal = self.store.prepare_claim(int(self.clock()))
        async with self._client() as client:
            response = await client.post(
                "/api/runner/claims",
                headers={"Authorization": f"Bearer {association.runner_credential}"},
                json={
                    "claim_id": proposal.claim_id,
                    "reporting_capability_digest": _digest(
                        proposal.reporting_capability
                    ),
                },
            )
        if response.status_code == 204:
            self.store.clear_pending_claim(proposal.claim_id)
            return None
        response.raise_for_status()
        snapshot = RunRequestSnapshot.model_validate(response.json()["snapshot"])
        self.store.accept_claim(snapshot, proposal, int(self.clock()))
        await self.sync_run_events(snapshot.run_id)
        return snapshot

    async def record_event(
        self,
        run_id: str,
        event: PhaseRunEvent | ActionRunEvent | CheckRunEvent | OutcomeRunEvent,
    ) -> None:
        self.store.append_event(run_id, event, int(self.clock()))
        try:
            await self.sync_run_events(run_id)
        except httpx.RequestError:
            return
        except httpx.HTTPStatusError as error:
            if error.response.status_code < 500:
                raise

    async def sync_run_events(self, run_id: str) -> None:
        local_run = self.store.run(run_id)
        if local_run is None:
            raise RuntimeError("run is not accepted locally")
        async with self._client() as client:
            for envelope in self.store.pending_events(run_id):
                response = await client.post(
                    f"/api/runs/{run_id}/events",
                    headers={
                        "Authorization": f"Bearer {local_run.reporting_capability}"
                    },
                    json=envelope.model_dump(mode="json"),
                )
                response.raise_for_status()
                acknowledgement = response.json()
                if acknowledgement["last_event_sequence"] < envelope.sequence:
                    raise RuntimeError("backend did not durably acknowledge the event")
                self.store.mark_event_synced(envelope.event_id, int(self.clock()))

    async def record_artifact(self, run_id: str, kind: str, path) -> None:
        self.store.save_artifact(
            run_id,
            kind,  # type: ignore[arg-type]
            path,
            int(self.clock()),
        )
        try:
            await self.sync_run_artifacts(run_id)
        except httpx.RequestError:
            return
        except httpx.HTTPStatusError as error:
            if error.response.status_code < 500:
                raise

    async def sync_run_artifacts(self, run_id: str) -> None:
        local_run = self.store.run(run_id)
        if local_run is None:
            raise RuntimeError("run is not accepted locally")
        async with self._client() as client:
            for artifact in self.store.pending_artifacts(run_id):
                content = await asyncio.to_thread(artifact.path.read_bytes)
                if hashlib.sha256(content).hexdigest() != artifact.sha256:
                    raise RuntimeError("local artifact changed before synchronization")
                response = await client.put(
                    f"/api/runs/{run_id}/artifacts/{artifact.artifact_id}",
                    headers={
                        "Authorization": f"Bearer {local_run.reporting_capability}",
                        "Content-Type": "image/png",
                        "X-Artifact-Kind": artifact.kind,
                        "X-Artifact-Sha256": artifact.sha256,
                    },
                    content=content,
                )
                response.raise_for_status()
                if response.json()["artifact"]["sha256"] != artifact.sha256:
                    raise RuntimeError("backend acknowledged a different artifact")
                self.store.mark_artifact_synced(artifact.artifact_id, int(self.clock()))