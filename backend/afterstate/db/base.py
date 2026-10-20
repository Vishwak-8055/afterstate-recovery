from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sqlalchemy import DateTime, MetaData
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map: dict[Any, Any] = {datetime: DateTime(timezone=True)}


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> uuid.UUID:
    return uuid.uuid4()


def enum_type(enum_cls: type[Enum], length: int = 32) -> SAEnum:
    """Store enums as validated VARCHAR (portable migrations, no PG enum type to alter).

    SQLAlchemy's implicit CHECK constraints are disabled on purpose: they cannot be given
    per-column names and autogenerate duplicates them. The migration creates one explicit,
    uniquely named CHECK per enum column instead (verified by tests/test_migrations.py)."""
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=length,
        validate_strings=True,
        create_constraint=False,
        values_callable=lambda e: [m.value for m in e],
    )
