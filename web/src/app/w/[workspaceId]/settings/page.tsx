"use client";
import { useEffect, useState } from "react";
import { del, describeError, patch, post } from "@/lib/api";
import { bytes, relative } from "@/lib/format";
import { useQuery } from "@/lib/hooks";
import { can } from "@/lib/rbac";
import type { Role } from "@/lib/types";
import { useAuth, useWorkspace } from "@/components/shell";
import { Async, Button, Field, Input, KV, PageHeader, Panel, Pill, Select, Tabs, tableCls, useToast } from "@/components/ui";

interface Ws { name: string; role: Role; max_concurrent_runs: number; retention_days: number; webhook_configured: boolean; webhook_url: string | null }
interface Member { user_id: string; email: string; display_name: string; role: Role }
interface Sys { workers: { id: string; hostname: string; version: string; age_seconds: number; alive: boolean; current_run_id: string | null }[]; queue: Record<string, number>; limits: Record<string, number>; postgresql: string; lab_enabled: boolean; registration_enabled: boolean }

export default function SettingsPage() {
  const ws = useWorkspace();
  const { me, refresh } = useAuth();
  const toast = useToast();
  const [tab, setTab] = useState<"workspace" | "members" | "security">("workspace");
  const w = useQuery<Ws>(`${ws.base}`);
  const members = useQuery<{ items: Member[] }>(`${ws.base}/members`);
  const sys = useQuery<Sys>(`${ws.base}/system`, { pollMs: 5000 });
  const [form, setForm] = useState({ name: "", max_concurrent_runs: "2", retention_days: "90", webhook_url: "" });
  const [invite, setInvite] = useState({ email: "", role: "viewer" });
  useEffect(() => { if (w.data) setForm({ name: w.data.name, max_concurrent_runs: String(w.data.max_concurrent_runs), retention_days: String(w.data.retention_days), webhook_url: w.data.webhook_url ?? "" }); }, [w.data]);
  const manage = can(ws.role, "workspace.manage"), owner = can(ws.role, "members.manage");
  const run = async (fn: () => Promise<unknown>, ok: string) => { try { await fn(); toast(ok); w.reload(); members.reload(); refresh(); } catch (e) { toast(describeError(e), "fail"); } };
  return (
    <>
      <PageHeader title="Workspace settings" subtitle="Roles are enforced by the server on every request. This page only reflects what your role allows." />
      <Tabs value={tab} onChange={setTab} tabs={[{ id: "workspace", label: "Workspace" }, { id: "members", label: "Members", count: members.data?.items.length }, { id: "security", label: "Security and operations" }]} />
      {tab === "workspace" && <Panel title="General"><fieldset disabled={!manage} className="grid max-w-xl gap-4">
        <Field label="Name">{(id) => <Input id={id} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />}</Field>
        <div className="grid grid-cols-2 gap-4"><Field label="Concurrent runs" hint="Per workspace, enforced when a worker claims a run.">{(id) => <Input id={id} inputMode="numeric" value={form.max_concurrent_runs} onChange={(e) => setForm({ ...form, max_concurrent_runs: e.target.value })} />}</Field><Field label="Retention (days)" hint="How long run history is intended to be kept.">{(id) => <Input id={id} inputMode="numeric" value={form.retention_days} onChange={(e) => setForm({ ...form, retention_days: e.target.value })} />}</Field></div>
        <div><Button variant="primary" onClick={() => run(() => patch(ws.base, { name: form.name, max_concurrent_runs: Number(form.max_concurrent_runs), retention_days: Number(form.retention_days) }), "Settings saved.")}>Save changes</Button>{!manage && <span className="ml-3 text-sm text-muted">Only admins and owners can change these.</span>}</div></fieldset></Panel>}
      {tab === "members" && <div className="space-y-5"><Panel flush><Async q={members}>{(d) => <table className={tableCls.table}><thead><tr>{["Member", "Role", ""].map((h) => <th key={h} className={tableCls.th}>{h}</th>)}</tr></thead><tbody>{d.items.map((m) => (
        <tr key={m.user_id} className={tableCls.tr}><td className={tableCls.td}>{m.display_name}<div className="text-xs text-muted">{m.email}</div></td>
          <td className={tableCls.td}>{owner ? <Select aria-label={`Role for ${m.email}`} value={m.role} className="w-36" onChange={(e) => run(() => patch(`${ws.base}/members/${m.user_id}`, { role: e.target.value }), "Role updated.")}>{["owner", "admin", "operator", "viewer"].map((r) => <option key={r}>{r}</option>)}</Select> : <Pill tone="idle" icon={false}>{m.role}</Pill>}</td>
          <td className={tableCls.td + " text-right"}>{owner && m.user_id !== me?.user.id && <Button size="sm" variant="ghost" onClick={() => run(() => del(`${ws.base}/members/${m.user_id}`), "Member removed.")}>Remove</Button>}</td></tr>))}</tbody></table>}</Async></Panel>
        {owner && <Panel title="Add a member"><div className="flex flex-wrap items-end gap-3"><Field label="Email" hint="They must have registered first." className="w-72">{(id) => <Input id={id} type="email" value={invite.email} onChange={(e) => setInvite({ ...invite, email: e.target.value })} />}</Field><Field label="Role">{(id) => <Select id={id} value={invite.role} onChange={(e) => setInvite({ ...invite, role: e.target.value })} className="w-36">{["admin", "operator", "viewer", "owner"].map((r) => <option key={r}>{r}</option>)}</Select>}</Field><Button variant="primary" disabled={!invite.email} onClick={() => run(async () => { await post(`${ws.base}/members`, invite); setInvite({ ...invite, email: "" }); }, "Member added.")}>Add member</Button></div>
          <details className="mt-4 text-xs text-muted"><summary className="cursor-pointer">What each role can do</summary><ul className="mt-2 space-y-1"><li><b className="text-ink">viewer</b> reads everything except audit history.</li><li><b className="text-ink">operator</b> also starts and cancels runs, verifies backups, runs the Lab.</li><li><b className="text-ink">admin</b> also registers backups, edits projects and schedules, reads audit history.</li><li><b className="text-ink">owner</b> also manages members and the notification webhook.</li></ul></details></Panel>}</div>}
      {tab === "security" && <div className="grid gap-5 lg:grid-cols-2">
        <Panel title="Failure notifications"><Field label="Webhook URL" hint="Failed and not-verified runs are POSTed here as JSON. Private and loopback addresses are refused.">{(id) => <Input id={id} disabled={!owner} value={form.webhook_url} onChange={(e) => setForm({ ...form, webhook_url: e.target.value })} placeholder="https://hooks.example.com/afterstate" />}</Field>
          {owner ? <div className="mt-3 flex gap-2"><Button variant="primary" onClick={() => run(() => patch(ws.base, { webhook_url: form.webhook_url }), "Webhook saved.")}>Save webhook</Button><Button onClick={() => run(() => patch(ws.base, { clear_webhook: true }), "Webhook removed.")}>Remove</Button></div> : <p className="mt-3 text-xs text-muted">Only owners can change the webhook.</p>}</Panel>
        <Panel title="Isolation guarantees"><ul className="list-disc space-y-1.5 pl-4 text-[13px] text-muted"><li>Each restore runs in a private PostgreSQL cluster with no network listener.</li><li>Backups restore as an unprivileged role: no programs, server files or untrusted extensions.</li><li>Validation queries run read-only as a separate role with a time limit.</li><li>Nothing inside a backup is executed. Plain-SQL dumps are refused.</li></ul></Panel>
        <Panel title="System" className="lg:col-span-2"><Async q={sys}>{(s) => <div className="grid gap-5 md:grid-cols-3"><div><h3 className="mb-2 text-xs font-medium text-muted">Workers</h3>{s.workers.length === 0 ? <p className="text-sm text-warn">No worker has reported in. Queued runs will wait.</p> : <ul className="space-y-2 text-sm">{s.workers.map((x) => <li key={x.id}><Pill tone={x.alive ? "pass" : "warn"}>{x.alive ? "alive" : "stale"}</Pill> <span className="mono text-xs">{x.hostname}</span><div className="text-xs text-muted">v{x.version} · seen {relative(new Date(Date.now() - x.age_seconds * 1000).toISOString())}</div></li>)}</ul>}</div>
          <div><h3 className="mb-2 text-xs font-medium text-muted">Queue</h3><dl className="text-sm">{Object.entries(s.queue).map(([k, v]) => <KV key={k} k={k.replace("_", " ").toLowerCase()}>{v}</KV>)}</dl></div>
          <div><h3 className="mb-2 text-xs font-medium text-muted">Limits</h3><dl className="text-sm"><KV k="Max backup">{bytes(s.limits.max_artifact_bytes ?? null)}</KV><KV k="Env disk cap">{bytes(s.limits.max_environment_bytes ?? null)}</KV><KV k="PostgreSQL">{s.postgresql}</KV></dl></div></div>}</Async></Panel></div>}
    </>
  );
}
