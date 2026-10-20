"""Independent verification of a restored database.

Every query runs as the validator role (not a superuser, read-only by role default) inside a
READ ONLY transaction with a statement timeout. Result *values* are never written to logs or
reports: only counts, hashes and booleans, because backups may contain personal data.
"""

from __future__ import annotations

import http.client
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import errors as pgerr
from psycopg import sql
from pydantic import ValidationError

from afterstate.domain.checks import CheckResult
from afterstate.domain.enums import Coverage, Outcome
from afterstate.domain.rules import (
    ColumnRule,
    ConstraintRule,
    DatasetChecksumRule,
    ExtensionRule,
    HttpCheckRule,
    IndexRule,
    RecoveryPointRule,
    RowCountRule,
    SchemaRule,
    SqlAssertionRule,
    TableRule,
    _Rule,
    parse_rule,
    rule_category,
)
from afterstate.engine.dataset import dataset_checksum
from afterstate.engine.environment import LocalClusterEnvironment
from afterstate.security.redact import Redactor

Done = Callable[..., CheckResult]


class EnvironmentLost(Exception):  # noqa: N818
    """The disposable database is gone; remaining checks cannot run."""


@dataclass
class ValidationState:
    latest_recoverable_at: datetime | None = None
    recovery_point_note: str | None = None


_ALIASES = {
    "int": "integer",
    "int4": "integer",
    "int8": "bigint",
    "int2": "smallint",
    "bool": "boolean",
    "varchar": "character varying",
    "char": "character",
    "bpchar": "character",
    "timestamptz": "timestamp with time zone",
    "timestamp": "timestamp without time zone",
    "float8": "double precision",
    "float4": "real",
    "decimal": "numeric",
    "serial": "integer",
    "bigserial": "bigint",
    "timetz": "time with time zone",
}


def normalize_type(text: str) -> tuple[str, str, str]:
    """Return (base, modifiers, array_suffix) in canonical PostgreSQL spelling."""
    t = re.sub(r"\s+", " ", text.strip().lower())
    m = re.match(r"^([a-z][a-z ]*?)\s*(\([^)]*\))?\s*((?:\[\])*)$", t)
    if not m:
        return t, "", ""
    base, mods, arr = m.group(1).strip(), (m.group(2) or "").replace(" ", ""), m.group(3) or ""
    return _ALIASES.get(base, base), mods, arr


def types_match(expected: str, actual: str) -> bool:
    eb, em, ea = normalize_type(expected)
    ab, am, aa = normalize_type(actual)
    return eb == ab and ea == aa and (not em or em == am)


