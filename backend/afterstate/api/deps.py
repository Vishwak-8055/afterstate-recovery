from __future__ import annotations

import hmac
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from urllib.parse import urlsplit

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from afterstate.audit import Actor, record
from afterstate.config import get_settings
from afterstate.db.base import utcnow
from afterstate.db.models import AuthSession, Membership, User, Workspace
from afterstate.db.session import get_sessionmaker
from afterstate.engine.pipeline import Services
from afterstate.logging import correlation_id_var, workspace_id_var
from afterstate.security.ratelimit import RateLimiter
from afterstate.security.rbac import Permission, role_allows
from afterstate.security.sessions import hash_token
from afterstate.services import build_services

from .errors import ApiError

COOKIE_SESSION = "afterstate_session"
COOKIE_CSRF = "afterstate_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
limiter = RateLimiter()


@lru_cache
def get_services() -> Services:
    return build_services()


def reset_services_cache() -> None:
    get_services.cache_clear()
    limiter.reset()


def get_db() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@dataclass
class AuthContext:
    user: User
    session: AuthSession
    ip: str

    @property
    def actor(self) -> Actor:
        return Actor(self.user.id, self.user.email, self.ip, correlation_id_var.get())


@dataclass
class WorkspaceCtx:
    auth: AuthContext
    workspace: Workspace
    membership: Membership

    @property
    def id(self) -> uuid.UUID:
        return self.workspace.id

    @property
    def actor(self) -> Actor:
        return self.auth.actor


def _check_csrf(request: Request, row: AuthSession) -> None:
    sent = request.headers.get("x-csrf-token", "")
    if not sent or not hmac.compare_digest(sent, row.csrf_token):
        raise ApiError(403, "csrf_failed", "Missing or invalid CSRF token.")
    origin = request.headers.get("origin")
    if origin:
        allowed = {o.rstrip("/") for o in get_settings().allowed_origins}
        host = urlsplit(origin).netloc
        if origin.rstrip("/") not in allowed and host != request.headers.get("host", ""):
            raise ApiError(403, "origin_not_allowed", "Cross-origin request refused.")


def current_auth(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    token = request.cookies.get(COOKIE_SESSION)
    if not token:
        raise ApiError(401, "unauthenticated", "Sign in to continue.")
    row = db.scalar(select(AuthSession).where(AuthSession.token_hash == hash_token(token)))
    now = utcnow()
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        raise ApiError(401, "unauthenticated", "Your session has expired. Sign in again.")
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise ApiError(401, "unauthenticated", "This account is not active.")
    if request.method not in SAFE_METHODS:
        _check_csrf(request, row)
    if now - row.last_seen_at > timedelta(seconds=60):
        row.last_seen_at = now
        db.commit()
    return AuthContext(user, row, client_ip(request))


def require(permission: Permission) -> Callable[..., WorkspaceCtx]:
    """Dependency factory: membership + permission are enforced on the server for every request.
    Non-members get 404 (workspace existence is not disclosed); members lacking the permission get an
    audited 403."""

    def dependency(
        workspace_id: uuid.UUID, auth: AuthContext = Depends(current_auth), db: Session = Depends(get_db)
    ) -> WorkspaceCtx:
        membership = db.scalar(
            select(Membership).where(Membership.workspace_id == workspace_id, Membership.user_id == auth.user.id)
        )
        if membership is None:
            raise ApiError(404, "not_found", "Workspace not found.")
        if not role_allows(membership.role, permission):
            record(
                db,
                auth.actor,
                "access.denied",
                workspace_id=workspace_id,
                outcome="denied",
                details={"permission": permission.value, "role": membership.role.value},
            )
            db.commit()
            raise ApiError(
                403,
                "forbidden",
                f"Your role ({membership.role.value}) does not allow this action ({permission.value}).",
            )
        workspace = db.get(Workspace, workspace_id)
        assert workspace is not None
        workspace_id_var.set(str(workspace_id))
        return WorkspaceCtx(auth, workspace, membership)

    return dependency
