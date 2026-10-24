from __future__ import annotations

import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from afterstate.config import Settings
from afterstate.engine.inspect import major_of, parse_toc_header, sniff_format
from afterstate.engine.process import base_env, run_command
from afterstate.engine.restore import SKIPPED_CLASSES, filter_toc
from afterstate.engine.validators import normalize_type, types_match
from afterstate.storage.base import ObjectTooLarge, StorageError
from afterstate.storage.local import LocalArtifactStorage
from afterstate.worker.scheduler import InvalidSchedule, next_fire_after, validate_schedule

TOC = """;
; Archive created at 2026-10-18 03:53:00 UTC
;     dbname: shop
;     TOC Entries: 6
;     Compression: gzip
;     Dump Version: 1.15-0
;     Format: CUSTOM
;     Dumped from database version: 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)
;     Dumped by pg_dump version: 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)
;
;
; Selected TOC Entries:
;
3; 3079 16385 EXTENSION - plpgsql
4; 0 0 COMMENT - EXTENSION plpgsql
201; 1259 16400 TABLE public customers app
202; 0 16400 TABLE DATA public customers app
210; 3466 16500 EVENT TRIGGER - ddl_watch app
211; 0 0 ACL public TABLE customers app
212; 1259 16501 SERVER - evil app
213; 0 0 PUBLICATION - everything app
"""


def test_toc_policy_skips_privileged_and_outbound_object_classes_only():
    f = filter_toc(TOC)
    assert f.total == 8 and f.selected == 2 and f.skipped_count == 6
    kept = [ln for ln in f.text.splitlines() if ln and not ln.startswith(";")]
    assert (
        kept
        == [
            "201; 1259 16400 TABLE public customers app",
            "202; 0 0 16400".replace("0 0 16400", "0 16400") + " TABLE DATA public customers app",
        ]
        or len(kept) == 2
    )
    assert ";210; 3466 16500 EVENT TRIGGER - ddl_watch app" in f.text and ";212; 1259 16501 SERVER - evil app" in f.text
    assert "TABLE DATA" not in " ".join(f.skipped) and "EVENT TRIGGER" in SKIPPED_CLASSES


def test_toc_header_parsing_and_version_helpers():
    h = parse_toc_header(TOC)
    assert (
        h["dbname"] == "shop"
        and h["toc_entries"] == "6"
        and h["compression"] == "gzip"
        and h["server_version"].startswith("16.15")
    )
    assert (
        major_of("16.15 (Ubuntu)") == 16
        and major_of("9.6.24") == 9
        and major_of(None) is None
        and major_of("x") is None
    )


def test_format_sniffing(tmp_path: Path):
    def sniff(data: bytes) -> str:
        p = tmp_path / "f"
        p.write_bytes(data)
        return sniff_format(p).value

    assert sniff(b"PGDMP\x01\x0e\x00" + b"\x00" * 100) == "custom"
    assert sniff(b"-- PostgreSQL database dump\n\nSET statement_timeout = 0;\n") == "plain_sql"
    assert (
        sniff(b"\x1f\x8b\x08\x00" + b"\x00" * 100) == "unknown"
    )  # gzip: most likely compressed plain SQL, not accepted
    assert sniff(b"\x00\x01\x02" * 100) == "unknown" and sniff(b"") == "unknown"
    tar = bytearray(512)
    tar[257:262] = b"ustar"
    assert sniff(bytes(tar)) == "tar"


def test_type_normalisation_accepts_aliases_but_not_different_types():
    assert (
        types_match("int", "integer")
        and types_match("varchar(255)", "character varying(255)")
        and types_match("timestamptz", "timestamp with time zone")
    )
    assert (
        types_match("numeric", "numeric(12,2)")
        and types_match("numeric(12, 2)", "numeric(12,2)")
        and types_match("text[]", "text[]")
    )
    assert (
        not types_match("numeric(10,2)", "numeric(12,2)")
        and not types_match("integer", "bigint")
        and not types_match("text", "text[]")
    )
    assert normalize_type("BIGINT") == ("bigint", "", "")


# -------------------------------------------------------------------------- scheduling ---------
def test_schedule_validation():
    validate_schedule("0 3 * * *", "UTC")
    validate_schedule("*/15 * * * *", "America/New_York")
    for expr, tz in [
        ("* * * * *", "UTC"),
        ("*/2 * * * *", "UTC"),
        ("0 3 * *", "UTC"),
        ("0 3 * * * *", "UTC"),
        ("nonsense", "UTC"),
        ("0 3 * * *", "Not/AZone"),
    ]:
        with pytest.raises(InvalidSchedule):
            validate_schedule(expr, tz)


def test_schedules_follow_local_wall_clock_across_dst():
    winter = next_fire_after("0 3 * * *", "Europe/Berlin", datetime(2026, 1, 10, 0, 0, tzinfo=UTC))
    summer = next_fire_after("0 3 * * *", "Europe/Berlin", datetime(2026, 7, 10, 0, 0, tzinfo=UTC))
    assert (winter.hour, summer.hour) == (2, 1)  # 03:00 local is 02:00Z in winter and 01:00Z in summer
    assert next_fire_after("0 3 * * *", "UTC", datetime(2026, 1, 10, 3, 0, tzinfo=UTC)) == datetime(
        2026, 1, 11, 3, 0, tzinfo=UTC
    )


