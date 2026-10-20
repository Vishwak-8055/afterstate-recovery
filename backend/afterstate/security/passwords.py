from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()  # argon2id with the library's current recommended parameters
_DUMMY_HASH = _hasher.hash("afterstate-timing-equaliser")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def burn_time(password: str) -> None:
    """Spend the same time as a real verification so unknown accounts are not distinguishable by latency."""
    verify_password(_DUMMY_HASH, password)


def password_problem(password: str, email: str) -> str | None:
    if len(password) < 12:
        return "Use at least 12 characters."
    if len(password) > 128:
        return "Use at most 128 characters."
    local = email.split("@", maxsplit=1)[0].lower()
    if password.lower() in (email.lower(), local) or len(set(password)) < 4:
        return "Choose a password that is not your email address and has some variety."
    return None
