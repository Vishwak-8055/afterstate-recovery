"""Role-based access control. Permissions are enforced on the server for every request."""

from __future__ import annotations

from enum import StrEnum

from afterstate.domain.enums import Role


class Permission(StrEnum):
    WORKSPACE_READ = "workspace.read"
    WORKSPACE_MANAGE = "workspace.manage"
    MEMBERS_MANAGE = "members.manage"
    SECURITY_MANAGE = "security.manage"
    ARTIFACT_READ = "artifact.read"
    ARTIFACT_WRITE = "artifact.write"
    ARTIFACT_VERIFY = "artifact.verify"
    ARTIFACT_DELETE = "artifact.delete"
    PROJECT_READ = "project.read"
    PROJECT_WRITE = "project.write"
    RUN_READ = "run.read"
    RUN_START = "run.start"
    RUN_CANCEL = "run.cancel"
    SCHEDULE_READ = "schedule.read"
    SCHEDULE_WRITE = "schedule.write"
    AUDIT_READ = "audit.read"
    LAB_RUN = "lab.run"


_VIEWER = {
    Permission.WORKSPACE_READ,
    Permission.ARTIFACT_READ,
    Permission.PROJECT_READ,
    Permission.RUN_READ,
    Permission.SCHEDULE_READ,
}
_OPERATOR = _VIEWER | {
    Permission.ARTIFACT_VERIFY,
    Permission.RUN_START,
    Permission.RUN_CANCEL,
    Permission.LAB_RUN,
}
_ADMIN = _OPERATOR | {
    Permission.ARTIFACT_WRITE,
    Permission.ARTIFACT_DELETE,
    Permission.PROJECT_WRITE,
    Permission.SCHEDULE_WRITE,
    Permission.AUDIT_READ,
    Permission.WORKSPACE_MANAGE,
}
_OWNER = _ADMIN | {Permission.MEMBERS_MANAGE, Permission.SECURITY_MANAGE}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset(_VIEWER),
    Role.OPERATOR: frozenset(_OPERATOR),
    Role.ADMIN: frozenset(_ADMIN),
    Role.OWNER: frozenset(_OWNER),
}


def role_allows(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]