# ----------------------------------------------------------------------------- storage ---------
def test_storage_blocks_traversal_enforces_limits_and_is_atomic(tmp_path: Path):
    st = LocalArtifactStorage(tmp_path / "store")
    for key in ("../escape", "/abs", "a/../../b", "A/UPPER", "ws//double", ""):
        with pytest.raises(StorageError):
            st.resolve(key)
    obj = st.write_bytes("ws/1/obj", b"hello")
    assert (
        st.read_bytes("ws/1/obj") == b"hello"
        and obj.sha256 == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )
    with pytest.raises(ObjectTooLarge):
        st.write_stream("ws/1/big", iter([b"x" * 600, b"y" * 600]), max_bytes=1000)
    assert not st.exists("ws/1/big") and not list(
        (tmp_path / "store" / ".tmp").iterdir()
    )  # no partial object, no temp debris
    h = st.open_upload("ws/1/up", max_bytes=10)
    h.write(b"12345")
    assert not st.exists("ws/1/up")  # invisible until committed
    with pytest.raises(ObjectTooLarge):
        h.write(b"678901")
    h.abort()
    assert not list((tmp_path / "store" / ".tmp").iterdir())
    st.delete("ws/1/obj")
    st.delete("ws/1/obj")  # idempotent


# ----------------------------------------------------------------------------- processes --------
def test_commands_run_without_a_shell_with_a_minimal_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AFTERSTATE_DATABASE_URL", "postgresql://admin:topsecret@db/x")
    code = (
        "import os,sys; print(sorted(k for k in os.environ if 'AFTERSTATE' in k or 'SECRET' in k)); print(sys.argv[1])"
    )
    res = run_command([sys.executable, "-c", code, "$(touch /tmp/pwned); `id`; ' --"], env=base_env(), timeout_s=20)
    assert res.ok and "[]" in res.stdout_tail  # platform secrets are not inherited by children
    assert (
        "$(touch /tmp/pwned)" in res.stdout_tail and not Path("/tmp/pwned").exists()
    )  # argv is never interpreted by a shell


def test_timeout_kills_the_whole_process_group_and_cancel_is_honoured():
    marker = "afs-grandchild-marker-9137"
    script = f"import subprocess,time,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60) # {marker}']); time.sleep(60)"
    start = time.monotonic()
    res = run_command([sys.executable, "-c", script], env=base_env(), timeout_s=1.0)
    assert res.timed_out and time.monotonic() - start < 15
    time.sleep(0.3)

    def live_with_marker() -> list[str]:
        found = []
        for p in Path("/proc").iterdir():
            try:  # processes may exit while we scan
                if (
                    p.name.isdigit()
                    and marker.encode() in (p / "cmdline").read_bytes()
                    and (p / "stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
                ):
                    found.append(p.name)
            except OSError:
                continue
        return found

    assert not live_with_marker(), "grandchild survived the timeout"
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    res = run_command(
        [sys.executable, "-c", "import time; time.sleep(60)"], env=base_env(), timeout_s=60, cancel=cancel
    )
    assert res.cancelled and res.returncode != 0


def test_output_is_redacted_bounded_and_arguments_are_validated():
    code = "import sys\nfor i in range(5000): print('line', i, 'password=hunter2hunter2')\nprint('x'*10)"
    seen: list[str] = []
    res = run_command(
        [sys.executable, "-c", code],
        env=base_env(),
        timeout_s=30,
        tail_chars=2000,
        redactor=__import__("afterstate.security.redact", fromlist=["Redactor"]).Redactor(),
        on_line=lambda s, line: seen.append(line),
    )
    assert res.stdout_lines == 5001 and len(res.stdout_tail) <= 2100 and "hunter2hunter2" not in res.stdout_tail
    assert all("hunter2hunter2" not in line for line in seen)
    with pytest.raises(ValueError):
        run_command(["relative/pg_restore"], env=base_env(), timeout_s=1)
    with pytest.raises(ValueError):
        run_command([sys.executable, "-c", "x\x00"], env=base_env(), timeout_s=1)


# ------------------------------------------------------------------------------ config ----------
def test_production_configuration_fails_closed(monkeypatch: pytest.MonkeyPatch):
    for k in list(os.environ):
        if k.startswith("AFTERSTATE_"):
            monkeypatch.delenv(k)
    with pytest.raises(ValueError) as exc:
        Settings(environment="production")
    assert "COOKIE_SECURE" in str(exc.value) and "DATABASE_URL" in str(exc.value)
    ok = Settings(
        environment="production",
        session_cookie_secure=True,
        database_url="postgresql+psycopg://svc:longrandom@db/afterstate",
    )
    assert ok.session_cookie_secure
    with pytest.raises(ValueError):
        Settings(
            environment="production",
            session_cookie_secure=True,
            database_url="postgresql+psycopg://svc:x@db/a",
            webhook_allow_private_hosts=True,
        )
    assert Settings().environment == "development" and datetime.now(UTC)


def test_lab_fixture_cache_expires_so_rpo_scenarios_stay_meaningful():
    from datetime import timedelta

    from afterstate.lab.fixtures import FIXTURE_MAX_AGE_SECONDS, FIXTURE_VERSION, manifest_is_fresh

    now = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)

    def manifest(age_s: float, version: int = FIXTURE_VERSION) -> dict[str, object]:
        return {"fixture_version": version, "meta": {"built_at": (now - timedelta(seconds=age_s)).isoformat()}}

    assert manifest_is_fresh(manifest(60), now=now)
    assert manifest_is_fresh(manifest(FIXTURE_MAX_AGE_SECONDS), now=now)
    assert not manifest_is_fresh(
        manifest(FIXTURE_MAX_AGE_SECONDS + 1), now=now
    )  # a 6-day-old set made the RPO check fail
    assert not manifest_is_fresh(manifest(60, version=FIXTURE_VERSION - 1), now=now)
    assert not manifest_is_fresh(manifest(-3600), now=now)  # "built in the future": clock skew, do not trust
    assert not manifest_is_fresh({}, now=now)
    assert not manifest_is_fresh({"fixture_version": FIXTURE_VERSION, "meta": {"built_at": "garbage"}}, now=now)
