"""Deterministic synthetic PostgreSQL backups for the Recovery Lab, demos and tests.

All fixtures are produced by real pg_dump runs against a real, throwaway source cluster, so the
restore engine is always exercised with genuine archives. Variants are real databases (or real
byte-level damage), never flags that make the engine pretend.
"""

from __future__ import annotations

import fcntl
import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from afterstate.engine.dataset import dataset_checksum
from afterstate.engine.environment import ADMIN_USER, LocalClusterEnvironment
from afterstate.engine.pgtools import PgTools
from afterstate.engine.process import base_env, run_command
from afterstate.security.redact import Redactor

FIXTURE_VERSION = 3

SHOP_DDL = """
CREATE EXTENSION pgcrypto;
CREATE SCHEMA billing;
CREATE TABLE customers (
  id bigserial PRIMARY KEY,
  email text NOT NULL UNIQUE,
  full_name text NOT NULL,
  tier text NOT NULL DEFAULT 'standard',
  created_at timestamptz NOT NULL
);
CREATE TABLE products (
  id serial PRIMARY KEY,
  sku text NOT NULL UNIQUE,
  name text NOT NULL,
  price_cents integer NOT NULL CHECK (price_cents >= 0)
);
CREATE TABLE orders (
  id bigserial PRIMARY KEY,
  customer_id bigint NOT NULL REFERENCES customers(id),
  status text NOT NULL CHECK (status IN ('pending','paid','shipped','refunded')),
  total_cents bigint NOT NULL,
  placed_at timestamptz NOT NULL
);
CREATE TABLE order_items (
  id bigserial PRIMARY KEY,
  order_id bigint NOT NULL REFERENCES orders(id),
  product_id integer NOT NULL REFERENCES products(id),
  quantity integer NOT NULL CHECK (quantity > 0),
  unit_price_cents integer NOT NULL
);
CREATE TABLE billing.ledger_entries (
  id bigserial PRIMARY KEY,
  order_id bigint REFERENCES orders(id),
  kind text NOT NULL CHECK (kind IN ('charge','refund')),
  amount_cents bigint NOT NULL,
  posted_at timestamptz NOT NULL
);
CREATE INDEX orders_customer_idx ON orders (customer_id);
CREATE INDEX orders_placed_at_idx ON orders (placed_at);
"""

SHOP_DATA = """
INSERT INTO customers (email, full_name, tier, created_at)
SELECT 'customer' || g || '@example.test', 'Customer ' || g,
       (ARRAY['standard','plus','pro'])[1 + g % 3], '@BASE@'::timestamptz - (g * interval '3 hours')
FROM generate_series(1, @CUSTOMERS@) g;
INSERT INTO products (sku, name, price_cents)
SELECT 'SKU-' || lpad(g::text, 4, '0'), 'Product ' || g, 500 + (g * 137) % 9000 FROM generate_series(1, 40) g;
INSERT INTO orders (customer_id, status, total_cents, placed_at)
SELECT 1 + (g * 7) % @CUSTOMERS@, (ARRAY['paid','shipped','paid','pending','refunded'])[1 + g % 5], 0,
       '@BASE@'::timestamptz - (g * interval '7 minutes')
FROM generate_series(1, @ORDERS@) g;
INSERT INTO order_items (order_id, product_id, quantity, unit_price_cents)
SELECT o.id, 1 + (o.id * k) % 40, 1 + (o.id + k) % 3, p.price_cents
FROM orders o CROSS JOIN generate_series(1, 3) k JOIN products p ON p.id = 1 + (o.id * k) % 40
WHERE (o.id + k) % 4 <> 0;
UPDATE orders o SET total_cents = (SELECT sum(quantity * unit_price_cents) FROM order_items i WHERE i.order_id = o.id);
INSERT INTO billing.ledger_entries (order_id, kind, amount_cents, posted_at)
SELECT id, 'charge', total_cents, placed_at FROM orders WHERE status IN ('paid','shipped','refunded');
INSERT INTO billing.ledger_entries (order_id, kind, amount_cents, posted_at)
SELECT id, 'refund', total_cents, placed_at + interval '1 day' FROM orders WHERE status = 'refunded';
COMMENT ON TABLE customers IS 'Customer master records (synthetic)';
"""

