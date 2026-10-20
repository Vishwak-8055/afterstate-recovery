"""Migration validation used by CI and `make migrations-check`.

1. upgrade an empty database to head;  2. fail if the models drifted from the migrations
(alembic check);  3. downgrade to base and upgrade again (reversibility);
4. confirm every enum column is protected by a CHECK constraint.
"""

import os
import subprocess
import sys

import sqlalchemy as sa
from afterstate.db import models  # noqa: F401
from afterstate.db.base import Base


def alembic(*args: str, env: dict[str, str]) -> None:
    r = subprocess.run(["alembic", *args], env=env, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        raise SystemExit(f"alembic {' '.join(args)} failed")
    print(f"ok: alembic {' '.join(args)}")


def main(url: str) -> None:
    env = dict(os.environ, AFTERSTATE_DATABASE_URL=url)
    alembic("upgrade", "head", env=env)
    alembic("check", env=env)
    alembic("downgrade", "base", env=env)
    alembic("upgrade", "head", env=env)
    engine = sa.create_engine(url)
    with engine.connect() as c:
        present = {r[0] for r in c.execute(sa.text("SELECT conname FROM pg_constraint WHERE contype = 'c'"))}
        tables = {r[0] for r in c.execute(sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}
    expected_tables = set(Base.metadata.tables) | {"alembic_version"}
    missing_tables = expected_tables - tables
    missing = [
        f"ck_{t.name}_{col.name}"
        for t in Base.metadata.tables.values()
        for col in t.columns
        if isinstance(col.type, sa.Enum) and f"ck_{t.name}_{col.name}" not in present
    ]
    if missing_tables or missing:
        raise SystemExit(f"schema incomplete: missing tables={sorted(missing_tables)} missing enum checks={missing}")
    print(f"ok: {len(Base.metadata.tables)} tables, enum CHECK constraints present")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        from afterstate.devcluster import PlatformCluster

        cluster = PlatformCluster()
        try:
            main(cluster.start())
        finally:
            cluster.stop()
