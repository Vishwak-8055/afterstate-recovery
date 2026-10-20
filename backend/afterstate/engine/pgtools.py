"""Locate PostgreSQL client/server binaries and record their versions as evidence."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


class ToolNotFound(RuntimeError):
    pass


@dataclass(frozen=True)
class PgTools:
    bin_dir: Path
    pg_restore: Path
    pg_dump: Path
    postgres: Path
    initdb: Path
    psql: Path
    version: str  # e.g. "16.15"
    major: int

    def versions(self) -> dict[str, str]:
        return {"postgresql": self.version, "pg_restore": _version_of(self.pg_restore), "bin_dir": str(self.bin_dir)}


def _version_of(binary: Path) -> str:
    out = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=10, check=False)
    return out.stdout.strip()


def _candidates(explicit: Path | None) -> list[Path]:
    found: list[Path] = []
    if explicit:
        found.append(explicit)
    on_path = shutil.which("pg_restore")
    if on_path:
        found.append(Path(on_path).resolve().parent)
    base = Path("/usr/lib/postgresql")
    if base.is_dir():
        found += sorted(
            (p / "bin" for p in base.iterdir() if p.name.isdigit()), key=lambda p: int(p.parent.name), reverse=True
        )
    return found


@lru_cache
def locate_pg_tools(explicit: Path | None = None) -> PgTools:
    for bin_dir in _candidates(explicit):
        paths = {n: bin_dir / n for n in ("pg_restore", "pg_dump", "postgres", "initdb", "psql")}
        if all(p.is_file() for p in paths.values()):
            text = _version_of(paths["postgres"])
            m = re.search(r"\(PostgreSQL\)\s+(\d+)(?:\.(\d+))?", text)
            if not m:
                continue
            major = int(m.group(1))
            version = f"{major}.{m.group(2)}" if m.group(2) else str(major)
            return PgTools(
                bin_dir,
                paths["pg_restore"],
                paths["pg_dump"],
                paths["postgres"],
                paths["initdb"],
                paths["psql"],
                version,
                major,
            )
    raise ToolNotFound(
        "PostgreSQL server and client binaries (pg_restore, pg_dump, postgres, initdb, psql) were not found. "
        "Install PostgreSQL or set AFTERSTATE_PG_BIN_DIR."
    )
