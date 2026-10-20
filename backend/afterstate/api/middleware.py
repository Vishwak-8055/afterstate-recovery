"""Pure-ASGI middleware: correlation ids, security headers, request-size limits and rate limiting."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from afterstate.config import get_settings
from afterstate.logging import correlation_id_var

_RID = re.compile(r"^[A-Za-z0-9._\-]{8,64}$")
_UPLOAD = re.compile(r"^/api/v1/workspaces/[0-9a-f\-]{36}/artifacts$")


def _json_response(
    status: int, code: str, message: str, headers: list[tuple[bytes, bytes]] | None = None
) -> tuple[Message, Message]:
    body = json.dumps({"error": {"code": code, "message": message}}).encode()
    start = {
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            *(headers or []),
        ],
    }
    return start, {"type": "http.response.body", "body": body}


class SecurityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from afterstate.api.deps import limiter

        settings = get_settings()
        headers = {k.decode().lower(): v.decode("latin-1") for k, v in scope["headers"]}
        rid = headers.get("x-request-id", "")
        rid = rid if _RID.match(rid) else uuid.uuid4().hex
        token = correlation_id_var.set(rid)
        path: str = scope["path"]
        method: str = scope["method"]

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                extra: list[tuple[bytes, bytes]] = [
                    (b"x-request-id", rid.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (b"cross-origin-resource-policy", b"same-origin"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                ]
                existing = {k.lower() for k, _ in message.get("headers", [])}
                if b"cache-control" not in existing:
                    extra.append((b"cache-control", b"no-store"))
                if b"content-security-policy" not in existing:
                    extra.append((b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"))
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        try:
            ip = scope["client"][0] if scope.get("client") else "unknown"
            if path.startswith("/api/") and not path.startswith("/api/v1/health"):
                auth_path = path.endswith(("/auth/login", "/auth/register"))
                allowed, retry = limiter.allow(
                    f"{'auth' if auth_path else 'api'}:{ip}",
                    settings.rate_limit_auth_per_minute if auth_path else settings.rate_limit_api_per_minute,
                )
                if not allowed:
                    start, body = _json_response(
                        429,
                        "rate_limited",
                        "Too many requests. Slow down and retry shortly.",
                        [(b"retry-after", str(retry).encode())],
                    )
                    await send_wrapper(start)
                    await send_wrapper(body)
                    return

            if method in ("POST", "PUT", "PATCH") and not _UPLOAD.match(path):
                limit = settings.max_json_body_bytes
                declared = headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > limit:
                    start, body = _json_response(
                        413, "payload_too_large", f"Request bodies are limited to {limit} bytes on this endpoint."
                    )
                    await send_wrapper(start)
                    await send_wrapper(body)
                    return
                seen = 0

                async def limited_receive() -> Message:
                    nonlocal seen
                    message = await receive()
                    if message["type"] == "http.request":
                        seen += len(message.get("body", b""))
                        if seen > limit:
                            raise _TooLarge
                    return message

                try:
                    await self.app(scope, limited_receive, send_wrapper)
                except _TooLarge:
                    start, body = _json_response(
                        413, "payload_too_large", f"Request bodies are limited to {limit} bytes on this endpoint."
                    )
                    await send_wrapper(start)
                    await send_wrapper(body)
                return
            await self.app(scope, receive, send_wrapper)
        finally:
            correlation_id_var.reset(token)


class _TooLarge(Exception):
    pass


def unused(*_: Any) -> None:  # pragma: no cover
    return None
