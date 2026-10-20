"""Print the (table, column, allowed values) list used by the migration's enum CHECK constraints."""

import sqlalchemy as sa
from afterstate.db import models  # noqa: F401
from afterstate.db.base import Base

rows = []
for table in sorted(Base.metadata.tables.values(), key=lambda t: t.name):
    for col in table.columns:
        if isinstance(col.type, sa.Enum):
            rows.append((table.name, col.name, list(col.type.enums)))
print("_ENUM_CHECKS: list[tuple[str, str, list[str]]] = [")
for t, c, v in rows:
    print(f"    ({t!r}, {c!r}, {v!r}),")
print("]")
