from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from workflow_api import ApiConfig, create_app
from workflow_api.security import digest_secret
from workflow_api.workflows import (
    build_run_snapshot,
    build_workflow_definition,
    verification_profile,
)
from workflow_companion import CompanionClient, CompanionStore
from workflow_companion.service import reconcile_unfinished
from workflow_protocol import (
    CaptureProvenance,
    PhaseRunEvent,
    VERIFIED_PRESET_CATEGORY,
    VERIFIED_PRESET_CATEGORY_ID,
)


def test_general_restart_interrupts_without_business_outcome() -> None:
    async def scenario() -> None:
        fixture = Path(__file__).parents[3] / "packages" / "protocol" / "fixtures" / "workflow_base.json"
        actions = json.loads(fixture.read_text(encoding="utf-8"))["actions"]
        workflow = build_workflow_definition(
            workflow_id="wf_general_restart01",
            created_at=4_000,
            name="General restart fixture",
            description="",
            capture=CaptureProvenance.model_validate(
                {
                    "extension_version": "0.1.0",
                    "captured_at": "2026-09-14T12:00:00Z",
                    "document_count": 1,
                    "event_count": len(actions),
                    "completeness": "complete",
                }
            ),
            actions=actions,
            category_id="cat_ticket_intake",
            category="Ticket intake",
            category_verification_profile=None,
        )
        snapshot = build_run_snapshot(
            run_id="run_generalrestart01",
            workflow=workflow,
            requested_at=4_000,
            expires_at=4_060,
        )
        events = []

        class Client:
            async def record_event(self, run_id: str, event: object) -> None:
                assert run_id == snapshot.run_id
                events.append(event)

        store = SimpleNamespace(
            unfinished_runs=lambda: [SimpleNamespace(run_id=snapshot.run_id, snapshot=snapshot)]
        )
        await reconcile_unfinished(Client(), store)  # type: ignore[arg-type]

        assert [event.kind for event in events] == ["phase"]  # type: ignore[attr-defined]
        assert events[0].phase == "interrupted"  # type: ignore[attr-defined]

    asyncio.run(scenario())


def test_claim_is_durable_before_acceptance_event_is_synchronized(tmp_path: Path) -> None:
    async def scenario() -> None:
        api_database = tmp_path / "api.sqlite3"
        app = create_app(ApiConfig(api_database, "http://testserver", False))
        app.state.database.initialize()
        transport = httpx.ASGITransport(app=app)
        store = CompanionStore(tmp_path / "companion.sqlite3")
        companion = CompanionClient("http://testserver", store, transport=transport)

        launch = await companion.initiate_pairing("Run handoff", "0.1.0")
        ticket = parse_qs(urlsplit(launch.pairing_url).fragment)["ticket"][0]
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as portal:
            session = (await portal.get("/api/session")).json()
            portal_headers = {
                "Origin": "http://testserver",
                "X-CSRF-Token": session["csrf_token"],
            }
            assert (await portal.post(
                "/api/pairings/connect",
                headers=portal_headers,
                json={"ticket": ticket},
            )).status_code == 200
            assert await companion.refresh_pairing() == "connected"
            await companion.heartbeat("ready", "0.1.0")

            fixture = Path(__file__).parents[3] / "packages" / "protocol" / "fixtures" / "workflow_base.json"
            actions = json.loads(fixture.read_text(encoding="utf-8"))["actions"]
            workflow = build_workflow_definition(
                workflow_id="wf_handoff0000001",
                created_at=4_000,
                name="Handoff fixture",
                description="",
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
                        4_000,
                        workflow.workflow_mode,
                        workflow.category,
                        workflow.category_id,
                    ),
                )
            created = await portal.post(
                f"/api/workflows/{workflow.workflow_id}/runs",
                headers=portal_headers,
                json={"client_request_key": "request_handoff000001"},
            )
            assert created.status_code == 201
            run_id = created.json()["run"]["run_id"]

        snapshot = await companion.claim_run()
        assert snapshot is not None and snapshot.run_id == run_id
        local = store.run(run_id)
        assert local is not None
        assert local.phase == "accepted"
        assert local.snapshot.workflow.content_hash == workflow.content_hash
        assert store.pending_claim() is None
        assert store.pending_events(run_id) == []

        await companion.record_event(
            run_id,
            PhaseRunEvent(
                kind="phase",
                phase="preparing",
                observed_at=datetime.now(timezone.utc),
            ),
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as reader:
            detail = (await reader.get(f"/api/runs/{run_id}")).json()
        assert detail["run"]["phase"] == "preparing"
        assert [item["sequence"] for item in detail["events"]] == [0, 1]

        await reconcile_unfinished(companion, store)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as reader:
            interrupted = (await reader.get(f"/api/runs/{run_id}")).json()
        assert interrupted["run"]["phase"] == "interrupted"
        assert interrupted["run"]["outcome"] == "uncertain"
        assert "not resumed" in interrupted["run"]["outcome_summary"]
        assert [item["event"]["kind"] for item in interrupted["events"][-2:]] == [
            "outcome",
            "phase",
        ]

        with sqlite3.connect(api_database) as connection:
            central_values = "\n".join(
                str(row)
                for row in connection.execute(
                    "SELECT reporting_capability_digest, snapshot_json FROM workflow_run"
                )
            )
        assert local.reporting_capability not in central_values
        assert digest_secret(local.reporting_capability) in central_values

    asyncio.run(scenario())