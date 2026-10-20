from __future__ import annotations

import pytest
from afterstate.domain.enums import Role
from afterstate.domain.rules import parse_rule
from afterstate.domain.sqlsafety import UnsafeSql, validate_readonly_sql
from afterstate.security.passwords import hash_password, password_problem, verify_password
from afterstate.security.rbac import ROLE_PERMISSIONS, Permission, role_allows
from afterstate.security.redact import Redactor, redact_text, redact_value

# ------------------------------------------------------------------------------ SQL gate -------
ACCEPT = [
    "SELECT 1",
    "select count(*) = 0 from orders o left join customers c on c.id = o.customer_id where c.id is null;",
    "WITH t AS (SELECT 1 AS x) SELECT x FROM t",
    "SELECT ';' AS semicolon_in_a_literal",
    "SELECT $$; DROP TABLE x; --$$ AS dollar_quoted",
    "SELECT 'it''s fine; really' FROM orders",
    "SELECT /* DELETE FROM x; */ 1",
    "SELECT 1 -- ; DROP TABLE x",
    'SELECT "set", "copy" FROM t',
    "SELECT $tag$ INSERT INTO x $tag$",
    "VALUES (1), (2)",
    "TABLE orders",
]
REJECT = [
    ("", "empty"),
    ("   ;  ", "empty"),
    ("SELECT 1; SELECT 2", "single statement"),
    ("SELECT 1; DROP TABLE orders", "single statement"),
    ("DELETE FROM orders", "must start"),
    ("INSERT INTO t VALUES (1)", "must start"),
    ("UPDATE orders SET a = 1", "must start"),
    ("DROP TABLE orders", "must start"),
    ("COPY orders TO PROGRAM 'curl evil'", "must start"),
    ("CALL do_it()", "must start"),
    ("SELECT * INTO new_table FROM orders", "INTO"),
    ("WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d", "DELETE"),
    ("WITH i AS (INSERT INTO t VALUES (1) RETURNING *) SELECT 1", "INSERT"),
    ("SELECT set_config('x','y',false)", "set_config"),
    ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
    ("SELECT pg_terminate_backend(1)", "pg_terminate_backend"),
    ("SELECT lo_export(1, '/tmp/x')", "lo_export"),
    ("SELECT nextval('s')", "nextval"),
    ("SELECT dblink_connect('host=evil')", "dblink"),
    ("SELECT 1 /* unterminated", "unterminated"),
    ("SELECT 'unterminated", "unterminated"),
    ("SELECT $$unterminated", "unterminated"),
    ("SELECT 1 FOR UPDATE", "UPDATE"),
    ("SELECT 1\x00", "NUL"),
    ("SELECT 1; \n -- hidden\n DROP TABLE x", "single statement"),
    ("EXPLAIN ANALYZE SELECT 1", "must start"),
]


@pytest.mark.parametrize("sql", ACCEPT)
def test_read_only_sql_is_accepted(sql):
    assert validate_readonly_sql(sql)


@pytest.mark.parametrize("sql,why", REJECT)
def test_everything_else_is_rejected(sql, why):
    with pytest.raises(UnsafeSql) as exc:
        validate_readonly_sql(sql)
    assert why.lower() in str(exc.value).lower()


def test_trailing_semicolons_are_normalised_and_long_sql_is_refused():
    assert validate_readonly_sql("SELECT 1;;  ") == "SELECT 1"
    with pytest.raises(UnsafeSql):
        validate_readonly_sql("SELECT " + "1," * 20_000 + "1")


def test_backslash_handling_matches_postgresql_string_rules():
    # In a standard string a backslash is an ordinary character, so this is a complete literal followed by a
    # second statement. It must be rejected (fail-closed even if a session had standard_conforming_strings off).
    with pytest.raises(UnsafeSql):
        validate_readonly_sql(r"SELECT '\'; DROP TABLE x; --'")
    # In an E-string \' is an escaped quote, so this is ONE literal containing semicolons: valid and harmless.
    assert validate_readonly_sql(r"SELECT E'\'; DROP TABLE x; --'")
    assert validate_readonly_sql(r"SELECT E'it\'s; fine'")


