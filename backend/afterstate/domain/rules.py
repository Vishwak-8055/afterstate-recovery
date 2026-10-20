"""Validation rule definitions. One discriminated union, validated on save and on execution."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from afterstate.domain.enums import (
    APPLICATION_KINDS,
    DATA_KINDS,
    STRUCTURAL_KINDS,
    RuleKind,
)
from afterstate.domain.sqlsafety import UnsafeSql, validate_readonly_sql

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=63, pattern=r"^[^\x00-\x1f]+$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{64}$")]
Label = Annotated[str, StringConstraints(max_length=200)]


class _Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kind: str
    name: Label | None = None
    required: bool = True

    def label(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def display_name(self) -> str:
        return self.name or self.label()


class SchemaRule(_Rule):
    kind: Literal["schema"] = "schema"
    schema_: Identifier = Field(alias="schema")

    def label(self) -> str:
        return f"Schema {self.schema_} exists"


class TableRule(_Rule):
    kind: Literal["table"] = "table"
    schema_: Identifier = Field(default="public", alias="schema")
    table: Identifier
    relation: Literal["table", "view", "materialized_view"] = "table"

    def label(self) -> str:
        return f"{self.relation.replace('_', ' ').capitalize()} {self.schema_}.{self.table} exists"


class ColumnRule(_Rule):
    kind: Literal["column"] = "column"
    schema_: Identifier = Field(default="public", alias="schema")
    table: Identifier
    column: Identifier
    data_type: Annotated[str, StringConstraints(max_length=100)] | None = None
    nullable: bool | None = None

    def label(self) -> str:
        return f"Column {self.schema_}.{self.table}.{self.column}"


class ConstraintRule(_Rule):
    kind: Literal["constraint"] = "constraint"
    schema_: Identifier = Field(default="public", alias="schema")
    table: Identifier
    constraint_type: Literal["primary_key", "unique", "foreign_key", "check"]
    constraint_name: Identifier | None = None
    columns: list[Identifier] | None = Field(default=None, max_length=32)
    references: Annotated[str, StringConstraints(max_length=130)] | None = None

    @model_validator(mode="after")
    def _needs_selector(self) -> ConstraintRule:
        if self.constraint_name is None and not self.columns and self.constraint_type != "check":
            raise ValueError("provide constraint_name or columns to identify the constraint")
        if self.constraint_type == "check" and self.constraint_name is None:
            raise ValueError("check constraints are identified by constraint_name")
        return self

    def label(self) -> str:
        what = self.constraint_name or ", ".join(self.columns or [])
        return f"{self.constraint_type.replace('_', ' ').capitalize()} on {self.schema_}.{self.table} ({what})"


class IndexRule(_Rule):
    kind: Literal["index"] = "index"
    schema_: Identifier = Field(default="public", alias="schema")
    table: Identifier
    index_name: Identifier | None = None
    columns: list[Identifier] | None = Field(default=None, max_length=32)
    unique: bool | None = None

    @model_validator(mode="after")
    def _needs_selector(self) -> IndexRule:
        if self.index_name is None and not self.columns:
            raise ValueError("provide index_name or columns")
        return self

    def label(self) -> str:
        return f"Index on {self.schema_}.{self.table} ({self.index_name or ', '.join(self.columns or [])})"


class ExtensionRule(_Rule):
    kind: Literal["extension"] = "extension"
    extension: Identifier

    def label(self) -> str:
        return f"Extension {self.extension} installed"


class RowCountRule(_Rule):
    kind: Literal["row_count"] = "row_count"
    schema_: Identifier = Field(default="public", alias="schema")
    table: Identifier
    exact: Annotated[int, Field(ge=0)] | None = None
    minimum: Annotated[int, Field(ge=0)] | None = None
    maximum: Annotated[int, Field(ge=0)] | None = None
    timeout_seconds: Annotated[int, Field(ge=1, le=600)] = 60

    @model_validator(mode="after")
    def _bounds(self) -> RowCountRule:
        if self.exact is None and self.minimum is None and self.maximum is None:
            raise ValueError("provide exact, minimum or maximum")
        if self.exact is not None and (self.minimum is not None or self.maximum is not None):
            raise ValueError("exact cannot be combined with minimum or maximum")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum must not exceed maximum")
        return self

    def label(self) -> str:
        return f"Row count of {self.schema_}.{self.table}"


class SqlAssertionRule(_Rule):
    kind: Literal["sql_assertion"] = "sql_assertion"
    sql: str
    expect: Literal["true", "no_rows", "equals", "between"] = "true"
    value: int | float | str | None = None
    minimum: float | None = None
    maximum: float | None = None
    timeout_seconds: Annotated[int, Field(ge=1, le=600)] = 30

    @field_validator("sql")
    @classmethod
    def _readonly(cls, v: str) -> str:
        try:
            return validate_readonly_sql(v)
        except UnsafeSql as exc:
            raise ValueError(str(exc)) from exc

    @model_validator(mode="after")
    def _expectation(self) -> SqlAssertionRule:
        if self.expect == "equals" and self.value is None:
            raise ValueError("expect=equals requires value")
        if self.expect == "between" and (self.minimum is None or self.maximum is None):
            raise ValueError("expect=between requires minimum and maximum")
        return self

    def label(self) -> str:
        return "SQL assertion"


class DatasetChecksumRule(_Rule):
    kind: Literal["dataset_checksum"] = "dataset_checksum"
    sql: str
    expected_sha256: Sha256Hex
    expected_rows: Annotated[int, Field(ge=0)] | None = None
    timeout_seconds: Annotated[int, Field(ge=1, le=900)] = 120

    @field_validator("sql")
    @classmethod
    def _readonly(cls, v: str) -> str:
        try:
            return validate_readonly_sql(v)
        except UnsafeSql as exc:
            raise ValueError(str(exc)) from exc

    def label(self) -> str:
        return "Reference dataset checksum"


class RecoveryPointRule(_Rule):
    """Query returning one timestamp: the newest transaction time present in the restored data."""

    kind: Literal["recovery_point"] = "recovery_point"
    required: bool = False
    sql: str
    timeout_seconds: Annotated[int, Field(ge=1, le=300)] = 30

    @field_validator("sql")
    @classmethod
    def _readonly(cls, v: str) -> str:
        try:
            return validate_readonly_sql(v)
        except UnsafeSql as exc:
            raise ValueError(str(exc)) from exc

    def label(self) -> str:
        return "Recovery point probe"


class HttpCheckRule(_Rule):
    kind: Literal["http_check"] = "http_check"
    path: Annotated[str, StringConstraints(pattern=r"^/[A-Za-z0-9._~!$&'()*+,;=:@/%?-]{0,200}$")]
    expected_status: Annotated[int, Field(ge=100, le=599)] = 200
    max_latency_ms: Annotated[int, Field(ge=1, le=60_000)] = 2000
    body_contains: Annotated[str, StringConstraints(max_length=200)] | None = None

    def label(self) -> str:
        return f"GET {self.path}"


Rule = Annotated[
    SchemaRule
    | TableRule
    | ColumnRule
    | ConstraintRule
    | IndexRule
    | ExtensionRule
    | RowCountRule
    | SqlAssertionRule
    | DatasetChecksumRule
    | RecoveryPointRule
    | HttpCheckRule,
    Field(discriminator="kind"),
]
_ADAPTER: TypeAdapter[Any] = TypeAdapter(Rule)


def parse_rule(data: dict[str, Any]) -> _Rule:
    rule: _Rule = _ADAPTER.validate_python(data)
    return rule


def dump_rule(rule: _Rule) -> dict[str, Any]:
    return rule.model_dump(by_alias=True, mode="json", exclude_none=True)


def rule_category(kind: str) -> str:
    k = RuleKind(kind)
    if k in STRUCTURAL_KINDS:
        return "structure"
    if k in DATA_KINDS:
        return "data"
    if k in APPLICATION_KINDS:
        return "application"
    raise ValueError(kind)  # pragma: no cover


class RestoreOptions(BaseModel):
    """Restore parameters a project may choose. Anything not listed here is not configurable,
    so project authors cannot inject arbitrary pg_restore arguments."""

    model_config = ConfigDict(extra="forbid")

    jobs: Annotated[int, Field(ge=1, le=4)] = 1


class Objectives(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rto_seconds: Annotated[int, Field(ge=1)] | None = None
    rpo_seconds: Annotated[int, Field(ge=1)] | None = None
