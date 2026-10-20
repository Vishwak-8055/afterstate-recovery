from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from afterstate.config import reset_settings_cache
from afterstate.db.session import reset_engine_cache
from afterstate.engine.pgtools import ToolNotFound, locate_pg_tools

BACKEND = Path(__file__).resolve().parents[1]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.geteuid() == 0:
        skip = pytest.mark.skip(reason="integration tests start PostgreSQL, which refuses to run as root")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip)


@pytest.fixture(scope="session")
def pg_tools():  # type: ignore[no-untyped-def]
    try:
        return locate_pg_tools()
    except ToolNotFound as exc:
        pytest.skip(str(exc))


@pytest.fixture(scope="session")
def platform_url(pg_tools) -> Iterator[str]:  # type: ignore[no-untyped-def]
    """A migrated platform database: AFTERSTATE_TEST_DATABASE_URL if given (CI), else a throwaway cluster."""
    from afterstate.devcluster import PlatformCluster

    explicit = os.environ.get("AFTERSTATE_TEST_DATABASE_URL")
    cluster = None if explicit else PlatformCluster(pg_tools)
    url = explicit or cluster.start()  # type: ignore[union-attr]
    env = dict(os.environ, AFTERSTATE_DATABASE_URL=url)
    res = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=False,
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"migration failed:\n{res.stdout}\n{res.stderr}")
    yield url
    if cluster:
        cluster.stop()


@pytest.fixture(scope="session")
def fixtures(pg_tools):  # type: ignore[no-untyped-def]
    """Real synthetic pg_dump archives, built once and reused across runs."""
    from afterstate.lab.fixtures import FIXTURE_VERSION, build_fixture_set

    root = Path(f"/tmp/afterstate-test-fixtures-v{FIXTURE_VERSION}-{os.getuid()}")
    return build_fixture_set(pg_tools, Path(f"/tmp/afs-fx-scratch-{os.getuid()}"), root)


@pytest.fixture
def settings(platform_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    short = Path(f"/tmp/afs-t-{uuid.uuid4().hex[:8]}")
    monkeypatch.setenv("AFTERSTATE_DATABASE_URL", platform_url)
    monkeypatch.setenv("AFTERSTATE_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("AFTERSTATE_SCRATCH_DIR", str(short / "scratch"))
    monkeypatch.setenv("AFTERSTATE_ENVIRONMENT", "test")
    monkeypatch.setenv("AFTERSTATE_LOG_JSON", "false")
    monkeypatch.setenv("AFTERSTATE_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("AFTERSTATE_WORKER_LEASE_SECONDS", "20")
    monkeypatch.setenv("AFTERSTATE_WORKER_HEARTBEAT_SECONDS", "2")
    monkeypatch.setenv("AFTERSTATE_CLEANUP_RETRY_DELAY_SECONDS", "1")
    monkeypatch.setenv("AFTERSTATE_WEBHOOK_ALLOW_PRIVATE_HOSTS", "true")
    reset_settings_cache()
    reset_engine_cache()
    from afterstate.config import get_settings

    yield get_settings()
    reset_engine_cache()
    reset_settings_cache()


@pytest.fixture
def svc(settings):  # type: ignore[no-untyped-def]
    from afterstate.services import build_services

    services = build_services(settings)
    yield services
    # leave nothing behind: any environment a test leaked is a test failure signal
    scratch = Path(settings.scratch_dir)
    leaked = [p.name for p in scratch.glob("e-*")] if scratch.exists() else []
    from sqlalchemy import text

    with services.sf() as s, s.begin():
        s.execute(text("TRUNCATE users, workspaces, worker_heartbeats, audit_events RESTART IDENTITY CASCADE"))
    from afterstate.engine.environment import LocalClusterEnvironment

    for name in leaked:  # best-effort cleanup (kills orphaned postgres too) so one failing test cannot poison the next
        try:
            LocalClusterEnvironment.destroy_path(scratch / name, scratch)
        except Exception:
            import shutil

            shutil.rmtree(scratch / name, ignore_errors=True)
    assert not leaked, f"test leaked restore environments: {leaked}"
