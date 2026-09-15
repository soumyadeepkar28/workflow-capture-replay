from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from workflow_api import ApiConfig, create_app
from workflow_companion import CompanionClient, CompanionStore


def test_companion_and_portal_complete_pairing_without_sharing_runner_secret(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        api_database = tmp_path / "api.sqlite3"
        app = create_app(
            ApiConfig(
                database_path=api_database,
                public_origin="http://testserver",
                cookie_secure=False,
            )
        )
        app.state.database.initialize()
        transport = httpx.ASGITransport(app=app)
        store = CompanionStore(tmp_path / "companion.sqlite3")
        companion = CompanionClient("http://testserver", store, transport=transport)

        launch = await companion.initiate_pairing("Local runner", "0.1.0")
        pending = store.pending()
        assert pending is not None
        assert await companion.refresh_pairing() == "pending"

        ticket = parse_qs(urlsplit(launch.pairing_url).fragment)["ticket"][0]
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as portal:
            session = (await portal.get("/api/session")).json()
            headers = {
                "Origin": "http://testserver",
                "X-CSRF-Token": session["csrf_token"],
            }
            preview = await portal.post(
                "/api/pairings/preview",
                headers=headers,
                json={"ticket": ticket},
            )
            assert preview.status_code == 200
            connected = await portal.post(
                "/api/pairings/connect",
                headers=headers,
                json={"ticket": ticket},
            )
            assert connected.status_code == 200

        assert await companion.refresh_pairing() == "connected"
        assert store.pending() is None
        association = store.association()
        assert association is not None
        await companion.heartbeat("ready", "0.1.0")

        with sqlite3.connect(api_database) as connection:
            serialized_rows = "\n".join(
                str(row)
                for table in ("pairing", "runner")
                for row in connection.execute(f"SELECT * FROM {table}").fetchall()
            )
        assert pending.pending_proof not in serialized_rows
        assert pending.runner_credential not in serialized_rows

    asyncio.run(scenario())