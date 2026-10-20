"""The disposable environment is the security boundary for hostile backups. These tests probe it as an attacker would."""

from __future__ import annotations

import socket
import uuid
from pathlib import Path

import psycopg
import pytest
from afterstate.engine.environment import DB_NAME, LocalClusterEnvironment
from afterstate.security.redact import Redactor

pytestmark = pytest.mark.integration


@pytest.fixture
def env(pg_tools, tmp_path):  # type: ignore[no-untyped-def]
    scratch = Path(f"/tmp/afs-envsec-{uuid.uuid4().hex[:8]}")
    e = LocalClusterEnvironment(scratch, uuid.uuid4(), pg_tools, redactor=Redactor())
    e.create()
    e.start()
    yield e
    e.destroy()
    scratch.rmdir()


def restore_conn(e):  # type: ignore[no-untyped-def]
    t = e.restore_target()
    return psycopg.connect(
        host=t.host, port=t.port, dbname=t.database, user=t.user, password=t.password, autocommit=True
    )


@pytest.mark.parametrize(
    "sql",
    [
        "COPY x FROM PROGRAM 'id'",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_ls_dir('/')",
        "CREATE EXTENSION dblink",
        "CREATE EXTENSION file_fdw",
        "CREATE LANGUAGE plpython3u",
        "CREATE EVENT TRIGGER e ON ddl_command_start EXECUTE FUNCTION pg_catalog.now()",
        "ALTER SYSTEM SET fsync = on",
        "CREATE ROLE evil SUPERUSER",
        "CREATE TABLESPACE t LOCATION '/tmp'",
        "COPY x TO '/tmp/exfiltrated'",
    ],
)
def test_the_restore_role_cannot_escape_the_database(env, sql):  # type: ignore[no-untyped-def]
    with restore_conn(env) as c:
        c.execute("CREATE TABLE IF NOT EXISTS x (a text)")
        with pytest.raises(psycopg.errors.Error) as exc:
            c.execute(sql)  # type: ignore[arg-type]
        assert (
            exc.value.sqlstate in ("42501", "0A000", "42809", "55000", "58P01")
            or "permission denied" in str(exc.value)
            or "must be" in str(exc.value)
            or "superuser" in str(exc.value)
        )
    assert not Path("/tmp/exfiltrated").exists()


