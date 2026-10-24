import { describe, expect, it } from "vitest";
import { bytes, duration, humanSeconds, relative } from "@/lib/format";
import { can } from "@/lib/rbac";
import { defaults, toPayload, fromRule, type DraftRule } from "@/lib/rules";
import { qs, describeError, ApiError } from "@/lib/api";
import type { RuleKindSpec } from "@/lib/types";

describe("formatters", () => {
  it("formats durations at every scale and never invents a value", () => {
    expect(duration(0.25)).toBe("250 ms");
    expect(duration(4.5)).toBe("4.50 s");
    expect(duration(75)).toBe("1 min 15 s");
    expect(duration(3720)).toBe("1 h 2 min");
    expect(duration(null)).toBe("-");
    expect(duration(undefined)).toBe("-");
  });
  it("formats bytes with binary units", () => {
    expect(bytes(0)).toBe("0 B");
    expect(bytes(1536)).toBe("1.50 KiB");
    expect(bytes(222377)).toBe("217.2 KiB");
    expect(bytes(null)).toBe("-");
  });
  it("describes relative time and objectives", () => {
    const now = Date.parse("2026-10-22T12:00:00Z");
    expect(relative("2026-10-22T11:59:50Z", now)).toBe("just now");
    expect(relative("2026-10-22T11:00:00Z", now)).toBe("1 h ago");
    expect(relative("2026-10-19T12:00:00Z", now)).toBe("3 d ago");
    expect(relative(null)).toBe("never");
    expect(humanSeconds(172800)).toBe("2 d");
    expect(humanSeconds(90)).toBe("90 s");
  });
});

describe("role hints mirror the server matrix", () => {
  it("is monotonic and conservative", () => {
    expect(can("viewer", "run.start")).toBe(false);
    expect(can("operator", "run.start")).toBe(true);
    expect(can("operator", "artifact.write")).toBe(false);
    expect(can("admin", "audit.read")).toBe(true);
    expect(can("admin", "members.manage")).toBe(false);
    expect(can("owner", "members.manage")).toBe(true);
    expect(can(undefined, "run.start")).toBe(false);
  });
});

const spec: RuleKindSpec = { kind: "constraint", label: "Constraint", stage: "structural", help: "", fields: [
  { name: "table", label: "Table", type: "text", required: true }, { name: "constraint_type", label: "Type", type: "select", options: ["primary_key"], required: true },
  { name: "columns", label: "Columns", type: "list" }, { name: "nullable", label: "Nullable", type: "tristate" }, { name: "exact", label: "Exactly", type: "number" }, { name: "schema", label: "Schema", type: "text", default: "public" } ] };

describe("rule builder serialisation", () => {
  const draft = (values: Record<string, string>): DraftRule => ({ key: "k", kind: "constraint", name: "  ", required: true, values });
  it("omits empty fields and converts numbers, lists and tri-state values", () => {
    const p = toPayload(spec, draft({ table: " orders ", constraint_type: "primary_key", columns: "id, tenant_id,", nullable: "false", exact: "42", schema: "" }));
    expect(p).toEqual({ kind: "constraint", required: true, table: "orders", constraint_type: "primary_key", columns: ["id", "tenant_id"], nullable: false, exact: 42 });
    expect("name" in p).toBe(false);
  });
  it("applies catalog defaults and round-trips an existing rule", () => {
    expect(defaults(spec)).toEqual({ schema: "public" });
    const back = fromRule(spec, { name: "Primary key", kind: "constraint", required: false, config: { table: "orders", columns: ["id"], constraint_type: "primary_key", name: "Primary key" } });
    expect(back.values).toMatchObject({ table: "orders", columns: "id", constraint_type: "primary_key" });
    expect(back.required).toBe(false);
  });
});

describe("api helpers", () => {
  it("builds query strings without empty values", () => {
    expect(qs({ a: 1, b: "", c: undefined, d: ["x", "y"], e: false })).toBe("?a=1&d=x&d=y&e=false");
    expect(qs({})).toBe("");
  });
  it("turns API validation details into one readable message", () => {
    const e = new ApiError(422, "invalid_rules", "Bad rules.", [{ field: "0.sql", message: "keyword 'DELETE' is not allowed" }]);
    expect(describeError(e)).toBe("Bad rules. 0.sql: keyword 'DELETE' is not allowed");
    expect(describeError(new Error("boom"))).toBe("boom");
  });
});
