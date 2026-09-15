from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from workflow_api import ApiConfig, create_app
from workflow_api.security import digest_secret, new_secret
from workflow_api.workflows import build_workflow_definition, verification_profile
from workflow_protocol import (
    CaptureProvenance,
    VERIFIED_PRESET_CATEGORY,
    VERIFIED_PRESET_CATEGORY_ID,
)


@dataclass
class MutableClock:
    value: float = 3_000.0

    def __call__(self) -> float:
        return self.value


def prepare(client: TestClient, app) -> tuple[str, str, str]:
    session = client.get("/api/session").json()
    pending = new_secret("pending")
    runner = new_secret("runner")
    pairing = client.post(
        "/api/pairings",
        json={
            "label": "Replay runner",
            "pending_proof_digest": digest_secret(pending),
            "runner_credential_digest": digest_secret(runner),
            "companion_version": "0.1.0",
            "protocol_version": 1,
        },
    ).json()
    ticket = parse_qs(urlsplit(pairing["pairing_url"]).fragment)["ticket"][0]
    headers = {"Origin": "http://testserver", "X-CSRF-Token": session["csrf_token"]}
    assert client.post("/api/pairings/connect", headers=headers, json={"ticket": ticket}).status_code == 200
    assert client.post(
        "/api/runner/heartbeat",
        headers={"Authorization": f"Bearer {runner}"},
        json={"readiness": "ready", "companion_version": "0.1.0", "protocol_version": 1},
    ).status_code == 200

    fixture = Path(__file__).parents[3] / "packages" / "protocol" / "fixtures" / "workflow_base.json"
    actions = json.loads(fixture.read_text(encoding="utf-8"))["actions"]
    workflow = build_workflow_definition(
        workflow_id="wf_runfixture0001",
        created_at=3_000,
        name="Replay fixture",
        description="Captured linked-ticket path.",
        capture=CaptureProvenance.model_validate(
            {
                "extension_version": "0.1.0",
                "captured_at": "2026-09-14T12:00:00Z",
                "document_count": 2,
                "event_count": len(actions),
                "completeness": "complete",
            }
        ),
        actions=actions,
        category_id=VERIFIED_PRESET_CATEGORY_ID,
        category=VERIFIED_PRESET_CATEGORY,
        category_verification_profile=verification_profile(),
    )
    with app.state.database.transaction(immediate=True) as connection:
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
                3_000,
                workflow.workflow_mode,
                workflow.category,
                workflow.category_id,
            ),
        )
    return session["csrf_token"], runner, workflow.workflow_id


def phase_event(sequence: int, phase: str) -> dict[str, object]:
    return {
        "event_id": f"event_phase{sequence:012d}",
        "sequence": sequence,
        "event": {
            "kind": "phase",
            "phase": phase,
            "observed_at": "2026-09-14T12:00:00Z",
        },
    }


