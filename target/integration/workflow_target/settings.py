from __future__ import annotations

import os
from pathlib import Path

from demodesk.config.settings import *  # noqa: F403

INTEGRATION_ROOT = Path(__file__).resolve().parents[1]
TARGET_DATA_ROOT = Path(os.environ["WORKFLOW_TARGET_DATA_ROOT"]).resolve()

SECRET_KEY = Path(os.environ["WORKFLOW_TARGET_SECRET_KEY_FILE"]).read_text(
    encoding="utf-8"
).strip()
DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1"]
ROOT_URLCONF = "workflow_target.urls"

DATABASES = {  # noqa: F405
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ["WORKFLOW_TARGET_DATABASE"],
    }
}
MEDIA_ROOT = str(TARGET_DATA_ROOT / "media")
STATIC_ROOT = str(TARGET_DATA_ROOT / "static")

TEMPLATES[0]["DIRS"] = [  # type: ignore[name-defined]  # noqa: F405
    str(INTEGRATION_ROOT / "templates"),
    *TEMPLATES[0]["DIRS"],  # type: ignore[name-defined]  # noqa: F405
]

HELPDESK_PUBLIC_ENABLED = False
HELPDESK_VIEW_A_TICKET_PUBLIC = False
HELPDESK_SUBMIT_A_TICKET_PUBLIC = False
HELPDESK_KB_ENABLED = True
HELPDESK_KANBAN_ENABLED = False
HELPDESK_REDIRECT_TO_LOGIN_BY_DEFAULT = True

SESSION_COOKIE_NAME = "workflow_helpdesk_sessionid"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_NAME = "workflow_helpdesk_csrftoken"
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"

WORKFLOW_TARGET_CONTROL_DATABASE = os.environ["WORKFLOW_TARGET_CONTROL_DATABASE"]
WORKFLOW_TARGET_EXPECTED_HOST = os.environ["WORKFLOW_TARGET_EXPECTED_HOST"]
WORKFLOW_TARGET_ORIGIN = os.environ["WORKFLOW_TARGET_ORIGIN"]