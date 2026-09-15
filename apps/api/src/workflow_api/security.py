from __future__ import annotations

import hashlib
import re
import secrets

DIGEST_PATTERN = re.compile(r"^[a-f0-9]{64}$")


def digest_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def new_secret(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def new_public_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token:
        return None
    return token