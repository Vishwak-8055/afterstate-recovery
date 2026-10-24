"use client";
import { ArrowDown, ArrowUp, Play, Plus, Trash2 } from "lucide-react";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { ApiError, describeError, get, post, put } from "@/lib/api";
import { useQuery } from "@/lib/hooks";
import { can } from "@/lib/rbac";
import { defaults, fromRule, newKey, toPayload, type DraftRule } from "@/lib/rules";
import type { Artifact, Meta, Page, Project, RuleField, RuleKindSpec, RunSummary } from "@/lib/types";
import { zodResolver } from "@/lib/zod-resolver";
import { useWorkspace } from "@/components/shell";
import { Async, Button, Field, Input, PageHeader, Panel, Pill, Select, Tabs, Textarea, useToast } from "@/components/ui";

const num = z.string().regex(/^\d+$/, "Enter a whole number.");
const optNum = z.string().regex(/^\d*$/, "Enter a whole number or leave empty.");
const schema = z.object({
  name: z.string().min(1, "Give the project a name.").max(120), description: z.string().max(2000), artifact_id: z.string(), artifact_label: z.string().max(120), app_suite: z.string(),
  jobs: z.string().regex(/^[1-4]$/, "Use 1 to 4 parallel jobs."), restore_timeout_seconds: num, validation_timeout_seconds: num, rto_seconds: optNum, rpo_seconds: optNum, max_test_age_days: num, max_attempts: z.string().regex(/^[1-5]$/, "Use 1 to 5 attempts."),
});
type Values = z.infer<typeof schema>;
const blank: Values = { name: "", description: "", artifact_id: "", artifact_label: "", app_suite: "", jobs: "1", restore_timeout_seconds: "1800", validation_timeout_seconds: "600", rto_seconds: "", rpo_seconds: "", max_test_age_days: "30", max_attempts: "2" };
const STAGE: Record<string, string> = { structural: "Structural", data: "Data", application: "Application" };

function FieldInput({ f, value, onChange, id }: { f: RuleField; value: string; onChange: (v: string) => void; id: string }) {
  if (f.type === "select") return <Select id={id} value={value} onChange={(e) => onChange(e.target.value)}>{!f.default && <option value="">Select</option>}{f.options?.map((o) => <option key={o} value={o}>{o.replace("_", " ")}</option>)}</Select>;
  if (f.type === "tristate") return <Select id={id} value={value} onChange={(e) => onChange(e.target.value)}><option value="">Any</option><option value="true">Yes</option><option value="false">No</option></Select>;
  if (f.type === "textarea") return <Textarea id={id} value={value} onChange={(e) => onChange(e.target.value)} placeholder={f.placeholder} spellCheck={false} />;
  return <Input id={id} value={value} type={f.type === "number" ? "number" : "text"} onChange={(e) => onChange(e.target.value)} placeholder={f.placeholder} className={f.name.includes("sha") || f.name === "path" ? "mono" : undefined} />;
}

