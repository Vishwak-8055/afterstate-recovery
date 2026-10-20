"""PRE-RELEASE ONLY: regenerate the single initial migration from the models, then add the explicit
enum CHECK constraints. After the first release, schema changes must be new, additive revisions."""

import os
import pathlib
import subprocess

from afterstate.devcluster import PlatformCluster

target = pathlib.Path("alembic/versions/0001_initial_schema.py")
target.unlink(missing_ok=True)
cluster = PlatformCluster()
try:
    env = dict(os.environ, AFTERSTATE_DATABASE_URL=cluster.start())
    subprocess.run(
        ["alembic", "revision", "--autogenerate", "-m", "initial schema", "--rev-id", "0001"],
        env=env,
        check=True,
        capture_output=True,
    )
finally:
    cluster.stop()
checks = subprocess.run(["python", "scripts/gen_enum_checks.py"], capture_output=True, text=True, check=True).stdout
s = target.read_text()
s = s.replace("depends_on = None\n", "depends_on = None\n\n" + checks + "\n", 1)
loop = """
    # Enum value integrity: one explicit, uniquely named CHECK per enum column.
    for table, column, values in _ENUM_CHECKS:
        allowed = ", ".join("'" + v + "'" for v in values)
        op.create_check_constraint(op.f(f"ck_{table}_{column}"), table, f"{column} IN ({allowed})")
"""
i = s.index("def downgrade()")
s = s[:i].rstrip() + "\n" + loop + "\n\n" + s[i:]
target.write_text(s)
print("regenerated", target)
