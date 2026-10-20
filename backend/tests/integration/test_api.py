"""API behaviour through real HTTP semantics: auth, CSRF, tenancy, RBAC, uploads, and the full workflow."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from afterstate.db.models import RunEvidence
from afterstate.lab.fixtures import shop_rules
from afterstate.worker.runner import Worker
from fastapi.testclient import TestClient
from sqlalchemy import select, update

pytestmark = pytest.mark.integration
PASSWORD = "correct horse battery staple"


class Client:
    def __init__(self, app: Any) -> None:
        self.http = TestClient(app, raise_server_exceptions=False)
        self.csrf: str | None = None

    def _h(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {**({"X-CSRF-Token": self.csrf} if self.csrf else {}), **(extra or {})}

    def register(self, email: str, name: str = "Test User") -> Any:
        r = self.http.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD, "display_name": name})
        if r.status_code == 201:
            self.csrf = r.json()["csrf_token"]
        return r

    def login(self, email: str, password: str = PASSWORD) -> Any:
        r = self.http.post("/api/v1/auth/login", json={"email": email, "password": password})
        if r.status_code == 200:
            self.csrf = r.json()["csrf_token"]
        return r

    def get(self, path: str, **kw: Any) -> Any:
        return self.http.get(path, **kw)

    def post(self, path: str, json: Any = None, headers: dict[str, str] | None = None, **kw: Any) -> Any:
        return self.http.post(path, json=json, headers=self._h(headers), **kw)

    def put(self, path: str, json: Any = None) -> Any:
        return self.http.put(path, json=json, headers=self._h())

    def patch(self, path: str, json: Any = None) -> Any:
        return self.http.patch(path, json=json, headers=self._h())

    def delete(self, path: str) -> Any:
        return self.http.delete(path, headers=self._h())

    def upload(self, ws: str, data: bytes, **params: Any) -> Any:
        params.setdefault("name", "orders-nightly")
        return self.http.post(
            f"/api/v1/workspaces/{ws}/artifacts",
            params=params,
            content=data,
            headers=self._h({"Content-Type": "application/octet-stream"}),
        )


@pytest.fixture
def make_client(svc, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AFTERSTATE_RATE_LIMIT_AUTH_PER_MINUTE", "1000")
    monkeypatch.setenv("AFTERSTATE_RATE_LIMIT_API_PER_MINUTE", "100000")
    from afterstate.api.deps import reset_services_cache
    from afterstate.config import reset_settings_cache

    reset_settings_cache()
    reset_services_cache()
    from afterstate.api.app import create_app

    app = create_app()
    return lambda: Client(app)


@pytest.fixture
def owner(make_client):  # type: ignore[no-untyped-def]
    c = make_client()
    assert c.register("owner@example.test", "Olivia Owner").status_code == 201
    ws = c.post("/api/v1/workspaces", {"name": "Platform Reliability"}).json()["memberships"][0]["workspace_id"]
    return c, ws


def good_dump(fixtures) -> bytes:  # type: ignore[no-untyped-def]
    return fixtures.path("good").read_bytes()


# ----------------------------------------------------------------------------- auth -------------
def test_registration_login_session_and_logout(make_client):  # type: ignore[no-untyped-def]
    c = make_client()
    r = c.register("a@example.test")
    assert r.status_code == 201 and r.json()["user"]["email"] == "a@example.test"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert c.get("/api/v1/auth/me").status_code == 200
    assert c.post("/api/v1/auth/logout").status_code == 204
    assert c.get("/api/v1/auth/me").status_code == 401  # the server-side session is revoked, not just the cookie
    assert c.login("a@example.test").status_code == 200
    assert make_client().register("a@example.test").status_code == 409


def test_bad_credentials_and_weak_passwords_are_refused(make_client):  # type: ignore[no-untyped-def]
    c = make_client()
    assert (
        c.http.post(
            "/api/v1/auth/register", json={"email": "b@example.test", "password": "short", "display_name": "B"}
        ).status_code
        == 422
    )
    c.register("b@example.test")
    r = make_client().login("b@example.test", "wrong password here")
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"
    r2 = make_client().login("nobody@example.test", "wrong password here")
    assert (
        r2.status_code == 401 and r2.json()["error"]["message"] == r.json()["error"]["message"]
    )  # no account enumeration


def test_mutations_require_a_valid_csrf_token(owner):  # type: ignore[no-untyped-def]
    c, ws = owner
    good = c.csrf
    c.csrf = None
    r = c.post(f"/api/v1/workspaces/{ws}/projects", {"name": "x"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    c.csrf = "0" * 43
    assert c.post(f"/api/v1/workspaces/{ws}/projects", {"name": "x"}).status_code == 403
    c.csrf = good
    assert c.post(f"/api/v1/workspaces/{ws}/projects", {"name": "x"}).status_code == 201
    cross = c.http.post(
        f"/api/v1/workspaces/{ws}/projects",
        json={"name": "y"},
        headers={"X-CSRF-Token": good, "Origin": "https://evil.example"},
    )
    assert cross.status_code == 403 and cross.json()["error"]["code"] == "origin_not_allowed"


def test_request_validation_errors_use_the_error_envelope_and_do_not_echo_input(owner):  # type: ignore[no-untyped-def]
    c, ws = owner
    r = c.http.post("/api/v1/auth/register", json={"email": "not-an-email", "password": "hunter2hunter2hunter2"})
    body = r.json()
    assert r.status_code == 422 and body["error"]["code"] == "validation_error"
    assert {d["field"] for d in body["error"]["details"]} >= {"email", "display_name"}
    assert "hunter2hunter2hunter2" not in r.text  # submitted values are never echoed back
    bad_id = c.get("/api/v1/workspaces/not-a-uuid/overview")
    assert bad_id.status_code == 422 and bad_id.json()["error"]["code"] == "validation_error"
    unknown = c.get("/api/v1/does-not-exist")
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "not_found"
    assert c.get(f"/api/v1/workspaces/{ws}/runs", params={"page": 0}).status_code == 422


def test_unauthenticated_requests_are_rejected(make_client):  # type: ignore[no-untyped-def]
    c = make_client()
    for path in ("/api/v1/auth/me", "/api/v1/workspaces/00000000-0000-0000-0000-000000000000/overview"):
        assert c.get(path).status_code == 401


def test_login_attempts_are_rate_limited_per_account(make_client):  # type: ignore[no-untyped-def]
    c = make_client()
    c.register("rl@example.test")
    codes = [make_client().login("rl@example.test", "definitely wrong pw").status_code for _ in range(10)]
    assert codes[:8] == [401] * 8 and codes[8:] == [429, 429]


def test_oversized_json_bodies_are_rejected(svc, monkeypatch, make_client):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AFTERSTATE_MAX_JSON_BODY_BYTES", "2048")
    from afterstate.api.deps import reset_services_cache
    from afterstate.config import reset_settings_cache

    reset_settings_cache()
    reset_services_cache()
    c = make_client()
    r = c.http.post(
        "/api/v1/auth/login",
        content=json.dumps({"email": "a@example.test", "password": "x" * 5000}),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"


# ------------------------------------------------------------------- tenancy and RBAC -----------
def test_workspaces_are_isolated_even_when_ids_are_guessed(owner, make_client, fixtures):  # type: ignore[no-untyped-def]
    a, ws_a = owner
    art = a.upload(ws_a, good_dump(fixtures)).json()
    proj = a.post(f"/api/v1/workspaces/{ws_a}/projects", {"name": "secret-project", "artifact_id": art["id"]}).json()

    b = make_client()
    b.register("intruder@example.test")
    ws_b = b.post("/api/v1/workspaces", {"name": "Other Org"}).json()["memberships"][0]["workspace_id"]

    assert b.get(f"/api/v1/workspaces/{ws_a}/overview").status_code == 404  # not even a hint that it exists
    # the intruder's own workspace path with the victim's resource ids must not resolve either
    assert b.get(f"/api/v1/workspaces/{ws_b}/artifacts/{art['id']}").status_code == 404
    assert b.get(f"/api/v1/workspaces/{ws_b}/projects/{proj['id']}").status_code == 404
    assert b.post(f"/api/v1/workspaces/{ws_b}/projects/{proj['id']}/runs", {}).status_code == 404
    assert b.delete(f"/api/v1/workspaces/{ws_b}/artifacts/{art['id']}").status_code == 404
    # and a project in B cannot be pointed at A's artifact
    r = b.post(f"/api/v1/workspaces/{ws_b}/projects", {"name": "steal", "artifact_id": art["id"]})
    assert r.status_code == 404
    assert b.get(f"/api/v1/workspaces/{ws_b}/artifacts").json()["total"] == 0


def test_roles_are_enforced_on_the_server_and_denials_are_audited(owner, make_client, fixtures):  # type: ignore[no-untyped-def]
    o, ws = owner
    art = o.upload(ws, good_dump(fixtures)).json()
    project = o.post(
        f"/api/v1/workspaces/{ws}/projects",
        {"name": "p", "artifact_id": art["id"], "rules": [{"kind": "table", "table": "orders"}]},
    ).json()
    clients = {}
    for role in ("viewer", "operator", "admin"):
        cl = make_client()
        cl.register(f"{role}@example.test")
        assert (
            o.post(f"/api/v1/workspaces/{ws}/members", {"email": f"{role}@example.test", "role": role}).status_code
            == 201
        )
        clients[role] = cl
    base = f"/api/v1/workspaces/{ws}"
    v, op, ad = clients["viewer"], clients["operator"], clients["admin"]

    assert v.get(f"{base}/overview").status_code == 200 and v.get(f"{base}/runs").status_code == 200
    assert v.post(f"{base}/projects/{project['id']}/runs", {}).status_code == 403
    assert v.get(f"{base}/audit").status_code == 403
    assert v.upload(ws, b"x").status_code == 403
    assert op.upload(ws, b"x").status_code == 403  # operators run tests; only admins register backups
    assert op.post(f"{base}/projects", {"name": "nope"}).status_code == 403
    assert ad.post(f"{base}/projects", {"name": "admin-made"}).status_code == 201
    assert (
        ad.post(f"{base}/members", {"email": "viewer@example.test", "role": "owner"}).status_code == 403
    )  # only owners manage members
    assert ad.patch(f"{base}", {"webhook_url": "http://example.test/hook"}).status_code == 403
    r = op.post(f"{base}/projects/{project['id']}/runs", {})
    assert r.status_code == 202

    denied = o.get(f"{base}/audit", params={"outcome": "denied"}).json()
    actors = {e["actor"] for e in denied["items"]}
    assert {"viewer@example.test", "operator@example.test", "admin@example.test"} <= actors
    assert all(e["action"] == "access.denied" for e in denied["items"])


def test_the_last_owner_cannot_be_removed_or_demoted(owner):  # type: ignore[no-untyped-def]
    o, ws = owner
    me = o.get("/api/v1/auth/me").json()["user"]["id"]
    assert o.patch(f"/api/v1/workspaces/{ws}/members/{me}", {"role": "viewer"}).status_code == 409
    assert o.delete(f"/api/v1/workspaces/{ws}/members/{me}").status_code == 409


# -------------------------------------------------------------------------- artifacts ------------
def test_upload_verifies_and_registers_a_real_backup(owner, fixtures):  # type: ignore[no-untyped-def]
    c, ws = owner
    data = good_dump(fixtures)
    r = c.upload(
        ws,
        data,
        filename="../../etc/passwd",
        expected_sha256=hashlib.sha256(data).hexdigest(),
        backup_created_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
    )
    assert r.status_code == 201, r.text
    art = r.json()
    assert (
        art["status"] == "verified" and art["format"] == "custom" and art["sha256"] == hashlib.sha256(data).hexdigest()
    )
    assert art["original_filename"] == "passwd"  # path components never survive
    assert (
        all(ch["outcome"] == "PASS" for ch in art["verification"]["checks"]) and len(art["verification"]["checks"]) == 6
    )
    assert art["dump_server_version"].startswith("16") and art["toc_entries"] > 20
    detail = c.get(f"/api/v1/workspaces/{ws}/artifacts/{art['id']}").json()
    assert detail["verifications"][0]["kind"] == "upload"
    assert c.post(f"/api/v1/workspaces/{ws}/artifacts/{art['id']}/verify").json()["status"] == "verified"


def test_upload_rejects_wrong_checksum_empty_bodies_and_wrong_content_type(owner, fixtures):  # type: ignore[no-untyped-def]
    c, ws = owner
    r = c.upload(ws, good_dump(fixtures), expected_sha256="0" * 64)
    assert r.status_code == 422 and r.json()["error"]["code"] == "checksum_mismatch"
    assert c.get(f"/api/v1/workspaces/{ws}/artifacts").json()["total"] == 0  # nothing was stored
    assert c.upload(ws, b"").status_code == 422
    r = c.http.post(
        f"/api/v1/workspaces/{ws}/artifacts",
        params={"name": "x"},
        content=b"abc",
        headers={"X-CSRF-Token": c.csrf or "", "Content-Type": "application/json"},
    )
    assert r.status_code == 415


def test_upload_size_limit_is_enforced_while_streaming(svc, monkeypatch, make_client, fixtures):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AFTERSTATE_MAX_ARTIFACT_BYTES", "100000")
    from afterstate.api.deps import reset_services_cache
    from afterstate.config import reset_settings_cache

    reset_settings_cache()
    reset_services_cache()
    c = make_client()
    c.register("big@example.test")
    ws = c.post("/api/v1/workspaces", {"name": "W"}).json()["memberships"][0]["workspace_id"]
    r = c.upload(ws, good_dump(fixtures))  # 222 KB > 100 KB
    assert r.status_code == 413 and r.json()["error"]["code"] == "artifact_too_large"
    from pathlib import Path

    assert not [p for p in Path(svc.settings.artifact_dir).rglob("*") if p.is_file() and ".tmp" not in p.parts]


@pytest.mark.parametrize("variant,klass", [("corrupt", "ARTIFACT_CORRUPT"), ("plain", "FORMAT_UNSUPPORTED")])
def test_bad_backups_are_registered_as_rejected_with_a_reason(owner, fixtures, variant, klass):  # type: ignore[no-untyped-def]
    c, ws = owner
    r = c.upload(ws, fixtures.path(variant).read_bytes())
    assert r.status_code == 201 and r.json()["status"] == "rejected"
    assert r.json()["verification"]["failure_class"] == klass
    listed = c.get(f"/api/v1/workspaces/{ws}/artifacts", params={"status": "rejected"}).json()
    assert listed["total"] == 1


# ------------------------------------------------------------------------ projects ---------------
def test_project_rules_are_validated_and_unsafe_sql_is_refused(owner):  # type: ignore[no-untyped-def]
    c, ws = owner
    base = f"/api/v1/workspaces/{ws}"
    bad = c.post(f"{base}/projects", {"name": "p", "rules": [{"kind": "sql_assertion", "sql": "DELETE FROM orders"}]})
    assert bad.status_code == 422 and "SELECT" in json.dumps(bad.json())
    for sql in ("SELECT pg_read_file('/etc/passwd')", "SELECT 1; DROP TABLE orders", "COPY orders TO PROGRAM 'id'"):
        assert c.post(f"{base}/rules/validate", {"kind": "sql_assertion", "sql": sql}).status_code == 422
    ok = c.post(f"{base}/rules/validate", {"kind": "table", "table": "orders"})
    assert ok.status_code == 200 and ok.json()["stage"] == "structure"
    assert c.post(f"{base}/projects", {"name": "p", "rules": [{"kind": "nonsense"}]}).status_code == 422
    assert c.post(f"{base}/projects", {"name": "p", "restore_options": {"jobs": 99}}).status_code == 422
    assert (
        c.post(f"{base}/projects", {"name": "p", "restore_options": {"extra_args": "--clean"}}).status_code == 422
    )  # no arbitrary pg_restore flags
    first = c.post(f"{base}/projects", {"name": "dup"})
    assert first.status_code == 201 and c.post(f"{base}/projects", {"name": "dup"}).status_code == 409


def test_execution_plan_preview_explains_risks(owner, fixtures):  # type: ignore[no-untyped-def]
    c, ws = owner
    base = f"/api/v1/workspaces/{ws}"
    p = c.post(
        f"{base}/projects", {"name": "bare", "rpo_seconds": 3600, "rules": [{"kind": "http_check", "path": "/healthz"}]}
    ).json()
    plan = c.get(f"{base}/projects/{p['id']}/plan").json()
    text = " ".join(plan["warnings"])
    assert "No backup artifact" in text and "no application suite" in text and "RPO will be reported as UNKNOWN" in text
    assert (
        plan["restore"]["command_template"][0] == "pg_restore"
        and "--exit-on-error" in plan["restore"]["command_template"]
    )
    assert [s["state"] for s in plan["stages"]][0] == "PREPARING"


# ------------------------------------------------------------------ the full workflow -----------
def test_end_to_end_register_configure_run_report_repeat_and_compare(owner, svc, fixtures):  # type: ignore[no-untyped-def]
    c, ws = owner
    base = f"/api/v1/workspaces/{ws}"
    art = c.upload(
        ws, good_dump(fixtures), backup_created_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat()
    ).json()
    project = c.post(
        f"{base}/projects",
        {
            "name": "orders-db",
            "artifact_id": art["id"],
            "rto_seconds": 300,
            "rpo_seconds": 172_800,
            "app_suite": "reference-shop",
            "max_attempts": 1,
            "rules": shop_rules(fixtures.meta),
        },
    )
    assert project.status_code == 201, project.text
    pid = project.json()["id"]

    first = c.post(f"{base}/projects/{pid}/runs", {}, headers={"Idempotency-Key": "ci-run-1"})
    assert first.status_code == 202 and first.json()["state"] == "QUEUED"
    replay = c.post(f"{base}/projects/{pid}/runs", {}, headers={"Idempotency-Key": "ci-run-1"})
    assert replay.json()["id"] == first.json()["id"] and replay.json()["idempotent_replay"] is True
    clash = c.post(f"{base}/projects/{pid}/runs", {})
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "active_run_exists"
    assert clash.json()["error"]["details"]["run_id"] == first.json()["id"]

    assert Worker(svc).drain() == 1
    run_id = first.json()["id"]
    run = c.get(f"{base}/runs/{run_id}").json()
    assert run["state"] == "SUCCEEDED" and run["cleanup"]["state"] == "DONE", json.dumps(run["failure_message"])
    assert len(run["results"]) >= 25 and all(r["outcome"] == "PASS" for r in run["results"] if r["required"])
    assert [s["name"] for s in run["stages"]][:3] == ["artifact_verification", "environment_provisioning", "restore"]
    assert run["objectives"]["RTO"]["status"] == "MET" and run["metrics"]["restore_throughput_mb_s"] > 0

    ev = c.get(f"{base}/runs/{run_id}/events").json()
    assert ev["terminal"] and any("pg_restore: creating" in e["message"] for e in ev["events"])
    assert (
        c.get(f"{base}/runs/{run_id}/events", params={"after": ev["next_after"]}).json()["events"] == []
    )  # cursor polling

    rep = c.get(f"{base}/runs/{run_id}/report", params={"format": "json"})
    doc = rep.json()
    assert rep.headers["x-evidence-sha256"] == hashlib.sha256(rep.content).hexdigest()
    assert (
        doc["schema"] == "afterstate.report/v1"
        and doc["run"]["state"] == "SUCCEEDED"
        and doc["artifact"]["sha256"] == art["sha256"]
    )
    html = c.get(f"{base}/runs/{run_id}/report")
    assert (
        html.headers["content-type"].startswith("text/html")
        and "default-src 'none'" in html.headers["content-security-policy"]
    )
    assert "Recovery verified" in html.text and "<script" not in html.text

    # evidence is tamper-evident: a modified report is refused, not served
    with svc.sf() as s:
        ev_row = s.scalar(select(RunEvidence).where(RunEvidence.kind == "report_json"))
        path = svc.storage.resolve(ev_row.storage_key)
    path.write_bytes(path.read_bytes() + b" ")
    tampered = c.get(f"{base}/runs/{run_id}/report", params={"format": "json"})
    assert tampered.status_code == 500 and tampered.json()["error"]["code"] == "evidence_integrity_failure"

    # repeat the run and compare
    second = c.post(f"{base}/projects/{pid}/runs", {}).json()
    assert Worker(svc).drain() == 1
    cmp = c.get(f"{base}/runs/compare", params={"a": run_id, "b": second["id"]}).json()
    assert cmp["same_artifact"] and cmp["same_project"] and cmp["check_changes"] == []
    assert (
        cmp["metric_deltas"]["restore_seconds"]["a"] > 0
        and cmp["metric_deltas"]["restore_seconds"]["delta"] is not None
    )

    ov = c.get(f"{base}/overview").json()
    assert ov["readiness"]["verified"] == 1 and ov["failure_rate_30d"]["finished"] == 2 and len(ov["series"]) == 2
    assert ov["unverified_artifacts"]["count"] == 0 and ov["projects"][0]["readiness"] == "verified"
    runs = c.get(f"{base}/runs", params={"state": "SUCCEEDED", "page_size": 1}).json()
    assert runs["total"] == 2 and len(runs["items"]) == 1
    actions = [e["action"] for e in c.get(f"{base}/audit", params={"page_size": 100}).json()["items"]]
    assert {"run.start", "run.succeeded", "artifact.register", "project.create"} <= set(actions)


def test_queued_runs_can_be_cancelled_and_cancelled_runs_do_not_execute(owner, svc, fixtures):  # type: ignore[no-untyped-def]
    c, ws = owner
    base = f"/api/v1/workspaces/{ws}"
    art = c.upload(ws, good_dump(fixtures)).json()
    pid = c.post(f"{base}/projects", {"name": "p", "artifact_id": art["id"]}).json()["id"]
    run = c.post(f"{base}/projects/{pid}/runs", {}).json()
    r = c.post(f"{base}/runs/{run['id']}/cancel")
    assert r.status_code == 200 and r.json()["run"]["state"] == "CANCELLED"
    assert Worker(svc).drain() == 0
    assert c.post(f"{base}/runs/{run['id']}/cancel").status_code == 409
    assert c.post(f"{base}/projects/{pid}/runs", {}).status_code == 202  # the project slot was released


def test_failed_run_notifies_in_app_and_by_webhook(owner, svc, fixtures):  # type: ignore[no-untyped-def]
    import http.server
    import threading

    received: list[dict[str, Any]] = []

    class Hook(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a: Any) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        c, ws = owner
        base = f"/api/v1/workspaces/{ws}"
        assert c.patch(base, {"webhook_url": f"http://127.0.0.1:{server.server_address[1]}/hook"}).status_code == 200
        art = c.upload(ws, fixtures.path("corrupt").read_bytes()).json()
        pid = c.post(f"{base}/projects", {"name": "p", "artifact_id": art["id"], "max_attempts": 1}).json()["id"]
        run_id = c.post(f"{base}/projects/{pid}/runs", {}).json()["id"]
        Worker(svc).drain()
        assert c.get(f"{base}/runs/{run_id}").json()["failure_class"] == "ARTIFACT_CORRUPT"
        notes = c.get(f"{base}/notifications").json()["items"]
        assert notes and notes[0]["kind"] == "run_failed" and notes[0]["run_id"] == run_id
        from afterstate.worker import notify

        assert notify.dispatch_pending(svc) == 1
        assert received and received[0]["run_id"] == run_id and received[0]["failure_class"] == "ARTIFACT_CORRUPT"
    finally:
        server.shutdown()


def test_webhook_urls_cannot_target_internal_addresses_when_private_hosts_are_disallowed(svc):  # type: ignore[no-untyped-def]
    from afterstate.worker.notify import check_webhook_url

    for url in (
        "http://127.0.0.1/x",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5/",
        "http://[::1]/",
        "ftp://example.com/x",
        "http://user:pw@example.com/",
    ):
        assert check_webhook_url(url, allow_private=False), url


# --------------------------------------------------------------------------- lab ----------------
def test_lab_scenarios_run_the_real_engine_and_reset_only_what_they_own(owner, svc, fixtures, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr("afterstate.lab.service.fixture_set", lambda _svc: fixtures)
    c, ws = owner
    base = f"/api/v1/workspaces/{ws}"
    real_art = c.upload(ws, good_dump(fixtures)).json()
    real_project = c.post(f"{base}/projects", {"name": "production-orders", "artifact_id": real_art["id"]}).json()

    listing = c.get(f"{base}/lab/scenarios").json()
    assert [s["key"] for s in listing["items"]][:2] == ["successful_restore", "corrupted_backup"] and all(
        s["status"] == "not_run" for s in listing["items"]
    )
    started = c.post(f"{base}/lab/scenarios/corrupted_backup/run", {})
    assert started.status_code == 202
    run_id = started.json()["run_id"]
    assert Worker(svc).drain() == 1
    run = c.get(f"{base}/runs/{run_id}").json()
    assert (
        run["state"] == "FAILED"
        and run["failure_class"] == "ARTIFACT_CORRUPT"
        and run["lab_scenario"] == "corrupted_backup"
    )
    status = next(s for s in c.get(f"{base}/lab/scenarios").json()["items"] if s["key"] == "corrupted_backup")
    assert status["status"] == "matched" and "as expected" in status["note"]

    reset = c.post(f"{base}/lab/scenarios/corrupted_backup/reset", {})
    assert (
        reset.status_code == 200 and reset.json()["removed"]["runs"] == 1 and reset.json()["removed"]["projects"] == 1
    )
    assert c.get(f"{base}/runs/{run_id}").status_code == 404
    assert c.get(f"{base}/projects/{real_project['id']}").status_code == 200  # customer resources are untouched
    assert c.get(f"{base}/artifacts/{real_art['id']}").status_code == 200
    assert c.post(f"{base}/lab/scenarios/not_a_scenario/run", {}).status_code == 404


# ----------------------------------------------------------------------- schedules ---------------
def test_schedule_validation_and_firing_policies(owner, svc, fixtures):  # type: ignore[no-untyped-def]
    from afterstate.db.models import Schedule, ScheduleFiring
    from afterstate.worker import scheduler

    c, ws = owner
    base = f"/api/v1/workspaces/{ws}"
    art = c.upload(ws, good_dump(fixtures)).json()
    pid = c.post(
        f"{base}/projects", {"name": "p", "artifact_id": art["id"], "rules": [{"kind": "table", "table": "orders"}]}
    ).json()["id"]
    body = {"name": "nightly", "project_id": pid, "cron_expr": "0 3 * * *", "timezone": "Europe/Berlin"}
    for bad in (
        {**body, "cron_expr": "* * * * *"},
        {**body, "cron_expr": "not cron at all"},
        {**body, "timezone": "Mars/Base"},
    ):
        assert c.post(f"{base}/schedules", bad).status_code == 422
    created = c.post(f"{base}/schedules", body)
    assert created.status_code == 201
    sid = created.json()["id"]
    nxt = datetime.fromisoformat(created.json()["next_run_at"])
    assert nxt > datetime.now(UTC) and nxt.astimezone(UTC).hour in (
        1,
        2,
    )  # 03:00 Berlin is 01:00/02:00 UTC depending on DST

    # on time: fires one run
    with svc.sf() as s, s.begin():
        s.execute(
            update(Schedule).where(Schedule.id == sid).values(next_run_at=datetime.now(UTC) - timedelta(seconds=5))
        )
    assert scheduler.tick(svc) == 1
    firing = c.get(f"{base}/schedules/{sid}/firings").json()["items"]
    assert firing[0]["outcome"] == "enqueued" and firing[0]["run_id"]
    # the project is busy: the next due slot is skipped as overlap, not queued twice
    with svc.sf() as s, s.begin():
        s.execute(
            update(Schedule).where(Schedule.id == sid).values(next_run_at=datetime.now(UTC) - timedelta(seconds=5))
        )
    assert scheduler.tick(svc) == 0
    assert c.get(f"{base}/schedules/{sid}/firings").json()["items"][0]["outcome"] == "skipped_overlap"
    Worker(svc).drain()
    assert next(r for r in c.get(f"{base}/runs").json()["items"])["trigger"] == "schedule"

    # worker was down for three days: policy 'skip' records the missed slots and does not run
    with svc.sf() as s, s.begin():
        s.execute(
            update(Schedule)
            .where(Schedule.id == sid)
            .values(next_run_at=datetime.now(UTC) - timedelta(days=3, hours=1), missed_run_policy="skip")
        )
    assert scheduler.tick(svc) == 0
    with svc.sf() as s:
        missed = s.scalars(select(ScheduleFiring).where(ScheduleFiring.outcome == "skipped_missed")).all()
        assert len(missed) >= 3
    # policy 'run_once' runs a single catch-up
    with svc.sf() as s, s.begin():
        s.execute(
            update(Schedule)
            .where(Schedule.id == sid)
            .values(next_run_at=datetime.now(UTC) - timedelta(days=3, hours=1), missed_run_policy="run_once")
        )
    assert scheduler.tick(svc) == 1
    Worker(svc).drain()
    assert c.patch(f"{base}/schedules/{sid}", {"enabled": False}).json()["next_run_at"] is None


def test_health_endpoints_report_real_state(make_client, svc):  # type: ignore[no-untyped-def]
    c = make_client()
    assert c.get("/api/v1/health/live").json() == {"status": "ok"}
    r = c.get("/api/v1/health/ready")
    assert r.status_code == 200 and r.json()["checks"]["database"] == "ok" and r.json()["checks"]["storage"] == "ok"
    assert r.json()["checks"]["worker"]["status"] == "stale"  # no worker has reported in
    Worker(svc).beat()
    assert c.get("/api/v1/health/ready").json()["checks"]["worker"]["status"] == "ok"
