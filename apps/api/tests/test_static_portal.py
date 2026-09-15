from pathlib import Path

from fastapi.testclient import TestClient

from workflow_api import ApiConfig, create_app


def test_built_portal_supports_spa_routes_without_masking_unknown_api(tmp_path: Path) -> None:
    portal = tmp_path / "dist"
    portal.mkdir()
    (portal / "index.html").write_text("<h1>Workflow Replay</h1>", encoding="utf-8")
    (portal / "asset.txt").write_text("asset", encoding="utf-8")
    app = create_app(
        ApiConfig(
            database_path=tmp_path / "api.sqlite3",
            public_origin="http://testserver",
            cookie_secure=False,
            portal_dist=portal,
        )
    )

    with TestClient(app, base_url="http://testserver") as client:
        assert "Workflow Replay" in client.get("/pair").text
        assert client.get("/asset.txt").text == "asset"
        assert client.get("/api/does-not-exist").status_code == 404