class Validator:
    def __init__(
        self,
        env: LocalClusterEnvironment,
        *,
        budget_s: float,
        redactor: Redactor,
        cancel: threading.Event | None = None,
    ) -> None:
        self.env = env
        self.deadline = time.monotonic() + budget_s
        self.redactor = redactor
        self.cancel = cancel or threading.Event()
        self.state = ValidationState()
        self._conn: psycopg.Connection[tuple[Any, ...]] | None = None

    # ---- connection plumbing ---------------------------------------------------------------
    def _connection(self) -> psycopg.Connection[tuple[Any, ...]]:
        if self._conn is None or self._conn.closed:
            try:
                self._conn = self.env.validator_conn()
            except psycopg.Error as exc:
                raise EnvironmentLost(f"cannot connect to the restored database: {_first_line(exc)}") from exc
        return self._conn

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            try:
                self._conn.close()
            except psycopg.Error:
                pass
        self._conn = None

    @contextmanager
    def _tx(self, timeout_s: float) -> Iterator[psycopg.Connection[tuple[Any, ...]]]:
        conn = self._connection()
        remaining = max(1.0, min(float(timeout_s), self.deadline - time.monotonic()))
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SELECT set_config('statement_timeout', %s, true)", (f"{int(remaining * 1000)}ms",))
            conn.execute("SELECT set_config('lock_timeout', '5s', true)")
            yield conn

    def baseline_checks(self) -> list[CheckResult]:
        """Checks every run gets, independent of project rules: reachable and not empty."""
        out: list[CheckResult] = []
        t = time.perf_counter()
        with self._tx(15) as c:
            c.execute("SELECT 1")
        out.append(
            CheckResult(
                "db.connectivity",
                "Restored database accepts connections",
                "structure",
                Outcome.PASS,
                True,
                "Connected as the read-only validator role.",
                duration_ms=int((time.perf_counter() - t) * 1000),
            )
        )
        t = time.perf_counter()
        with self._tx(30) as c:
            row = c.execute(
                "SELECT count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg\\_toast%'"
            ).fetchone()
        tables = int(row[0]) if row else 0
        out.append(
            CheckResult(
                "db.non_empty",
                "Restored database contains user tables",
                "structure",
                Outcome.PASS if tables else Outcome.FAIL,
                True,
                f"{tables} user table(s) present."
                if tables
                else "The restore produced a database with no user tables.",
                expected=">= 1 table",
                actual={"tables": tables},
                coverage=Coverage.COMPLETE,
                duration_ms=int((time.perf_counter() - t) * 1000),
            )
        )
        return out

    # ---- dispatch --------------------------------------------------------------------------
    def evaluate(
        self, rule_id: str | None, position: int, name: str, kind: str, config: dict[str, Any], required: bool
    ) -> CheckResult:
        started = time.perf_counter()
        category = rule_category(kind)
        key = f"{category}.{kind}"
        display = name

        def done(
            outcome: Outcome, message: str, expected: Any = None, actual: Any = None, coverage: Coverage | None = None
        ) -> CheckResult:
            return CheckResult(
                key=key,
                name=display,
                category=category,
                outcome=outcome,
                required=required,
                message=message,
                expected=expected,
                actual=actual,
                coverage=coverage,
                duration_ms=int((time.perf_counter() - started) * 1000),
                rule_id=rule_id,
            )

        if self.cancel.is_set():
            return done(Outcome.SKIPPED, "Run was cancelled before this check executed.")
        if time.monotonic() >= self.deadline:
            return done(Outcome.INCONCLUSIVE, "The validation time budget was exhausted before this check ran.")
        try:
            rule = parse_rule(config)
        except ValidationError as exc:
            return done(Outcome.INCONCLUSIVE, f"The rule definition is invalid: {_first_line(exc)}")
        display = name or rule.display_name()
        try:
            return self._dispatch(rule, done)
        except pgerr.QueryCanceled:
            return done(Outcome.INCONCLUSIVE, "The query exceeded its time limit, so the result is unknown.")
        except pgerr.LockNotAvailable:
            return done(Outcome.INCONCLUSIVE, "The query could not obtain a lock in time.")
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            if self._conn is None or self._conn.closed or not self.env.postmaster_alive():
                raise EnvironmentLost(f"the restored database became unavailable: {_first_line(exc)}") from exc
            return done(Outcome.INCONCLUSIVE, f"Database error: {_first_line(exc)}")
        except psycopg.Error as exc:
            state = getattr(exc, "sqlstate", None) or "?"
            if isinstance(rule, RowCountRule) and isinstance(exc, pgerr.UndefinedTable):
                return done(Outcome.FAIL, f"Table {rule.schema_}.{rule.table} does not exist.", actual="missing")
            return done(Outcome.INCONCLUSIVE, f"The query failed (SQLSTATE {state}): {_first_line(exc)}")

    def _dispatch(self, rule: _Rule, done: Done) -> CheckResult:
        match rule:
            case SchemaRule():
                return self._schema(rule, done)
            case TableRule():
                return self._table(rule, done)
            case ColumnRule():
                return self._column(rule, done)
            case ConstraintRule():
                return self._constraint(rule, done)
            case IndexRule():
                return self._index(rule, done)
            case ExtensionRule():
                return self._extension(rule, done)
            case RowCountRule():
                return self._row_count(rule, done)
            case SqlAssertionRule():
                return self._sql_assertion(rule, done)
            case DatasetChecksumRule():
                return self._dataset_checksum(rule, done)
            case RecoveryPointRule():
                return self._recovery_point(rule, done)
            case _:
                return done(Outcome.SKIPPED, "This rule type is evaluated by another stage.")

    # ---- structural ------------------------------------------------------------------------
    def _schema(self, r: SchemaRule, done: Done) -> CheckResult:
        with self._tx(30) as c:
            found = c.execute("SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = %s", (r.schema_,)).fetchone()
            if found:
                return done(Outcome.PASS, f"Schema '{r.schema_}' exists.", coverage=Coverage.COMPLETE)
            actual = [
                x[0]
                for x in c.execute(
                    "SELECT nspname FROM pg_catalog.pg_namespace WHERE nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema' ORDER BY 1 LIMIT 50"
                ).fetchall()
            ]
        return done(
            Outcome.FAIL,
            f"Schema '{r.schema_}' is missing from the restored database.",
            expected=r.schema_,
            actual={"schemas_present": actual},
            coverage=Coverage.COMPLETE,
        )

    def _table(self, r: TableRule, done: Done) -> CheckResult:
        kinds = {"table": ("r", "p"), "view": ("v",), "materialized_view": ("m",)}[r.relation]
        with self._tx(30) as c:
            row = c.execute(
                "SELECT c.relkind FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relname = %s",
                (r.schema_, r.table),
            ).fetchone()
        if row and row[0] in kinds:
            return done(
                Outcome.PASS,
                f"{r.relation.replace('_', ' ').capitalize()} {r.schema_}.{r.table} exists.",
                coverage=Coverage.COMPLETE,
            )
        if row:
            return done(
                Outcome.FAIL,
                f"{r.schema_}.{r.table} exists but is not a {r.relation.replace('_', ' ')}.",
                expected=r.relation,
                actual={"relkind": row[0]},
                coverage=Coverage.COMPLETE,
            )
        return done(
            Outcome.FAIL,
            f"{r.relation.replace('_', ' ').capitalize()} {r.schema_}.{r.table} is missing.",
            expected=f"{r.schema_}.{r.table}",
            actual="missing",
            coverage=Coverage.COMPLETE,
        )

    def _column(self, r: ColumnRule, done: Done) -> CheckResult:
        with self._tx(30) as c:
            cols = c.execute(
                "SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod), a.attnotnull "
                "FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class t ON t.oid = a.attrelid "
                "JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = %s AND t.relname = %s AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum",
                (r.schema_, r.table),
            ).fetchall()
        if not cols:
            return done(
                Outcome.FAIL,
                f"Table {r.schema_}.{r.table} has no columns or does not exist.",
                actual="missing",
                coverage=Coverage.COMPLETE,
            )
        match = next((x for x in cols if x[0] == r.column), None)
        if match is None:
            return done(
                Outcome.FAIL,
                f"Column '{r.column}' is missing from {r.schema_}.{r.table}.",
                expected=r.column,
                actual={"columns": [x[0] for x in cols][:60]},
                coverage=Coverage.COMPLETE,
            )
        problems = []
        if r.data_type and not types_match(r.data_type, match[1]):
            problems.append(f"type is {match[1]}, expected {r.data_type}")
        if r.nullable is not None and (not match[2]) != r.nullable:
            problems.append(
                f"column is {'nullable' if not match[2] else 'NOT NULL'}, expected {'nullable' if r.nullable else 'NOT NULL'}"
            )
        actual = {"type": match[1], "nullable": not match[2]}
        if problems:
            return done(
                Outcome.FAIL,
                f"Column {r.table}.{r.column}: " + "; ".join(problems) + ".",
                expected={"type": r.data_type, "nullable": r.nullable},
                actual=actual,
                coverage=Coverage.COMPLETE,
            )
        return done(
            Outcome.PASS,
            f"Column {r.table}.{r.column} is {match[1]}.",
            expected={"type": r.data_type, "nullable": r.nullable},
            actual=actual,
            coverage=Coverage.COMPLETE,
        )

    def _constraint(self, r: ConstraintRule, done: Done) -> CheckResult:
        contype = {"primary_key": "p", "unique": "u", "foreign_key": "f", "check": "c"}[r.constraint_type]
        with self._tx(30) as c:
            rows = c.execute(
                "SELECT con.conname, con.contype, "
                "  coalesce((SELECT array_agg(att.attname ORDER BY k.ord) FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) "
                "            JOIN pg_catalog.pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.attnum), ARRAY[]::name[]), "
                "  (SELECT rn.nspname || '.' || rc.relname FROM pg_catalog.pg_class rc JOIN pg_catalog.pg_namespace rn ON rn.oid = rc.relnamespace WHERE rc.oid = con.confrelid) "
                "FROM pg_catalog.pg_constraint con JOIN pg_catalog.pg_class t ON t.oid = con.conrelid "
                "JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = %s AND t.relname = %s",
                (r.schema_, r.table),
            ).fetchall()
        present = [{"name": x[0], "type": x[1], "columns": list(x[2]), "references": x[3]} for x in rows]
        candidates = [x for x in present if x["type"] == contype]
        if r.constraint_name:
            candidates = [x for x in candidates if x["name"] == r.constraint_name]
        if r.columns:
            candidates = [x for x in candidates if x["columns"] == r.columns]
        if r.references:
            want = r.references if "." in r.references else f"public.{r.references}"
            candidates = [x for x in candidates if x["references"] == want]
        if candidates:
            return done(
                Outcome.PASS,
                f"{r.constraint_type.replace('_', ' ').capitalize()} constraint present on {r.schema_}.{r.table}.",
                actual=candidates[0],
                coverage=Coverage.COMPLETE,
            )
        return done(
            Outcome.FAIL,
            f"No matching {r.constraint_type.replace('_', ' ')} constraint on {r.schema_}.{r.table}.",
            expected={"name": r.constraint_name, "columns": r.columns, "references": r.references},
            actual={"constraints_present": [x for x in present if x["type"] == contype][:30]},
            coverage=Coverage.COMPLETE,
        )

    def _index(self, r: IndexRule, done: Done) -> CheckResult:
        with self._tx(30) as c:
            rows = c.execute(
                "SELECT ic.relname, i.indisunique, i.indisvalid, "
                "  ARRAY(SELECT a.attname FROM unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) "
                "        JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum ORDER BY k.ord) "
                "FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid "
                "JOIN pg_catalog.pg_class t ON t.oid = i.indrelid JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = %s AND t.relname = %s",
                (r.schema_, r.table),
            ).fetchall()
        present = [{"name": x[0], "unique": x[1], "valid": x[2], "columns": list(x[3])} for x in rows]
        candidates = [x for x in present if x["valid"]]
        if r.index_name:
            candidates = [x for x in candidates if x["name"] == r.index_name]
        if r.columns:
            candidates = [x for x in candidates if x["columns"][: len(r.columns)] == r.columns]
        if r.unique is not None:
            candidates = [x for x in candidates if x["unique"] == r.unique]
        if candidates:
            return done(
                Outcome.PASS,
                f"Valid index present on {r.schema_}.{r.table}.",
                actual=candidates[0],
                coverage=Coverage.COMPLETE,
            )
        return done(
            Outcome.FAIL,
            f"No matching valid index on {r.schema_}.{r.table}.",
            expected={"name": r.index_name, "columns": r.columns, "unique": r.unique},
            actual={"indexes_present": present[:30]},
            coverage=Coverage.COMPLETE,
        )

    def _extension(self, r: ExtensionRule, done: Done) -> CheckResult:
        with self._tx(30) as c:
            row = c.execute(
                "SELECT extversion FROM pg_catalog.pg_extension WHERE extname = %s", (r.extension,)
            ).fetchone()
        if row:
            return done(
                Outcome.PASS,
                f"Extension {r.extension} {row[0]} is installed.",
                actual=row[0],
                coverage=Coverage.COMPLETE,
            )
        return done(
            Outcome.FAIL,
            f"Extension {r.extension} is not installed.",
            expected=r.extension,
            actual="missing",
            coverage=Coverage.COMPLETE,
        )

    # ---- data ------------------------------------------------------------------------------
    def _row_count(self, r: RowCountRule, done: Done) -> CheckResult:
        query = sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(r.schema_), sql.Identifier(r.table))
        with self._tx(r.timeout_seconds) as c:
            row = c.execute(query).fetchone()
        n = int(row[0]) if row else -1
        expected = {"exact": r.exact, "minimum": r.minimum, "maximum": r.maximum}
        ok = (
            (n == r.exact)
            if r.exact is not None
            else ((r.minimum is None or n >= r.minimum) and (r.maximum is None or n <= r.maximum))
        )
        msg = f"{r.schema_}.{r.table} has {n} rows."
        return done(
            Outcome.PASS if ok else Outcome.FAIL,
            msg if ok else msg + " This is outside the expected range.",
            expected,
            {"rows": n},
            Coverage.PARTIAL,
        )

    def _sql_assertion(self, r: SqlAssertionRule, done: Done) -> CheckResult:
        with self._tx(r.timeout_seconds) as c, c.cursor() as cur:
            cur.execute(sql.SQL(r.sql))  # validated read-only text, run read-only as a non-superuser
            cols = [d.name for d in (cur.description or [])]
            rows = cur.fetchmany(1001)
        if r.expect == "no_rows":
            if not rows:
                return done(
                    Outcome.PASS,
                    "The query returned no rows (no violations).",
                    "no rows",
                    {"rows": 0},
                    Coverage.PARTIAL,
                )
            shown = f"{len(rows)}+" if len(rows) > 1000 else str(len(rows))
            return done(
                Outcome.FAIL,
                f"The query returned {shown} row(s); none were expected.",
                "no rows",
                {"rows": len(rows), "columns": cols},
                Coverage.PARTIAL,
            )
        if len(rows) != 1 or len(rows[0]) != 1:
            return done(
                Outcome.INCONCLUSIVE,
                f"The assertion must return exactly one value; it returned {len(rows)} row(s) and {len(cols)} column(s).",
                actual={"rows": len(rows), "columns": cols},
            )
        value = rows[0][0]
        if r.expect == "true":
            if not isinstance(value, bool):
                return done(
                    Outcome.INCONCLUSIVE,
                    "The assertion must return a boolean (true means the invariant holds).",
                    actual={"type": type(value).__name__},
                )
            return done(
                Outcome.PASS if value else Outcome.FAIL,
                "The invariant holds." if value else "The invariant does not hold.",
                True,
                value,
                Coverage.PARTIAL,
            )
        if isinstance(value, bool) or not isinstance(value, int | float | Decimal | str):
            return done(
                Outcome.INCONCLUSIVE,
                "The assertion returned a value that cannot be compared.",
                actual={"type": type(value).__name__},
            )
        if r.expect == "equals":
            ok = (
                (str(value) == str(r.value))
                if isinstance(value, str)
                else (Decimal(str(value)) == Decimal(str(r.value)))
            )
            shown_value: Any = {"length": len(value)} if isinstance(value, str) else float(value)
            return done(
                Outcome.PASS if ok else Outcome.FAIL,
                "The value matches." if ok else "The value does not match the expectation.",
                r.value if not isinstance(r.value, str) else {"length": len(r.value)},
                shown_value,
                Coverage.PARTIAL,
            )
        if isinstance(value, str):
            return done(Outcome.INCONCLUSIVE, "A 'between' assertion needs a numeric result.")
        num = float(value)
        ok = r.minimum <= num <= r.maximum  # type: ignore[operator]
        return done(
            Outcome.PASS if ok else Outcome.FAIL,
            f"The value {num:g} is {'within' if ok else 'outside'} [{r.minimum:g}, {r.maximum:g}].",
            [r.minimum, r.maximum],
            num,
            Coverage.PARTIAL,
        )

    def _dataset_checksum(self, r: DatasetChecksumRule, done: Done) -> CheckResult:
        with self._tx(r.timeout_seconds) as c:
            digest, rows = dataset_checksum(c, r.sql)
        actual = {"sha256": digest, "rows": rows}
        expected = {"sha256": r.expected_sha256.lower(), "rows": r.expected_rows}
        ok = digest == expected["sha256"] and (r.expected_rows is None or rows == r.expected_rows)
        return done(
            Outcome.PASS if ok else Outcome.FAIL,
            f"All {rows} rows of the reference dataset match byte for byte."
            if ok
            else "The reference dataset differs from the recorded checksum.",
            expected,
            actual,
            Coverage.COMPLETE,
        )

    def _recovery_point(self, r: RecoveryPointRule, done: Done) -> CheckResult:
        with self._tx(r.timeout_seconds) as c:
            row = c.execute(sql.SQL(r.sql)).fetchone()
        value = row[0] if row else None
        if not isinstance(value, datetime):
            return done(
                Outcome.INCONCLUSIVE,
                "The probe must return one timestamp; no recovery point could be determined.",
                actual={"type": type(value).__name__},
            )
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        self.state.latest_recoverable_at = value
        return done(
            Outcome.PASS,
            f"Newest recovered record is dated {value.isoformat()}.",
            actual={"latest_recoverable_at": value.isoformat()},
            coverage=Coverage.PARTIAL,
        )