def test_run_is_idempotently_claimed_and_accepts_contiguous_events(tmp_path: Path) -> None:
    clock = MutableClock()
    app = create_app(ApiConfig(tmp_path / "api.sqlite3", "http://testserver", False), clock=clock)
    with TestClient(app, base_url="http://testserver") as client:
        csrf, runner, workflow_id = prepare(client, app)
        portal_headers = {"Origin": "http://testserver", "X-CSRF-Token": csrf}
        request = {"client_request_key": "request_example000001"}
        created = client.post(f"/api/workflows/{workflow_id}/runs", headers=portal_headers, json=request)
        assert created.status_code == 201
        run_id = created.json()["run"]["run_id"]
        assert client.post(f"/api/workflows/{workflow_id}/runs", headers=portal_headers, json=request).json()["run"]["run_id"] == run_id

        report = new_secret("report")
        claim = {
            "claim_id": "claim_example000001",
            "reporting_capability_digest": digest_secret(report),
        }
        runner_headers = {"Authorization": f"Bearer {runner}"}
        claimed = client.post("/api/runner/claims", headers=runner_headers, json=claim)
        assert claimed.status_code == 200
        assert claimed.json()["snapshot"]["run_id"] == run_id
        assert client.post("/api/runner/claims", headers=runner_headers, json=claim).json() == claimed.json()
        assert client.post(
            "/api/runner/claims",
            headers=runner_headers,
            json={
                "claim_id": "claim_example000002",
                "reporting_capability_digest": digest_secret(new_secret("report")),
            },
        ).status_code == 204

        report_headers = {"Authorization": f"Bearer {report}"}
        accepted = client.post(f"/api/runs/{run_id}/events", headers=report_headers, json=phase_event(0, "accepted"))
        assert accepted.status_code == 201
        assert accepted.json()["phase"] == "accepted"
        duplicate = client.post(f"/api/runs/{run_id}/events", headers=report_headers, json=phase_event(0, "accepted"))
        assert duplicate.status_code == 201 and duplicate.json()["duplicate"] is True
        assert client.post(f"/api/runs/{run_id}/events", headers=report_headers, json=phase_event(2, "preparing")).status_code == 409
        assert client.post(f"/api/runs/{run_id}/events", headers=report_headers, json=phase_event(1, "running")).status_code == 409

        sequence = 1
        for phase in ("preparing", "running", "verifying"):
            response = client.post(
                f"/api/runs/{run_id}/events",
                headers=report_headers,
                json=phase_event(sequence, phase),
            )
            assert response.status_code == 201
            sequence += 1
        outcome = {
            "event_id": "event_outcome000001",
            "sequence": sequence,
            "event": {
                "kind": "outcome",
                "outcome": "succeeded",
                "summary": "All required Helpdesk conditions were verified.",
                "observed_at": "2026-09-14T12:00:00Z",
            },
        }
        assert client.post(f"/api/runs/{run_id}/events", headers=report_headers, json=outcome).status_code == 201
        sequence += 1
        assert client.post(
            f"/api/runs/{run_id}/events",
            headers=report_headers,
            json=phase_event(sequence, "finished"),
        ).status_code == 201

        png = b"\x89PNG\r\n\x1a\nsynthetic-test-image"
        artifact_hash = hashlib.sha256(png).hexdigest()
        artifact_headers = {
            **report_headers,
            "Content-Type": "image/png",
            "X-Artifact-Kind": "baseline",
            "X-Artifact-Sha256": artifact_hash,
        }
        artifact_url = f"/api/runs/{run_id}/artifacts/artifact_example000001"
        artifact = client.put(artifact_url, headers=artifact_headers, content=png)
        assert artifact.status_code == 201
        assert artifact.json()["duplicate"] is False
        assert client.put(artifact_url, headers=artifact_headers, content=png).json()["duplicate"] is True
        assert client.get(artifact_url).content == png

        detail = client.get(f"/api/runs/{run_id}").json()
        assert detail["run"]["phase"] == "finished"
        assert detail["run"]["outcome"] == "succeeded"
        assert len(detail["events"]) == sequence + 1
        assert detail["artifacts"][0]["kind"] == "baseline"
        assert client.get("/api/runs").json()["runs"][0]["run_id"] == run_id


def test_unclaimed_run_expires_without_becoming_an_attempt(tmp_path: Path) -> None:
    clock = MutableClock()
    app = create_app(ApiConfig(tmp_path / "api.sqlite3", "http://testserver", False), clock=clock)
    with TestClient(app, base_url="http://testserver") as client:
        csrf, runner, workflow_id = prepare(client, app)
        created = client.post(
            f"/api/workflows/{workflow_id}/runs",
            headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
            json={"client_request_key": "request_expiring000001"},
        ).json()
        clock.value += 61
        response = client.post(
            "/api/runner/claims",
            headers={"Authorization": f"Bearer {runner}"},
            json={
                "claim_id": "claim_expiring000001",
                "reporting_capability_digest": digest_secret(new_secret("report")),
            },
        )
        assert response.status_code == 204
        detail = client.get(f"/api/runs/{created['run']['run_id']}").json()
        assert detail["run"]["phase"] == "expired"
        assert detail["run"]["outcome"] is None
        assert detail["events"] == []