# ----------------------------------------------------------------------------- rules -----------
def test_rule_models_validate_and_serialise_by_alias():
    r = parse_rule({"kind": "table", "schema": "billing", "table": "invoices"})
    assert r.schema_ == "billing" and r.display_name() == "Table billing.invoices exists"
    with pytest.raises(ValueError):
        parse_rule({"kind": "table", "table": "t", "unexpected": 1})
    with pytest.raises(ValueError):
        parse_rule({"kind": "row_count", "table": "t"})
    with pytest.raises(ValueError):
        parse_rule({"kind": "row_count", "table": "t", "exact": 1, "minimum": 0})
    with pytest.raises(ValueError):
        parse_rule({"kind": "constraint", "table": "t", "constraint_type": "foreign_key"})
    with pytest.raises(ValueError):
        parse_rule({"kind": "sql_assertion", "sql": "SELECT 1", "expect": "between"})
    with pytest.raises(ValueError):
        parse_rule({"kind": "dataset_checksum", "sql": "SELECT 1", "expected_sha256": "abc"})
    with pytest.raises(ValueError):
        parse_rule({"kind": "http_check", "path": "no-leading-slash"})
    assert parse_rule({"kind": "recovery_point", "sql": "SELECT max(t) FROM x"}).required is False


# ----------------------------------------------------------------------------- redaction -------
@pytest.mark.parametrize(
    "raw,must_not_contain",
    [
        ("connecting to postgresql://app:Sup3rS3cret@db.internal:5432/prod", "Sup3rS3cret"),
        ("postgresql+psycopg://user:p%40ss@host/db", "p%40ss"),
        ("PGPASSWORD=hunter2hunter2 pg_restore ...", "hunter2hunter2"),
        ("password: 'correct horse'", "correct horse"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig", "eyJhbGciOiJIUzI1NiJ9"),
        ("key AKIAIOSFODNN7EXAMPLE leaked", "AKIAIOSFODNN7EXAMPLE"),
        ('CONTEXT:  COPY customers, line 7: "7\tjane.doe@example.com\tJane Doe"', "jane.doe@example.com"),
        ("DETAIL:  Key (email)=(jane.doe@example.com) already exists.", "jane.doe@example.com"),
        ("DETAIL:  Failing row contains (1, jane.doe@example.com, null).", "jane.doe@example.com"),
        ("Command was: INSERT INTO customers VALUES ('jane.doe@example.com')", "jane.doe@example.com"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----", "MIIEow"),
    ],
)
def test_secrets_and_row_data_are_redacted(raw, must_not_contain):
    assert must_not_contain not in redact_text(raw)


def test_redactor_masks_runtime_generated_secrets_and_nested_keys():
    r = Redactor(["generated-secret-123"])
    assert "generated-secret-123" not in r.apply("auth ok with generated-secret-123 !")
    cleaned = redact_value(
        {
            "user": "a",
            "password": "x",
            "nested": {"api_key": "k", "note": "postgresql://u:pw123456@h/d"},
            "list": [{"Authorization": "t"}],
        }
    )
    assert (
        cleaned["password"] == "[REDACTED]"
        and cleaned["nested"]["api_key"] == "[REDACTED]"
        and "pw123456" not in cleaned["nested"]["note"]
    )
    assert cleaned["list"][0]["Authorization"] == "[REDACTED]" and cleaned["user"] == "a"


def test_redaction_does_not_destroy_ordinary_diagnostics():
    line = 'pg_restore: error: could not execute query: ERROR:  relation "public.orders" already exists'
    assert redact_text(line) == line


# ------------------------------------------------------------------------------ rbac ------------
def test_role_permissions_are_strictly_nested_and_owner_only_actions_are_owner_only():
    viewer, operator, admin, owner = (ROLE_PERMISSIONS[r] for r in (Role.VIEWER, Role.OPERATOR, Role.ADMIN, Role.OWNER))
    assert viewer < operator < admin < owner
    assert owner - admin == {Permission.MEMBERS_MANAGE, Permission.SECURITY_MANAGE}
    writes = {p for p in Permission if not p.value.endswith(".read")}
    assert not viewer & writes
    assert not role_allows(Role.OPERATOR, Permission.ARTIFACT_WRITE) and role_allows(
        Role.OPERATOR, Permission.RUN_START
    )
    assert not role_allows(Role.ADMIN, Permission.MEMBERS_MANAGE) and not role_allows(
        Role.VIEWER, Permission.AUDIT_READ
    )


# ---------------------------------------------------------------------------- passwords --------
def test_password_hashing_is_salted_argon2id_and_policy_is_enforced():
    h1, h2 = hash_password("correct horse battery"), hash_password("correct horse battery")
    assert (
        h1 != h2
        and h1.startswith("$argon2id$")
        and verify_password(h1, "correct horse battery")
        and not verify_password(h1, "wrong")
    )
    assert not verify_password("not-a-hash", "x")
    assert (
        password_problem("short", "a@b.c")
        and password_problem("a@example.test", "a@example.test")
        and password_problem("aaaaaaaaaaaaaaaa", "x@y.z")
    )
    assert password_problem("correct horse battery", "a@b.c") is None
