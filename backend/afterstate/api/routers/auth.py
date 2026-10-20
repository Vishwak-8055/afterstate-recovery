from __future__ import annotations

import re
import secrets
import uuid
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from afterstate.audit import Actor, record
from afterstate.config import get_settings
from afterstate.db.base import utcnow
from afterstate.db.models import AuthSession, Membership, Notification, RecoveryRun, User, WorkerHeartbeat, Workspace
from afterstate.domain.enums import Role, RunState
from afterstate.logging import correlation_id_var
from afterstate.security.passwords import burn_time, hash_password, password_problem, verify_password
from afterstate.security.rbac import Permission
from afterstate.security.sessions import hash_token, new_token
from afterstate.worker.notify import check_webhook_url

from ..deps import (
    COOKIE_CSRF,
    COOKIE_SESSION,
    AuthContext,
    WorkspaceCtx,
    client_ip,
    current_auth,
    get_db,
    get_services,
    limiter,
    require,
)
from ..errors import ApiError
from ..schemas import LoginIn, MemberIn, MemberPatch, RegisterIn, WorkspaceIn, WorkspacePatch
from ..serializers import iso

router = APIRouter(prefix="/api/v1")


def _me(db: Session, user: User, csrf: str | None = None) -> dict[str, Any]:
    rows = db.execute(
        select(Membership, Workspace)
        .join(Workspace, Workspace.id == Membership.workspace_id)
        .where(Membership.user_id == user.id)
        .order_by(Workspace.name)
    ).all()
    return {
        "user": {"id": str(user.id), "email": user.email, "display_name": user.display_name},
        "memberships": [
            {"workspace_id": str(w.id), "name": w.name, "slug": w.slug, "role": m.role.value} for m, w in rows
        ],
        "csrf_token": csrf,
    }


def _start_session(db: Session, user: User, request: Request, response: Response) -> dict[str, Any]:
    settings = get_settings()
    token, csrf = new_token(), new_token()
    now = utcnow()
    db.add(
        AuthSession(
            user_id=user.id,
            token_hash=hash_token(token),
            csrf_token=csrf,
            expires_at=now + timedelta(seconds=settings.session_ttl_seconds),
            ip=client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:300],
        )
    )
    user.last_login_at = now
    ttl = settings.session_ttl_seconds
    response.set_cookie(
        COOKIE_SESSION,
        token,
        max_age=ttl,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        COOKIE_CSRF, csrf, max_age=ttl, httponly=False, secure=settings.session_cookie_secure, samesite="lax", path="/"
    )
    return _me(db, user, csrf)