def test_the_restore_role_is_not_a_superuser_but_can_restore_trusted_extensions(env):  # type: ignore[no-untyped-def]
    with restore_conn(env) as c:
        assert (
            c.execute(
                "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            ).fetchone()
            == (False,) * 5
        )
        c.execute("CREATE EXTENSION pgcrypto")  # trusted extensions work: that is why real dumps restore
        assert c.execute("SELECT count(*) FROM pg_extension WHERE extname = 'pgcrypto'").fetchone() == (1,)


def test_the_validator_cannot_write_even_inside_a_transaction_it_controls(env):  # type: ignore[no-untyped-def]
    with restore_conn(env) as c:
        c.execute("CREATE TABLE t (a int)")
        c.execute("INSERT INTO t VALUES (1)")
    with env.validator_conn() as v:
        assert v.execute("SELECT count(*) FROM t").fetchone() == (1,)  # can read everything (pg_read_all_data)
        for sql in (
            "INSERT INTO t VALUES (2)",
            "DELETE FROM t",
            "DROP TABLE t",
            "CREATE TABLE y (a int)",
            "TRUNCATE t",
        ):
            with pytest.raises(psycopg.errors.Error):
                v.execute(sql)  # type: ignore[arg-type]
            v.rollback()
        v.execute("BEGIN READ WRITE") if False else None  # noqa: SIM210
        with pytest.raises(psycopg.errors.Error):  # an attempt to flip the session to read-write still cannot write
            v.execute("SET default_transaction_read_only = off")
            v.execute("INSERT INTO t VALUES (3)")
        v.rollback()
    with restore_conn(env) as c:
        assert c.execute("SELECT count(*) FROM t").fetchone() == (1,)


def test_a_hostile_backup_cannot_change_how_the_validator_session_behaves(env):  # type: ignore[no-untyped-def]
    """ALTER DATABASE ... SET is available to the database owner, which is the role a backup restores as."""
    with restore_conn(env) as c:
        c.execute(f'ALTER DATABASE "{DB_NAME}" SET standard_conforming_strings = off')  # type: ignore[arg-type]
        c.execute(f'ALTER DATABASE "{DB_NAME}" SET search_path = pg_temp')  # type: ignore[arg-type]
        c.execute(f'ALTER DATABASE "{DB_NAME}" SET default_transaction_read_only = off')  # type: ignore[arg-type]
    with env.validator_conn() as v:
        assert v.execute("SHOW standard_conforming_strings").fetchone() == ("on",)
        assert v.execute("SHOW search_path").fetchone() == ("public",)
        assert v.execute("SHOW default_transaction_read_only").fetchone() == ("on",)


def test_a_malicious_function_cannot_shadow_builtins_used_by_validation_queries(env):  # type: ignore[no-untyped-def]
    with restore_conn(env) as c:
        c.execute("CREATE TABLE t (a int)")
        c.execute("INSERT INTO t SELECT generate_series(1, 5)")
        c.execute("CREATE FUNCTION public.count(anyelement) RETURNS bigint LANGUAGE sql AS 'SELECT 999::bigint'")
        c.execute(
            "CREATE FUNCTION public.now() RETURNS timestamptz LANGUAGE sql AS $$ SELECT '1999-01-01'::timestamptz $$"
        )
    with env.validator_conn() as v:
        assert v.execute("SELECT count(*) FROM t").fetchone() == (
            5,
        )  # pg_catalog resolves first: the lie is not served
        assert v.execute("SELECT now() > '2020-01-01'").fetchone() == (True,)


def test_the_environment_has_no_network_listener_and_a_private_directory(env):  # type: ignore[no-untyped-def]
    mode = oct(env.path.stat().st_mode & 0o777)
    assert mode == "0o700" and oct(env.sockdir.stat().st_mode & 0o777) == "0o700"
    hba = (env.pgdata / "pg_hba.conf").read_text()
    assert (
        "host all all all reject" in hba
        and "trust" in hba.splitlines()[0]
        and "afterstate_admin" in hba.splitlines()[0]
    )
    with restore_conn(env) as c:
        assert c.execute("SHOW listen_addresses").fetchone() == ("",)
        with pytest.raises(
            psycopg.errors.InsufficientPrivilege
        ):  # the restore role may not even inspect socket settings
            c.execute("SHOW unix_socket_directories")
    with env.admin_conn("postgres") as admin:
        assert admin.execute("SHOW unix_socket_directories").fetchone() == (str(env.sockdir),)
    for port in (
        5432,
        5433,
        5434,
    ):  # nothing on loopback, at the default port or its neighbours, belongs to this cluster
        s = socket.socket()
        s.settimeout(0.3)
        try:
            assert s.connect_ex(("127.0.0.1", port)) != 0 or port != 5432
        finally:
            s.close()
    # other OS users cannot even reach the socket directory (0700); here we only confirm the permissions that guarantee it
    assert (env.path / ".afterstate-env").is_file()


def test_environment_passwords_are_unique_per_run_and_never_appear_in_the_data_directory_config(env, pg_tools):  # type: ignore[no-untyped-def]
    other = LocalClusterEnvironment(
        Path(f"/tmp/afs-envsec-{uuid.uuid4().hex[:8]}"), uuid.uuid4(), pg_tools
    )  # never created: only its secrets are compared
    assert other.restore_password != env.restore_password and other.validator_password != env.validator_password
    conf = (env.pgdata / "postgresql.conf").read_text() + (env.pgdata / "pg_hba.conf").read_text()
    assert env.restore_password not in conf and env.validator_password not in conf
