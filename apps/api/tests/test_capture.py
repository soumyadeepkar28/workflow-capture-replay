from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from workflow_api import ApiConfig, create_app
from workflow_api.security import digest_secret, new_secret


@dataclass
class MutableClock:
    value: float = 2_000.0

    def __call__(self) -> float:
        return self.value


def pair_ready_runner(client: TestClient) -> tuple[str, str]:
    session = client.get("/api/session").json()
    pending = new_secret("pending")
    runner = new_secret("runner")
    pairing = client.post(
        "/api/pairings",
        json={
            "label": "Capture runner",
            "pending_proof_digest": digest_secret(pending),
            "runner_credential_digest": digest_secret(runner),
            "companion_version": "0.1.0",
            "protocol_version": 1,
        },
    ).json()
    ticket = parse_qs(urlsplit(pairing["pairing_url"]).fragment)["ticket"][0]
    headers = {"Origin": "http://testserver", "X-CSRF-Token": session["csrf_token"]}
    assert client.post("/api/pairings/connect", json={"ticket": ticket}, headers=headers).status_code == 200
    assert client.post(
        "/api/runner/heartbeat",
        headers={"Authorization": f"Bearer {runner}"},
        json={"readiness": "ready", "companion_version": "0.1.0", "protocol_version": 1},
    ).status_code == 200
    return session["csrf_token"], runner


def fixture_actions() -> list[dict[str, object]]:
    path = Path(__file__).parents[3] / "packages" / "protocol" / "fixtures" / "workflow_base.json"
    return json.loads(path.read_text(encoding="utf-8"))["actions"]


def test_capture_batches_finalize_and_save_an_immutable_workflow(tmp_path: Path) -> None:
    clock = MutableClock()
    app = create_app(
        ApiConfig(tmp_path / "api.sqlite3", "http://testserver", False),
        clock=clock,
    )
    with TestClient(app, base_url="http://testserver") as client:
        csrf_token, _ = pair_ready_runner(client)
        portal_headers = {"Origin": "http://testserver", "X-CSRF-Token": csrf_token}
        draft = client.post("/api/captures", headers=portal_headers).json()
        capture_headers = {"Authorization": f"Bearer {draft['upload_capability']}"}
        actions = fixture_actions()
        first_batch = {
            "batch_id": "batch_example000001",
            "batch_index": 0,
            "actions": actions,
        }

        uploaded = client.post(
            f"/api/captures/{draft['draft_id']}/batches",
            headers=capture_headers,
            json=first_batch,
        )
        assert uploaded.status_code == 201
        assert uploaded.json()["duplicate"] is False
        duplicate = client.post(
            f"/api/captures/{draft['draft_id']}/batches",
            headers=capture_headers,
            json=first_batch,
        )
        assert duplicate.status_code == 201
        assert duplicate.json()["duplicate"] is True

        conflicting = {**first_batch, "actions": [{**actions[0], "value": "different"}]}
        assert client.post(
            f"/api/captures/{draft['draft_id']}/batches",
            headers=capture_headers,
            json=conflicting,
        ).status_code == 409
        assert client.post(
            f"/api/captures/{draft['draft_id']}/finalize",
            headers=capture_headers,
            json={
                "expected_batch_count": 2,
                "expected_action_count": 2,
                "capture": {
                    "extension_version": "0.1.0",
                    "captured_at": "2026-09-14T12:00:00Z",
                    "document_count": 1,
                    "event_count": 2,
                    "completeness": "complete",
                },
            },
        ).status_code == 409

        finalized = client.post(
            f"/api/captures/{draft['draft_id']}/finalize",
            headers=capture_headers,
            json={
                "expected_batch_count": 1,
                "expected_action_count": 2,
                "capture": {
                    "extension_version": "0.1.0",
                    "captured_at": "2026-09-14T12:00:00Z",
                    "document_count": 1,
                    "event_count": 2,
                    "completeness": "complete",
                },
            },
        )
        assert finalized.status_code == 200
        reviewed_actions = finalized.json()["actions"]
        assert len(reviewed_actions) == 2

        saved = client.post(
            f"/api/captures/{draft['draft_id']}/save",
            headers=portal_headers,
            json={
                "name": "Linked ticket resolution",
                "description": "Review before completion.",
                "category_id": "cat_linked_tickets_v1",
            },
        )
        assert saved.status_code == 201
        workflow = saved.json()["workflow"]
        assert workflow["actions"] == reviewed_actions
        assert workflow["schema_version"] == 3
        assert workflow["category_id"] == "cat_linked_tickets_v1"
        assert workflow["workflow_mode"] == "verified_preset"
        assert workflow["verification_profile"] is not None
        assert len(workflow["content_hash"]) == 64
        assert client.get("/api/workflows").json()["workflows"][0]["workflow_id"] == workflow["workflow_id"]
        assert client.get(f"/api/workflows/{workflow['workflow_id']}").json()["workflow"] == workflow
        assert client.post(
            f"/api/captures/{draft['draft_id']}/save",
            headers=portal_headers,
            json={
                "name": "Changed",
                "description": "",
                "category_id": "cat_linked_tickets_v1",
            },
        ).status_code == 409


