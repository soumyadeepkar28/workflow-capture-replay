from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from workflow_api import ApiConfig, create_app
from workflow_api.security import digest_secret, new_secret


@dataclass
class MutableClock:
    value: float = 1_000.0

    def __call__(self) -> float:
        return self.value


def csrf_headers(client: TestClient, csrf_token: str, origin: str = "http://testserver") -> dict[str, str]:
    return {"Origin": origin, "X-CSRF-Token": csrf_token}


def test_link_pairing_requires_explicit_connect_and_separates_credentials(tmp_path: Path) -> None:
    clock = MutableClock()
    app = create_app(
        ApiConfig(
            database_path=tmp_path / "api.sqlite3",
            public_origin="http://testserver",
            cookie_secure=False,
        ),
        clock=clock,
    )
    pending_proof = new_secret("pending")
    runner_credential = new_secret("runner")

    with TestClient(app, base_url="http://testserver") as client:
        session = client.get("/api/session")
        assert session.status_code == 200
        assert client.cookies.get("workflow_portal_session")
        csrf_token = session.json()["csrf_token"]

        initiated = client.post(
            "/api/pairings",
            json={
                "label": "Local replay runner",
                "pending_proof_digest": digest_secret(pending_proof),
                "runner_credential_digest": digest_secret(runner_credential),
                "companion_version": "0.1.0",
                "protocol_version": 1,
            },
        )
        assert initiated.status_code == 201
        pairing = initiated.json()
        split_url = urlsplit(pairing["pairing_url"])
        assert split_url.path == "/pair"
        assert split_url.query == ""
        ticket = parse_qs(split_url.fragment)["ticket"][0]

        pending = client.get(
            f"/api/pairings/{pairing['pairing_id']}/status",
            headers={"Authorization": f"Bearer {pending_proof}"},
        )
        assert pending.json()["state"] == "pending"

        preview = client.post(
            "/api/pairings/preview",
            json={"ticket": ticket},
            headers=csrf_headers(client, csrf_token),
        )
        assert preview.status_code == 200
        assert preview.json()["label"] == "Local replay runner"
        assert client.get(
            f"/api/pairings/{pairing['pairing_id']}/status",
            headers={"Authorization": f"Bearer {pending_proof}"},
        ).json()["state"] == "pending"

        rejected = client.post(
            "/api/pairings/connect",
            json={"ticket": ticket},
            headers=csrf_headers(client, csrf_token, origin="https://example.invalid"),
        )
        assert rejected.status_code == 403

        connected = client.post(
            "/api/pairings/connect",
            json={"ticket": ticket},
            headers=csrf_headers(client, csrf_token),
        )
        assert connected.status_code == 200
        runner_id = connected.json()["runner"]["runner_id"]

        status_response = client.get(
            f"/api/pairings/{pairing['pairing_id']}/status",
            headers={"Authorization": f"Bearer {pending_proof}"},
        )
        assert status_response.json() == {
            "pairing_id": pairing["pairing_id"],
            "state": "connected",
            "runner_id": runner_id,
        }

        heartbeat = client.post(
            "/api/runner/heartbeat",
            headers={"Authorization": f"Bearer {runner_credential}"},
            json={
                "readiness": "ready",
                "companion_version": "0.1.0",
                "protocol_version": 1,
            },
        )
        assert heartbeat.status_code == 200
        assert heartbeat.json()["runner"]["readiness"] == "ready"

        refreshed_session = client.get("/api/session").json()
        assert refreshed_session["runner"]["runner_id"] == runner_id
        assert refreshed_session["runner"]["readiness"] == "ready"

        reused = client.post(
            "/api/pairings/connect",
            json={"ticket": ticket},
            headers=csrf_headers(client, csrf_token),
        )
        assert reused.status_code == 409
        assert client.post(
            "/api/runner/heartbeat",
            headers={"Authorization": "Bearer runner_invalid"},
            json={
                "readiness": "ready",
                "companion_version": "0.1.0",
                "protocol_version": 1,
            },
        ).status_code == 401


def test_pairing_expires_without_granting_runner_authority(tmp_path: Path) -> None:
    clock = MutableClock()
    app = create_app(
        ApiConfig(
            database_path=tmp_path / "api.sqlite3",
            public_origin="http://testserver",
            cookie_secure=False,
            pairing_ttl_seconds=5,
        ),
        clock=clock,
    )
    pending_proof = new_secret("pending")
    runner_credential = new_secret("runner")

    with TestClient(app, base_url="http://testserver") as client:
        initiated = client.post(
            "/api/pairings",
            json={
                "label": "Expiring runner",
                "pending_proof_digest": digest_secret(pending_proof),
                "runner_credential_digest": digest_secret(runner_credential),
                "companion_version": "0.1.0",
                "protocol_version": 1,
            },
        ).json()
        ticket = parse_qs(urlsplit(initiated["pairing_url"]).fragment)["ticket"][0]
        session = client.get("/api/session").json()
        clock.value += 6

        assert client.get(
            f"/api/pairings/{initiated['pairing_id']}/status",
            headers={"Authorization": f"Bearer {pending_proof}"},
        ).json()["state"] == "expired"
        assert client.post(
            "/api/pairings/connect",
            json={"ticket": ticket},
            headers=csrf_headers(client, session["csrf_token"]),
        ).status_code == 409
        assert client.post(
            "/api/runner/heartbeat",
            headers={"Authorization": f"Bearer {runner_credential}"},
            json={
                "readiness": "ready",
                "companion_version": "0.1.0",
                "protocol_version": 1,
            },
        ).status_code == 401