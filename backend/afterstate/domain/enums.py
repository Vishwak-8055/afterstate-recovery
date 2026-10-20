"""Closed vocabularies shared by the database, API, engine and worker."""

from __future__ import annotations

from enum import StrEnum


class RunState(StrEnum):
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    RESTORING = "RESTORING"
    VALIDATING = "VALIDATING"
    FINALIZING = "FINALIZING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    NEEDS_CLEANUP = "NEEDS_CLEANUP"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"


class StageName(StrEnum):
    ARTIFACT_VERIFICATION = "artifact_verification"
    ENVIRONMENT = "environment_provisioning"
    RESTORE = "restore"
    STRUCTURAL = "structural_validation"
    DATA = "data_validation"
    APPLICATION = "application_validation"
    CLEANUP = "cleanup"
    REPORT = "report"


class StageState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class Outcome(StrEnum):
    """Result of one check. Only PASS counts as evidence of correctness."""

    PASS = "PASS"  # noqa: S105 - an outcome label, not a credential
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"
    INCONCLUSIVE = "INCONCLUSIVE"


class Coverage(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


class Trigger(StrEnum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    RETRY = "retry"
    LAB = "lab"


class ArtifactFormat(StrEnum):
    CUSTOM = "custom"
    TAR = "tar"
    PLAIN_SQL = "plain_sql"
    UNKNOWN = "unknown"


class ArtifactStatus(StrEnum):
    VERIFIED = "verified"
    REJECTED = "rejected"
    UNVERIFIED = "unverified"


class CleanupTaskState(StrEnum):
    PENDING = "PENDING"
    DONE = "DONE"
    FAILED = "FAILED"


class RunCleanupState(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    DONE = "DONE"
    FAILED = "FAILED"


class ObjectiveStatus(StrEnum):
    MET = "MET"
    MISSED = "MISSED"
    UNKNOWN = "UNKNOWN"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class FailureClass(StrEnum):
    ARTIFACT_MISSING = "ARTIFACT_MISSING"
    ARTIFACT_CHECKSUM_MISMATCH = "ARTIFACT_CHECKSUM_MISMATCH"
    ARTIFACT_CORRUPT = "ARTIFACT_CORRUPT"
    FORMAT_UNSUPPORTED = "FORMAT_UNSUPPORTED"
    VERSION_INCOMPATIBLE = "VERSION_INCOMPATIBLE"
    ENVIRONMENT_UNAVAILABLE = "ENVIRONMENT_UNAVAILABLE"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    RESTORE_FAILED = "RESTORE_FAILED"
    RESTORE_TIMEOUT = "RESTORE_TIMEOUT"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    VALIDATION_INCONCLUSIVE = "VALIDATION_INCONCLUSIVE"
    OBJECTIVE_MISSED = "OBJECTIVE_MISSED"
    OBJECTIVE_UNKNOWN = "OBJECTIVE_UNKNOWN"
    WORKER_INTERRUPTED = "WORKER_INTERRUPTED"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    CANCELLED = "CANCELLED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


#: Failure classes that a retry could plausibly fix. Corruption, bad rules and missed objectives
#: are properties of the data or configuration, so retrying them would only repeat the verdict.
RETRYABLE_FAILURES: frozenset[FailureClass] = frozenset(
    {
        FailureClass.ENVIRONMENT_UNAVAILABLE,
        FailureClass.WORKER_INTERRUPTED,
        FailureClass.INTERNAL_ERROR,
    }
)


class RuleKind(StrEnum):
    SCHEMA = "schema"
    TABLE = "table"
    COLUMN = "column"
    CONSTRAINT = "constraint"
    INDEX = "index"
    EXTENSION = "extension"
    ROW_COUNT = "row_count"
    SQL_ASSERTION = "sql_assertion"
    DATASET_CHECKSUM = "dataset_checksum"
    RECOVERY_POINT = "recovery_point"
    HTTP_CHECK = "http_check"


STRUCTURAL_KINDS = frozenset(
    {RuleKind.SCHEMA, RuleKind.TABLE, RuleKind.COLUMN, RuleKind.CONSTRAINT, RuleKind.INDEX, RuleKind.EXTENSION}
)
DATA_KINDS = frozenset({RuleKind.ROW_COUNT, RuleKind.SQL_ASSERTION, RuleKind.DATASET_CHECKSUM, RuleKind.RECOVERY_POINT})
APPLICATION_KINDS = frozenset({RuleKind.HTTP_CHECK})
