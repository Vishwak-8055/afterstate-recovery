from __future__ import annotations

import hashlib
import secrets


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Only the hash is stored, so a database leak does not yield usable session cookies."""
    return hashlib.sha256(token.encode()).hexdigest()
