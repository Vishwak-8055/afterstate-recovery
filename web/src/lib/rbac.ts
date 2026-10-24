import type { Role } from "./types";

// UI hint only: the API enforces every permission on the server. Mirrors backend/afterstate/security/rbac.py.
const order: Role[] = ["viewer", "operator", "admin", "owner"];
export type Action = "run.start" | "run.cancel" | "lab.run" | "artifact.verify" | "artifact.write" | "project.write" | "schedule.write" | "audit.read" | "workspace.manage" | "members.manage";
const minimum: Record<Action, Role> = {
  "run.start": "operator", "run.cancel": "operator", "lab.run": "operator", "artifact.verify": "operator",
  "artifact.write": "admin", "project.write": "admin", "schedule.write": "admin", "audit.read": "admin", "workspace.manage": "admin", "members.manage": "owner",
};
export function can(role: Role | undefined, action: Action): boolean {
  return !!role && order.indexOf(role) >= order.indexOf(minimum[action]);
}