SLOW_DDL = """
CREATE TABLE events (id integer PRIMARY KEY, payload text NOT NULL);
INSERT INTO events SELECT g, 'event-' || g FROM generate_series(1, 4000) g;
-- Slow by construction: only when run by the restore role, so building the source stays fast.
CREATE FUNCTION slow_key(p text) RETURNS text LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
  IF current_user = 'afterstate_restore' THEN PERFORM pg_sleep(0.01); END IF;
  RETURN p;
END $$;
CREATE INDEX events_slow_idx ON events (slow_key(payload));
"""

#: Queries and expectations used by the lab scenarios and tests, computed from the good source.
CUSTOMERS_QUERY = "SELECT id, email, tier FROM customers ORDER BY id"
INVARIANT_ORDER_TOTALS = (
    "SELECT NOT EXISTS (SELECT 1 FROM orders o WHERE o.total_cents <> "
    "(SELECT coalesce(sum(quantity * unit_price_cents), 0) FROM order_items i WHERE i.order_id = o.id))"
)
INVARIANT_LEDGER_BALANCE = (
    "SELECT (SELECT coalesce(sum(CASE WHEN kind = 'charge' THEN amount_cents ELSE -amount_cents END), 0) "
    "FROM billing.ledger_entries) = (SELECT coalesce(sum(total_cents), 0) FROM orders WHERE status IN ('paid','shipped'))"
)
RECOVERY_POINT_QUERY = "SELECT max(placed_at) FROM orders"


@dataclass
class FixtureSet:
    root: Path
    dumps: dict[str, Path]
    meta: dict[str, object]

    def path(self, name: str) -> Path:
        return self.dumps[name]


def _pg_dump(tools: PgTools, env: LocalClusterEnvironment, database: str, out: Path, fmt: str) -> None:
    res = run_command(
        [str(tools.pg_dump), "-h", str(env.sockdir), "-U", ADMIN_USER, f"-F{fmt}", "-f", str(out), database],
        env=base_env(),
        timeout_s=120,
    )
    if not res.ok:
        raise RuntimeError(f"pg_dump of {database} failed: {res.stderr_tail[-300:]}")


