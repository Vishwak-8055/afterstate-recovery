"""Build the synthetic shop backups and print the path of the healthy one (used by the E2E suite and docs)."""
import sys
from pathlib import Path

from afterstate.engine.pgtools import locate_pg_tools
from afterstate.lab.fixtures import build_fixture_set

out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/afterstate-fixtures")
fx = build_fixture_set(locate_pg_tools(), out / "scratch", out / "dumps")
print(fx.path("good"))
