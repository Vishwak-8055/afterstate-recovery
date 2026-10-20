"""Webhook delivery for failure notifications, with SSRF protection.

Webhook URLs are configured by workspace admins, so the destination must not be able to reach
internal services: only http(s), no redirects, and resolved addresses must be public unless the
operator explicitly opts in (AFTERSTATE_WEBHOOK_ALLOW_PRIVATE_HOSTS, development only)."""

from __future__ import annotations

import ipaddress
import logging
import socket
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from afterstate.db.base import utcnow
from afterstate.db.models import Notification, Workspace
from afterstate.engine.pipeline import Services

log = logging.getLogger("afterstate.notify")
MAX_ATTEMPTS = 5


def check_webhook_url(url: str, *, allow_private: bool) -> str | None:
    """Return a reason the URL is unacceptable, or None."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "webhook URL must be http(s) with a host"
    if parts.username or parts.password:
        return "credentials in the URL are not allowed"
    try:
        infos = socket.getaddrinfo(
            parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM
        )
    except OSError:
        return "webhook host does not resolve"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not allow_private and (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return f"webhook host resolves to a non-public address ({ip})"
    return None


def dispatch_pending(svc: Services, limit: int = 20) -> int:
    delivered = 0
    now = utcnow()
    with svc.sf() as s:
        ids = list(
            s.scalars(
                select(Notification.id)
                .where(
                    Notification.channel == "webhook",
                    Notification.status == "pending",
                    Notification.next_attempt_at <= now,
                )
                .order_by(Notification.created_at)
                .limit(limit)
            )
        )
    for nid in ids:
        with svc.sf() as s, s.begin():
            n = s.execute(
                select(Notification).where(Notification.id == nid).with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if n is None or n.status != "pending":
                continue
            ws = s.get(Workspace, n.workspace_id)
            url = ws.webhook_url if ws is not None else None
            error: str | None = None
            if not url:
                error = "no webhook configured"
            else:
                error = check_webhook_url(url, allow_private=svc.settings.webhook_allow_private_hosts)
            if error is None and url:
                try:
                    resp = httpx.post(
                        url,
                        json=n.payload,
                        timeout=5.0,
                        follow_redirects=False,
                        headers={"User-Agent": "afterstate-webhook/1"},
                    )
                    if not 200 <= resp.status_code < 300:
                        error = f"receiver answered HTTP {resp.status_code}"
                except httpx.HTTPError as exc:
                    error = f"delivery error: {type(exc).__name__}"
            n.attempts += 1
            if error is None:
                n.status, n.delivered_at, n.last_error = "delivered", utcnow(), None
                delivered += 1
            else:
                n.last_error = error[:300]
                if n.attempts >= MAX_ATTEMPTS:
                    n.status = "failed"
                else:
                    n.next_attempt_at = utcnow() + timedelta(seconds=15 * 2**n.attempts)
    return delivered