def build_fixture_set(
    tools: PgTools, scratch_root: Path, out_dir: Path, *, customers: int = 2000, orders: int = 6000
) -> FixtureSet:
    """Build (or reuse) the fixture dumps. Safe to call concurrently: a file lock serialises builders."""
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = out_dir / "manifest.json"
    with (out_dir / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if manifest.exists():
            data = json.loads(manifest.read_text())
            if data.get("fixture_version") == FIXTURE_VERSION and all(
                (out_dir / n).exists() for n in data["files"].values()
            ):
                return FixtureSet(out_dir, {k: out_dir / v for k, v in data["files"].items()}, data["meta"])

        env = LocalClusterEnvironment(scratch_root, uuid.uuid4(), tools, redactor=Redactor())
        try:
            env.create()
            env.start()
            base = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
            with env.admin_conn("postgres") as c:
                c.execute("CREATE DATABASE shop")
            with env.admin_conn("shop") as c:
                c.execute(SHOP_DDL)
                c.execute(
                    SHOP_DATA.replace("@BASE@", base.isoformat())
                    .replace("@CUSTOMERS@", str(int(customers)))
                    .replace("@ORDERS@", str(int(orders)))
                )
                c.execute("ANALYZE")
                cust_sha, cust_rows = dataset_checksum(c, CUSTOMERS_QUERY)
                counts = {
                    t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # type: ignore[index]  # noqa: S608
                    for t in ("customers", "products", "orders", "order_items", "billing.ledger_entries")
                }
                latest_row = c.execute(RECOVERY_POINT_QUERY).fetchone()
                assert latest_row is not None
                latest = cast(datetime, latest_row[0])
            with env.admin_conn("postgres") as c:
                c.execute("CREATE DATABASE shop_nobilling TEMPLATE shop")
                c.execute("CREATE DATABASE shop_broken TEMPLATE shop")
                c.execute("CREATE DATABASE shop_slow")
            with env.admin_conn("shop_nobilling") as c:
                c.execute("DROP SCHEMA billing CASCADE")
            with env.admin_conn("shop_broken") as c:
                c.execute("UPDATE orders SET total_cents = total_cents + 500 WHERE id % 997 = 0")
                c.execute("DELETE FROM billing.ledger_entries WHERE id % 1500 = 0")
            with env.admin_conn("shop_slow") as c:
                c.execute(SLOW_DDL)

            files = {
                "good": "shop-good.dump",
                "no_billing": "shop-no-billing.dump",
                "broken_invariant": "shop-broken-invariant.dump",
                "slow": "shop-slow.dump",
                "plain": "shop-plain.sql",
            }
            _pg_dump(tools, env, "shop", out_dir / files["good"], "c")
            _pg_dump(tools, env, "shop_nobilling", out_dir / files["no_billing"], "c")
            _pg_dump(tools, env, "shop_broken", out_dir / files["broken_invariant"], "c")
            _pg_dump(tools, env, "shop_slow", out_dir / files["slow"], "c")
            _pg_dump(tools, env, "shop", out_dir / files["plain"], "p")

            good = (out_dir / files["good"]).read_bytes()
            (out_dir / "shop-truncated.dump").write_bytes(good[: len(good) // 2])
            flipped = bytearray(good)
            for i in range(len(flipped) - 400, len(flipped) - 340):
                flipped[i] ^= 0xFF
            (out_dir / "shop-corrupt.dump").write_bytes(bytes(flipped))
            files["truncated"] = "shop-truncated.dump"
            files["corrupt"] = "shop-corrupt.dump"

            meta: dict[str, object] = {
                "counts": counts,
                "customers_sha256": cust_sha,
                "customers_rows": cust_rows,
                "latest_placed_at": latest.isoformat(),
                "built_at": datetime.now(UTC).isoformat(),
                "pg_version": tools.version,
            }
            manifest.write_text(
                json.dumps({"fixture_version": FIXTURE_VERSION, "files": files, "meta": meta}, indent=2)
            )
            return FixtureSet(out_dir, {k: out_dir / v for k, v in files.items()}, meta)
        except BaseException:
            shutil.rmtree(out_dir / "manifest.json", ignore_errors=True)
            raise
        finally:
            env.destroy()


def shop_rules(meta: dict[str, object]) -> list[dict[str, object]]:
    """The reference rule set for the shop fixture: structure, constraints, counts, invariants, a
    complete dataset comparison and a recovery-point probe."""
    counts: dict[str, int] = meta["counts"]  # type: ignore[assignment]
    return [
        {"kind": "schema", "schema": "billing", "name": "Billing schema exists"},
        {"kind": "table", "schema": "public", "table": "customers"},
        {"kind": "table", "schema": "public", "table": "orders"},
        {"kind": "table", "schema": "billing", "table": "ledger_entries"},
        {"kind": "column", "table": "orders", "column": "total_cents", "data_type": "bigint", "nullable": False},
        {"kind": "column", "table": "customers", "column": "email", "data_type": "text", "nullable": False},
        {"kind": "constraint", "table": "orders", "constraint_type": "primary_key", "columns": ["id"]},
        {
            "kind": "constraint",
            "table": "orders",
            "constraint_type": "foreign_key",
            "columns": ["customer_id"],
            "references": "public.customers",
        },
        {"kind": "constraint", "table": "customers", "constraint_type": "unique", "columns": ["email"]},
        {"kind": "index", "table": "orders", "index_name": "orders_customer_idx"},
        {"kind": "extension", "extension": "pgcrypto"},
        {"kind": "row_count", "table": "customers", "exact": counts["customers"]},
        {"kind": "row_count", "table": "orders", "minimum": counts["orders"]},
        {
            "kind": "sql_assertion",
            "name": "Order totals equal the sum of their items",
            "sql": INVARIANT_ORDER_TOTALS,
            "expect": "true",
        },
        {
            "kind": "sql_assertion",
            "name": "Ledger balance equals paid and shipped orders",
            "sql": INVARIANT_LEDGER_BALANCE,
            "expect": "true",
        },
        {
            "kind": "dataset_checksum",
            "name": "Customer reference dataset",
            "sql": CUSTOMERS_QUERY,
            "expected_sha256": meta["customers_sha256"],
            "expected_rows": meta["customers_rows"],
        },
        {"kind": "recovery_point", "name": "Newest order timestamp", "sql": RECOVERY_POINT_QUERY},
    ]