@router.post("/auth/register", status_code=201)
def register(body: RegisterIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict[str, Any]:
    if not get_settings().allow_registration:
        raise ApiError(403, "registration_disabled", "Self-service registration is disabled on this installation.")
    email = body.email.strip().lower()
    if (problem := password_problem(body.password, email)) is not None:
        raise ApiError(422, "weak_password", problem)
    user = User(email=email, display_name=body.display_name, password_hash=hash_password(body.password))
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError(409, "email_in_use", "An account with this email already exists.") from exc
    actor = Actor(user.id, email, client_ip(request), correlation_id_var.get())
    record(db, actor, "auth.register", workspace_id=None, target_type="user", target_id=user.id)
    result = _start_session(db, user, request, response)
    db.commit()
    return result


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict[str, Any]:
    email = body.email.strip().lower()
    allowed, retry = limiter.allow(f"login:{email}", 8, 300)
    if not allowed:
        raise ApiError(
            429, "rate_limited", f"Too many sign-in attempts for this account. Try again in {retry} seconds."
        )
    user = db.scalar(select(User).where(User.email == email))
    ok = False
    if user is not None and user.is_active:
        ok = verify_password(user.password_hash, body.password)
    else:
        burn_time(body.password)
    actor = Actor(user.id if user else None, email, client_ip(request), correlation_id_var.get())
    if not ok or user is None:
        record(db, actor, "auth.login", workspace_id=None, outcome="failure", details={"reason": "invalid_credentials"})
        db.commit()
        raise ApiError(401, "invalid_credentials", "Email or password is incorrect.")
    record(db, actor, "auth.login", workspace_id=None, target_type="user", target_id=user.id)
    result = _start_session(db, user, request, response)
    db.commit()
    return result


@router.post("/auth/logout", status_code=204)
def logout(response: Response, auth: AuthContext = Depends(current_auth), db: Session = Depends(get_db)) -> Response:
    auth.session.revoked_at = utcnow()
    record(db, auth.actor, "auth.logout", workspace_id=None)
    db.commit()
    response.delete_cookie(COOKIE_SESSION, path="/")
    response.delete_cookie(COOKIE_CSRF, path="/")
    response.status_code = 204
    return response


@router.get("/auth/me")
def me(auth: AuthContext = Depends(current_auth), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _me(db, auth.user, auth.session.csrf_token)


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "workspace"
    return f"{base}-{secrets.token_hex(3)}"


@router.post("/workspaces", status_code=201)
def create_workspace(
    body: WorkspaceIn, auth: AuthContext = Depends(current_auth), db: Session = Depends(get_db)
) -> dict[str, Any]:
    ws = Workspace(
        name=body.name, slug=_slug(body.name), max_concurrent_runs=get_settings().max_concurrent_runs_default
    )
    db.add(ws)
    db.flush()
    db.add(Membership(workspace_id=ws.id, user_id=auth.user.id, role=Role.OWNER))
    record(
        db,
        auth.actor,
        "workspace.create",
        workspace_id=ws.id,
        target_type="workspace",
        target_id=ws.id,
        details={"name": ws.name},
    )
    db.commit()
    return _me(db, auth.user)


def _workspace(ws: Workspace, role: Role) -> dict[str, Any]:
    owner = role is Role.OWNER
    return {
        "id": str(ws.id),
        "name": ws.name,
        "slug": ws.slug,
        "role": role.value,
        "max_concurrent_runs": ws.max_concurrent_runs,
        "retention_days": ws.retention_days,
        "webhook_configured": bool(ws.webhook_url),
        "webhook_url": ws.webhook_url if owner else None,
        "created_at": iso(ws.created_at),
    }


@router.get("/workspaces/{workspace_id}")
def get_workspace(ctx: WorkspaceCtx = Depends(require(Permission.WORKSPACE_READ))) -> dict[str, Any]:
    return _workspace(ctx.workspace, ctx.membership.role)


@router.patch("/workspaces/{workspace_id}")
def patch_workspace(
    body: WorkspacePatch,
    ctx: WorkspaceCtx = Depends(require(Permission.WORKSPACE_MANAGE)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ws, changed = ctx.workspace, {}
    for field in ("name", "max_concurrent_runs", "retention_days"):
        value = getattr(body, field)
        if value is not None:
            setattr(ws, field, value)
            changed[field] = value
    if body.webhook_url is not None or body.clear_webhook:
        if ctx.membership.role is not Role.OWNER:
            record(
                db,
                ctx.actor,
                "access.denied",
                workspace_id=ws.id,
                outcome="denied",
                details={"permission": Permission.SECURITY_MANAGE.value},
            )
            db.commit()
            raise ApiError(403, "forbidden", "Only workspace owners can change the notification webhook.")
        if body.clear_webhook:
            ws.webhook_url = None
        else:
            reason = check_webhook_url(
                body.webhook_url or "", allow_private=get_services().settings.webhook_allow_private_hosts
            )
            if reason:
                raise ApiError(422, "invalid_webhook", reason)
            ws.webhook_url = body.webhook_url
        changed["webhook"] = "cleared" if body.clear_webhook else "set"
    record(
        db, ctx.actor, "workspace.update", workspace_id=ws.id, target_type="workspace", target_id=ws.id, details=changed
    )
    db.commit()
    return _workspace(ws, ctx.membership.role)


def _members(db: Session, ws_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = db.scalars(select(Membership).where(Membership.workspace_id == ws_id).order_by(Membership.created_at)).all()
    return [
        {
            "user_id": str(m.user_id),
            "email": m.user.email,
            "display_name": m.user.display_name,
            "role": m.role.value,
            "joined_at": iso(m.created_at),
        }
        for m in rows
    ]


@router.get("/workspaces/{workspace_id}/members")
def list_members(
    ctx: WorkspaceCtx = Depends(require(Permission.WORKSPACE_READ)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    return {"items": _members(db, ctx.id)}


@router.post("/workspaces/{workspace_id}/members", status_code=201)
def add_member(
    body: MemberIn, ctx: WorkspaceCtx = Depends(require(Permission.MEMBERS_MANAGE)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if user is None:
        raise ApiError(404, "user_not_found", "No account exists for that email. Ask them to register first.")
    if db.scalar(select(Membership.id).where(Membership.workspace_id == ctx.id, Membership.user_id == user.id)):
        raise ApiError(409, "already_member", "That person is already a member of this workspace.")
    db.add(Membership(workspace_id=ctx.id, user_id=user.id, role=Role(body.role)))
    record(
        db,
        ctx.actor,
        "member.add",
        workspace_id=ctx.id,
        target_type="user",
        target_id=user.id,
        details={"email": user.email, "role": body.role},
    )
    db.commit()
    return {"items": _members(db, ctx.id)}


def _owner_count(db: Session, ws_id: uuid.UUID) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(Membership)
            .where(Membership.workspace_id == ws_id, Membership.role == Role.OWNER)
        )
        or 0
    )


@router.patch("/workspaces/{workspace_id}/members/{user_id}")
def change_role(
    user_id: uuid.UUID,
    body: MemberPatch,
    ctx: WorkspaceCtx = Depends(require(Permission.MEMBERS_MANAGE)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    m = db.scalar(select(Membership).where(Membership.workspace_id == ctx.id, Membership.user_id == user_id))
    if m is None:
        raise ApiError(404, "not_found", "Member not found.")
    if m.role is Role.OWNER and body.role != "owner" and _owner_count(db, ctx.id) <= 1:
        raise ApiError(409, "last_owner", "A workspace must keep at least one owner.")
    previous, m.role = m.role, Role(body.role)
    record(
        db,
        ctx.actor,
        "member.role_change",
        workspace_id=ctx.id,
        target_type="user",
        target_id=user_id,
        details={"from": previous.value, "to": body.role},
    )
    db.commit()
    return {"items": _members(db, ctx.id)}


@router.delete("/workspaces/{workspace_id}/members/{user_id}")
def remove_member(
    user_id: uuid.UUID, ctx: WorkspaceCtx = Depends(require(Permission.MEMBERS_MANAGE)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    m = db.scalar(select(Membership).where(Membership.workspace_id == ctx.id, Membership.user_id == user_id))
    if m is None:
        raise ApiError(404, "not_found", "Member not found.")
    if m.role is Role.OWNER and _owner_count(db, ctx.id) <= 1:
        raise ApiError(409, "last_owner", "A workspace must keep at least one owner.")
    db.delete(m)
    record(db, ctx.actor, "member.remove", workspace_id=ctx.id, target_type="user", target_id=user_id)
    db.commit()
    return {"items": _members(db, ctx.id)}


@router.get("/workspaces/{workspace_id}/notifications")
def notifications(
    limit: int = 30, ctx: WorkspaceCtx = Depends(require(Permission.WORKSPACE_READ)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    rows = db.scalars(
        select(Notification)
        .where(Notification.workspace_id == ctx.id, Notification.channel == "in_app")
        .order_by(Notification.created_at.desc())
        .limit(min(max(limit, 1), 100))
    ).all()
    return {
        "items": [
            {
                "id": str(n.id),
                "kind": n.kind,
                "title": n.title,
                "body": n.body,
                "run_id": str(n.run_id) if n.run_id else None,
                "created_at": iso(n.created_at),
            }
            for n in rows
        ]
    }


@router.get("/workspaces/{workspace_id}/system")
def system(
    ctx: WorkspaceCtx = Depends(require(Permission.WORKSPACE_READ)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    svc = get_services()
    now = utcnow()
    workers = db.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_seen_at.desc()).limit(20)).all()
    counts = dict(
        db.execute(
            select(RecoveryRun.state, func.count())
            .where(RecoveryRun.workspace_id == ctx.id)
            .group_by(RecoveryRun.state)
        ).all()
    )
    return {
        "workers": [
            {
                "id": w.worker_id,
                "hostname": w.hostname,
                "version": w.version,
                "last_seen_at": iso(w.last_seen_at),
                "age_seconds": int((now - w.last_seen_at).total_seconds()),
                "alive": (now - w.last_seen_at) < timedelta(seconds=30),
                "current_run_id": str(w.current_run_id) if w.current_run_id else None,
            }
            for w in workers
        ],
        "queue": {
            s.value: int(counts.get(s, 0))
            for s in (
                RunState.QUEUED,
                RunState.PREPARING,
                RunState.RESTORING,
                RunState.VALIDATING,
                RunState.FINALIZING,
                RunState.NEEDS_CLEANUP,
            )
        },
        "limits": {
            "max_artifact_bytes": svc.settings.max_artifact_bytes,
            "max_environment_bytes": svc.settings.max_environment_bytes,
            "max_json_body_bytes": svc.settings.max_json_body_bytes,
        },
        "postgresql": svc.tools.version,
        "lab_enabled": svc.settings.lab_enabled,
        "registration_enabled": svc.settings.allow_registration,
    }