def test_capture_can_be_interrupted_but_not_saved(tmp_path: Path) -> None:
    app = create_app(ApiConfig(tmp_path / "api.sqlite3", "http://testserver", False))
    with TestClient(app, base_url="http://testserver") as client:
        csrf_token, _ = pair_ready_runner(client)
        portal_headers = {"Origin": "http://testserver", "X-CSRF-Token": csrf_token}
        draft = client.post("/api/captures", headers=portal_headers).json()
        capability = {"Authorization": f"Bearer {draft['upload_capability']}"}
        active_status = client.get(
            f"/api/captures/{draft['draft_id']}/status",
            headers=capability,
        )
        assert active_status.status_code == 200
        assert active_status.json()["status"] == "active"
        duplicate_start = client.post("/api/captures", headers=portal_headers)
        assert duplicate_start.status_code == 409
        assert duplicate_start.json()["detail"]["code"] == "capture_already_active"
        recovered = client.post("/api/capture-recovery", headers=portal_headers)
        assert recovered.status_code == 200
        assert recovered.json()["interrupted_count"] == 1
        interrupted_status = client.get(
            f"/api/captures/{draft['draft_id']}/status",
            headers=capability,
        )
        assert interrupted_status.status_code == 200
        assert interrupted_status.json()["status"] == "interrupted"
        interrupted = client.post(
            f"/api/captures/{draft['draft_id']}/interrupt",
            headers=capability,
            json={"reason": "target tab left the supported route"},
        )
        assert interrupted.status_code == 200
        assert interrupted.json()["status"] == "interrupted"
        assert client.post(
            f"/api/captures/{draft['draft_id']}/interrupt",
            headers=capability,
            json={"reason": "retry"},
        ).status_code == 200
        assert client.post(
            f"/api/captures/{draft['draft_id']}/save",
            headers=portal_headers,
            json={
                "name": "Must not save",
                "description": "",
                "category_id": "cat_linked_tickets_v1",
            },
        ).status_code == 409
        assert client.post("/api/captures", headers=portal_headers).status_code == 201