def evaluate_http(
    rule: HttpCheckRule,
    *,
    host: str,
    port: int,
    rule_id: str | None,
    name: str,
    required: bool,
    timeout_s: float = 10.0,
) -> CheckResult:
    started = time.perf_counter()
    fields: dict[str, Any] = {
        "key": "application.http_check",
        "name": name or rule.display_name(),
        "category": "application",
        "required": required,
        "rule_id": rule_id,
    }
    expected = {
        "status": rule.expected_status,
        "max_latency_ms": rule.max_latency_ms,
        "body_contains": bool(rule.body_contains),
    }
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
        t0 = time.perf_counter()
        conn.request("GET", rule.path, headers={"Accept": "application/json", "User-Agent": "afterstate-smoke/1"})
        resp = conn.getresponse()
        body = resp.read(65536)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        conn.close()
    except (OSError, http.client.HTTPException) as exc:
        return CheckResult(
            outcome=Outcome.FAIL,
            message=f"The application did not respond: {type(exc).__name__}",
            expected=expected,
            actual={"error": type(exc).__name__},
            duration_ms=int((time.perf_counter() - started) * 1000),
            **fields,
        )
    problems = []
    if resp.status != rule.expected_status:
        problems.append(f"status {resp.status}, expected {rule.expected_status}")
    if latency_ms > rule.max_latency_ms:
        problems.append(f"latency {latency_ms} ms exceeds {rule.max_latency_ms} ms")
    if rule.body_contains and rule.body_contains.encode() not in body:
        problems.append("response body did not contain the expected text")
    actual = {"status": resp.status, "latency_ms": latency_ms, "bytes": len(body)}
    return CheckResult(
        outcome=Outcome.FAIL if problems else Outcome.PASS,
        message=("; ".join(problems) + ".") if problems else f"HTTP {resp.status} in {latency_ms} ms.",
        expected=expected,
        actual=actual,
        duration_ms=int((time.perf_counter() - started) * 1000),
        **fields,
    )


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return Redactor().apply(text)[:300]