function RuleBuilder({ meta, rules, setRules, errors }: { meta: Meta; rules: DraftRule[]; setRules: (r: DraftRule[]) => void; errors: Record<string, string> }) {
  const [kind, setKind] = useState(meta.rule_catalog[0]?.kind ?? "");
  const spec = (k: string) => meta.rule_catalog.find((s) => s.kind === k) as RuleKindSpec;
  const patch = (key: string, p: Partial<DraftRule>) => setRules(rules.map((r) => (r.key === key ? { ...r, ...p } : r)));
  const move = (i: number, d: number) => { const n = [...rules]; const [x] = n.splice(i, 1); n.splice(i + d, 0, x!); setRules(n); };
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-line bg-s1 p-4">
        <Field label="Add a validation rule" className="min-w-64 flex-1" hint={spec(kind)?.help}>{(id) => <Select id={id} value={kind} onChange={(e) => setKind(e.target.value)}>{["structural", "data", "application"].map((st) => <optgroup key={st} label={STAGE[st]}>{meta.rule_catalog.filter((s) => s.stage === st).map((s) => <option key={s.kind} value={s.kind}>{s.label}</option>)}</optgroup>)}</Select>}</Field>
        <Button onClick={() => setRules([...rules, { key: newKey(), kind, name: "", required: kind !== "recovery_point", values: defaults(spec(kind)) }])}><Plus className="size-4" />Add rule</Button>
      </div>
      {rules.length === 0 && <p className="rounded-lg border border-dashed border-line-strong p-6 text-center text-sm text-muted">No rules yet. Without rules a run can only prove that the restore completes and the database is not empty. That is a baseline, not verification.</p>}
      {rules.map((r, i) => { const s = spec(r.kind); return (
        <section key={r.key} className="rounded-lg border border-line bg-s1" aria-label={`Rule ${i + 1}: ${s.label}`}>
          <header className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-2.5"><div className="flex items-center gap-2.5"><span className="mono text-xs text-faint">{String(i + 1).padStart(2, "0")}</span><h3 className="text-sm font-semibold">{s.label}</h3><Pill tone="idle" icon={false}>{STAGE[s.stage]}</Pill></div>
            <div className="flex items-center gap-1"><label className="mr-2 flex items-center gap-1.5 text-xs text-muted"><input type="checkbox" checked={r.required} onChange={(e) => patch(r.key, { required: e.target.checked })} />Required</label>
              <Button variant="ghost" size="sm" aria-label="Move up" disabled={i === 0} onClick={() => move(i, -1)}><ArrowUp className="size-4" /></Button><Button variant="ghost" size="sm" aria-label="Move down" disabled={i === rules.length - 1} onClick={() => move(i, 1)}><ArrowDown className="size-4" /></Button><Button variant="ghost" size="sm" aria-label="Remove rule" onClick={() => setRules(rules.filter((x) => x.key !== r.key))}><Trash2 className="size-4" /></Button></div></header>
          <div className="grid gap-3 p-4 sm:grid-cols-2">
            <Field label="Label (optional)" className="sm:col-span-2">{(id) => <Input id={id} value={r.name} onChange={(e) => patch(r.key, { name: e.target.value })} placeholder="Shown in reports" />}</Field>
            {s.fields.map((f) => <Field key={f.name} label={f.label + (f.required ? " *" : "")} className={f.type === "textarea" ? "sm:col-span-2" : undefined}>{(id) => <FieldInput id={id} f={f} value={r.values[f.name] ?? ""} onChange={(v) => patch(r.key, { values: { ...r.values, [f.name]: v } })} />}</Field>)}
          </div>
          {errors[r.key] && <p role="alert" className="border-t border-fail-line bg-fail-bg px-4 py-2.5 text-sm text-fail">{errors[r.key]}</p>}
        </section>); })}
    </div>
  );
}

interface PlanT { artifact: Artifact | null; stages: { state: string; name: string; detail: string }[]; restore: { command_template: string[]; skipped_object_classes: string[]; timeout_seconds: number }; rules: { position: number; name: string; stage: string; required: boolean }[]; warnings: string[]; objectives: { rto_seconds: number | null; rpo_seconds: number | null }; retry: { max_attempts: number } }

function Plan({ base, id }: { base: string; id: string }) {
  const q = useQuery<PlanT>(`${base}/projects/${id}/plan`);
  return <Async q={q}>{(p) => (
    <div className="space-y-5">
      {p.warnings.length > 0 && <ul className="space-y-2" aria-label="Warnings">{p.warnings.map((w) => <li key={w} className="rounded-md border border-warn-line bg-warn-bg px-3.5 py-2.5 text-sm text-warn">{w}</li>)}</ul>}
      <Panel title="What a run will do"><ol className="space-y-3">{p.stages.map((s, i) => <li key={s.name} className="grid grid-cols-[2rem_1fr] gap-2"><span className="mono text-xs text-faint">{String(i + 1).padStart(2, "0")}</span><div><div className="text-sm font-medium">{s.name} <span className="mono ml-1 text-[11px] text-faint">{s.state}</span></div><p className="text-[13px] text-muted">{s.detail}</p></div></li>)}</ol></Panel>
      <Panel title="Restore command"><pre className="mono overflow-x-auto rounded-md bg-s2 p-3 text-[12.5px]">{p.restore.command_template.join(" ")}</pre><p className="mt-3 text-xs text-muted">Skipped for safety: {p.restore.skipped_object_classes.join(", ")}. Arguments are fixed by the platform; projects cannot add flags.</p></Panel>
      <Panel title={`Rules (${p.rules.length})`} flush>{p.rules.length ? <ul className="divide-y divide-line text-sm">{p.rules.map((r) => <li key={r.position} className="flex items-center justify-between px-4 py-2.5"><span>{r.name}</span><span className="flex gap-2"><Pill tone="idle" icon={false}>{r.stage}</Pill>{!r.required && <Pill tone="idle" icon={false}>optional</Pill>}</span></li>)}</ul> : <p className="p-4 text-sm text-muted">Baseline checks only.</p>}</Panel>
    </div>)}</Async>;
}