def test_capture_category_is_assigned_only_when_saving(tmp_path: Path) -> None:
    app = create_app(ApiConfig(tmp_path / "api.sqlite3", "http://testserver", False))
    with TestClient(app, base_url="http://testserver") as client:
        csrf_token, _ = pair_ready_runner(client)
        portal_headers = {"Origin": "http://testserver", "X-CSRF-Token": csrf_token}
        categories = client.get("/api/workflow-categories").json()["categories"]
        assert len(categories) == 1
        assert categories[0]["category_id"] == "cat_linked_tickets_v1"
        assert categories[0]["verification_profile"]["profile_id"] == "helpdesk-linked-tickets-v1"

        draft_response = client.post("/api/captures", headers=portal_headers)
        assert draft_response.status_code == 201
        draft = draft_response.json()
        assert "workflow_mode" not in draft
        assert "category" not in draft
        capture_headers = {"Authorization": f"Bearer {draft['upload_capability']}"}
        actions = fixture_actions()
        assert client.post(
            f"/api/captures/{draft['draft_id']}/batches",
            headers=capture_headers,
            json={
                "batch_id": "batch_general000001",
                "batch_index": 0,
                "actions": actions,
            },
        ).status_code == 201
        finalized = client.post(
            f"/api/captures/{draft['draft_id']}/finalize",
            headers=capture_headers,
            json={
                "expected_batch_count": 1,
                "expected_action_count": len(actions),
                "capture": {
                    "extension_version": "0.1.0",
                    "captured_at": "2026-09-14T12:00:00Z",
                    "document_count": 1,
                    "event_count": len(actions),
                    "completeness": "complete",
                },
            },
        )
        assert finalized.status_code == 200
        assert "category" not in finalized.json()

        saved = client.post(
            f"/api/captures/{draft['draft_id']}/save",
            headers=portal_headers,
            json={
                "name": "Create support request",
                "description": "General staff flow.",
                "new_category_name": "Ticket intake",
            },
        )
        assert saved.status_code == 201
        workflow = saved.json()["workflow"]
        assert workflow["schema_version"] == 3
        assert workflow["workflow_mode"] == "general"
        assert workflow["category_id"].startswith("cat_")
        assert workflow["category"] == "Ticket intake"
        assert workflow["verification_profile"] is None
        summary = client.get("/api/workflows").json()["workflows"][0]
        assert summary["workflow_mode"] == "general"
        assert summary["category_id"] == workflow["category_id"]
        assert summary["category"] == "Ticket intake"
        updated_categories = client.get("/api/workflow-categories").json()["categories"]
        assert [item["name"] for item in updated_categories] == [
            "Linked-ticket resolution",
            "Ticket intake",
        ]
        assert updated_categories[1]["verification_profile"] is None

        second_draft = client.post("/api/captures", headers=portal_headers).json()
        second_capture_headers = {
            "Authorization": f"Bearer {second_draft['upload_capability']}"
        }
        assert client.post(
            f"/api/captures/{second_draft['draft_id']}/batches",
            headers=second_capture_headers,
            json={
                "batch_id": "batch_general000002",
                "batch_index": 0,
                "actions": actions,
            },
        ).status_code == 201
        assert client.post(
            f"/api/captures/{second_draft['draft_id']}/finalize",
            headers=second_capture_headers,
            json={
                "expected_batch_count": 1,
                "expected_action_count": len(actions),
                "capture": {
                    "extension_version": "0.1.0",
                    "captured_at": "2026-09-14T12:01:00Z",
                    "document_count": 1,
                    "event_count": len(actions),
                    "completeness": "complete",
                },
            },
        ).status_code == 200
        duplicate_name = client.post(
            f"/api/captures/{second_draft['draft_id']}/save",
            headers=portal_headers,
            json={
                "name": "Duplicate category attempt",
                "new_category_name": "ticket INTAKE",
            },
        )
        assert duplicate_name.status_code == 409
        assert duplicate_name.json()["detail"]["code"] == "workflow_category_name_exists"
        reused = client.post(
            f"/api/captures/{second_draft['draft_id']}/save",
            headers=portal_headers,
            json={
                "name": "Reuse ticket intake",
                "category_id": workflow["category_id"],
            },
        )
        assert reused.status_code == 201
        assert reused.json()["workflow"]["category_id"] == workflow["category_id"]
        assert reused.json()["workflow"]["verification_profile"] is None


def test_capture_requires_recent_runner_readiness(tmp_path: Path) -> None:
    clock = MutableClock()
    app = create_app(
        ApiConfig(tmp_path / "api.sqlite3", "http://testserver", False),
        clock=clock,
    )
    with TestClient(app, base_url="http://testserver") as client:
        csrf_token, _ = pair_ready_runner(client)
        clock.value += 11
        headers = {"Origin": "http://testserver", "X-CSRF-Token": csrf_token}
        assert client.get("/api/session").json()["runner"]["readiness"] == "offline"
        response = client.post("/api/captures", headers=headers)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "runner_not_ready"