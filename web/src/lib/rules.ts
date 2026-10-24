import type { Rule, RuleField, RuleKindSpec } from "./types";

export type FormValues = Record<string, string>;
export interface DraftRule { key: string; kind: string; name: string; required: boolean; values: FormValues }

let counter = 0;
export const newKey = () => `r${Date.now().toString(36)}${counter++}`;

export function defaults(spec: RuleKindSpec): FormValues {
  const v: FormValues = {};
  for (const f of spec.fields) if (f.default !== undefined) v[f.name] = String(f.default);
  return v;
}

function encode(f: RuleField, raw: string | undefined): unknown {
  const s = (raw ?? "").trim();
  if (s === "") return undefined;
  if (f.type === "number") { const n = Number(s); return Number.isFinite(n) ? n : s; }
  if (f.type === "list") return s.split(",").map((x) => x.trim()).filter(Boolean);
  if (f.type === "tristate") return s === "true" ? true : s === "false" ? false : undefined;
  if (f.name === "value") { const n = Number(s); return Number.isFinite(n) && /^-?\d+(\.\d+)?$/.test(s) ? n : s; }
  return s;
}

/** Form values to the API rule object; empty optional fields are omitted so server defaults apply. */
export function toPayload(spec: RuleKindSpec, d: DraftRule): Record<string, unknown> {
  const out: Record<string, unknown> = { kind: spec.kind, required: d.required };
  if (d.name.trim()) out.name = d.name.trim();
  for (const f of spec.fields) { const v = encode(f, d.values[f.name]); if (v !== undefined) out[f.name] = v; }
  return out;
}

export function fromRule(spec: RuleKindSpec, r: Rule): DraftRule {
  const values: FormValues = {};
  for (const f of spec.fields) {
    const raw = r.config[f.name];
    if (raw === undefined || raw === null) continue;
    values[f.name] = Array.isArray(raw) ? raw.join(", ") : String(raw);
  }
  return { key: newKey(), kind: r.kind, name: r.name === (r.config.name as string | undefined) ? r.name : "", required: r.required, values };
}