export default function ProjectEditor() {
  const { id } = useParams<{ id: string }>();
  const isNew = id === "new";
  const ws = useWorkspace();
  const router = useRouter();
  const toast = useToast();
  const writable = can(ws.role, "project.write");
  const proj = useQuery<Project>(isNew ? null : `${ws.base}/projects/${id}`);
  const meta = useQuery<Meta>(`${ws.base}/meta`);
  const arts = useQuery<Page<Artifact>>(`${ws.base}/artifacts?page_size=100`);
  const [tab, setTab] = useState<"config" | "rules" | "plan">("config");
  const [rules, setRules] = useState<DraftRule[]>([]);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saveError, setSaveError] = useState("");
  const [running, setRunning] = useState(false);
  const { register, handleSubmit, reset, formState } = useForm<Values>({ resolver: zodResolver(schema), defaultValues: blank });
  useEffect(() => {
    if (!proj.data || !meta.data) return;
    const p = proj.data;
    reset({ name: p.name, description: p.description, artifact_id: p.artifact_id ?? "", artifact_label: p.artifact_label ?? "", app_suite: p.app_suite ?? "", jobs: String(p.restore_options.jobs ?? 1), restore_timeout_seconds: String(p.restore_timeout_seconds), validation_timeout_seconds: String(p.validation_timeout_seconds), rto_seconds: p.rto_seconds ? String(p.rto_seconds) : "", rpo_seconds: p.rpo_seconds ? String(p.rpo_seconds) : "", max_test_age_days: String(p.max_test_age_days), max_attempts: String(p.max_attempts) });
    setRules(p.rules.map((r) => { const spec = meta.data!.rule_catalog.find((s) => s.kind === r.kind)!; return fromRule(spec, r); }));
  }, [proj.data, meta.data, reset]);

  const save = handleSubmit(async (v) => {
    setSaveError(""); setErrors({});
    const catalog = meta.data!.rule_catalog;
    const body = {
      name: v.name, description: v.description, artifact_id: v.artifact_id || null, artifact_label: v.artifact_label || null, app_suite: v.app_suite || null, restore_options: { jobs: Number(v.jobs) },
      restore_timeout_seconds: Number(v.restore_timeout_seconds), validation_timeout_seconds: Number(v.validation_timeout_seconds), rto_seconds: v.rto_seconds ? Number(v.rto_seconds) : null, rpo_seconds: v.rpo_seconds ? Number(v.rpo_seconds) : null,
      max_test_age_days: Number(v.max_test_age_days), max_attempts: Number(v.max_attempts), rules: rules.map((r) => toPayload(catalog.find((s) => s.kind === r.kind)!, r)),
    };
    const bad: Record<string, string> = {};
    await Promise.all(rules.map(async (r, i) => { try { await post(`${ws.base}/rules/validate`, body.rules[i]); } catch (e) { bad[r.key] = describeError(e); } }));
    if (Object.keys(bad).length) { setErrors(bad); setTab("rules"); setSaveError("Fix the highlighted rules before saving."); return; }
    try {
      const saved = isNew ? await post<Project>(`${ws.base}/projects`, body) : await put<Project>(`${ws.base}/projects/${id}`, body);
      toast("Project saved."); if (isNew) router.replace(ws.href(`/projects/${saved.id}`)); else proj.reload();
    } catch (e) { setSaveError(describeError(e)); if (e instanceof ApiError && e.code === "invalid_rules") setTab("rules"); }
  });
  const run = async () => { setRunning(true); try { const r = await post<RunSummary>(`${ws.base}/projects/${id}/runs`); router.push(ws.href(`/runs/${r.id}`)); } catch (e) { toast(describeError(e), "fail"); setRunning(false); } };
  void get;
  return (
    <Async q={isNew ? { data: {} as Project, error: undefined, loading: false, reload: () => undefined } : proj}>{() => (
      <Async q={meta}>{(m) => (
        <form onSubmit={save} noValidate>
          <PageHeader title={isNew ? "New recovery project" : proj.data?.name ?? "Project"} subtitle="Choose what to restore, define what must be true afterwards, and set the objectives the result is judged against."
            actions={<>{!isNew && can(ws.role, "run.start") && <Button type="button" busy={running} onClick={run}><Play className="size-4" />Run test now</Button>}{writable && <Button type="submit" variant="primary" busy={formState.isSubmitting}>Save project</Button>}</>} />
          {!writable && <p className="mb-4 rounded-md border border-idle-line bg-idle-bg px-3.5 py-2.5 text-sm text-idle">You can view this project but not change it. Workspace admins can edit projects.</p>}
          <Tabs value={tab} onChange={setTab} tabs={[{ id: "config", label: "Configuration" }, { id: "rules", label: "Validation rules", count: rules.length }, ...(isNew ? [] : [{ id: "plan" as const, label: "Execution plan" }])]} />
          {saveError && <p role="alert" className="mb-4 rounded-md border border-fail-line bg-fail-bg px-3.5 py-2.5 text-sm text-fail">{saveError}</p>}
          <fieldset disabled={!writable} className={tab === "config" ? "grid gap-5 lg:grid-cols-2" : "hidden"}>
            <Panel title="Identity and source" className="space-y-4">
              <Field label="Name" error={formState.errors.name?.message}>{(i) => <Input id={i} {...register("name")} />}</Field>
              <Field label="Description" error={formState.errors.description?.message}>{(i) => <Textarea id={i} {...register("description")} className="font-sans" />}</Field>
              <Field label="Backup to restore" hint="Pin one backup, or leave empty and use a series label to test the newest backup with that label.">{(i) => <Select id={i} {...register("artifact_id")}><option value="">Use the series label below</option>{arts.data?.items.filter((a) => !a.lab_scenario).map((a) => <option key={a.id} value={a.id}>{a.name} ({a.status})</option>)}</Select>}</Field>
              <Field label="Series label">{(i) => <Input id={i} {...register("artifact_label")} placeholder="orders-nightly" className="mono" />}</Field>
              <Field label="Application suite" hint="Predefined smoke tests run against the restored database.">{(i) => <Select id={i} {...register("app_suite")}><option value="">None</option>{m.app_suites.map((s) => <option key={s.key} value={s.key}>{s.key}: {s.description}</option>)}</Select>}</Field>
            </Panel>
            <Panel title="Restore, objectives and retries" className="space-y-4">
              <div className="grid grid-cols-2 gap-4"><Field label="Parallel restore jobs" error={formState.errors.jobs?.message}>{(i) => <Input id={i} inputMode="numeric" {...register("jobs")} />}</Field><Field label="Attempts on retryable failure" error={formState.errors.max_attempts?.message} hint="Interrupted workers and lost environments only.">{(i) => <Input id={i} inputMode="numeric" {...register("max_attempts")} />}</Field></div>
              <div className="grid grid-cols-2 gap-4"><Field label="Restore timeout (s)" error={formState.errors.restore_timeout_seconds?.message}>{(i) => <Input id={i} inputMode="numeric" {...register("restore_timeout_seconds")} />}</Field><Field label="Validation budget (s)" error={formState.errors.validation_timeout_seconds?.message}>{(i) => <Input id={i} inputMode="numeric" {...register("validation_timeout_seconds")} />}</Field></div>
              <div className="grid grid-cols-2 gap-4"><Field label="RTO target (s)" hint="Time to a verified, usable database." error={formState.errors.rto_seconds?.message}>{(i) => <Input id={i} inputMode="numeric" {...register("rto_seconds")} />}</Field><Field label="RPO target (s)" hint="Judged from evidence, else reported unknown." error={formState.errors.rpo_seconds?.message}>{(i) => <Input id={i} inputMode="numeric" {...register("rpo_seconds")} />}</Field></div>
              <Field label="Re-test at least every (days)" hint="A project becomes stale when its last successful test is older." error={formState.errors.max_test_age_days?.message}>{(i) => <Input id={i} inputMode="numeric" {...register("max_test_age_days")} />}</Field>
            </Panel>
          </fieldset>
          {tab === "rules" && <fieldset disabled={!writable}><RuleBuilder meta={m} rules={rules} setRules={setRules} errors={errors} /></fieldset>}
          {tab === "plan" && !isNew && <Plan base={ws.base} id={id} />}
        </form>)}</Async>)}</Async>
  );
}
