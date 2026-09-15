from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ApiConfig:
    database_path: Path
    public_origin: str
    cookie_secure: bool
    portal_dist: Path | None = None
    artifact_root: Path | None = None
    pairing_ttl_seconds: int = 300
    portal_session_ttl_seconds: int = 7 * 24 * 60 * 60
    runner_credential_ttl_seconds: int = 7 * 24 * 60 * 60
    capture_capability_ttl_seconds: int = 60 * 60
    readiness_freshness_seconds: int = 10

    def __post_init__(self) -> None:
        parsed = urlsplit(self.public_origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("public_origin must be an absolute HTTP(S) origin")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("public_origin cannot include a path, query, or fragment")
        if self.cookie_secure and parsed.scheme != "https":
            raise ValueError("secure cookies require an HTTPS public origin")

    @classmethod
    def from_environment(cls) -> ApiConfig:
        project_root = Path(__file__).resolve().parents[4]
        public_origin = (
            os.environ.get("WORKFLOW_PUBLIC_ORIGIN")
            or os.environ.get("RENDER_EXTERNAL_URL")
            or "http://127.0.0.1:8000"
        )
        return cls(
            database_path=Path(
                os.environ.get(
                    "WORKFLOW_API_DATABASE",
                    project_root / ".local" / "api" / "workflow.sqlite3",
                )
            ),
            public_origin=public_origin.rstrip("/"),
            cookie_secure=public_origin.startswith("https://"),
            portal_dist=project_root / "apps" / "portal" / "dist",
            artifact_root=Path(
                os.environ.get(
                    "WORKFLOW_API_ARTIFACTS",
                    project_root / ".local" / "api" / "artifacts",
                )
            ),
        )