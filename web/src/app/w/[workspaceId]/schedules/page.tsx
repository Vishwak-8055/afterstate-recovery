"use client";
import { CalendarClock, Plus } from "lucide-react";
import { useState } from "react";
import { del, describeError, patch, post, qs } from "@/lib/api";
import { dateTime, relative } from "@/lib/format";
import { useQuery } from "@/lib/hooks";
import { can } from "@/lib/rbac";
import type { Page, Project, Schedule } from "@/lib/types";
import { useWorkspace } from "@/components/shell";
import { Async, Button, ConfirmDialog, Dialog, EmptyState, Field, Input, PageHeader, Panel, Pill, Select, tableCls, useToast } from "@/components/ui";

interface Firing { id: string; scheduled_for: string; outcome: string; run_id: string | null; detail: string }
const FIRING: Record<string, "pass" | "warn" | "idle" | "fail"> = { enqueued: "pass", skipped_overlap: "idle", skipped_missed: "warn", error: "fail" };

function Form({ open, onClose, onSaved, projects }: { open: boolean; onClose: () => void; onSaved: () => void; projects: Project[] }) {
  const ws = useWorkspace();
  const [v, setV] = useState({ name: "", project_id: "", cron_expr: "0 3 * * *", timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC", missed_run_policy: "skip", retry_max_attempts: "1", retry_backoff_seconds: "300" });
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true); setErr("");
    try { await post(`${ws.base}/schedules`, { ...v, retry_max_attempts: Number(v.retry_max_attempts), retry_backoff_seconds: Number(v.retry_backoff_seconds) }); onSaved(); onClose(); } catch (e) { setErr(describeError(e)); } finally { setBusy(false); }
  };
  const s = (k: keyof typeof v) => ({ value: v[k], onChange: (e: { target: { value: string } }) => setV({ ...v, [k]: e.target.value }) });
  return (
    <Dialog open={open} onClose={onClose} title="New schedule" wide footer={<><Button onClick={onClose}>Cancel</Button><Button variant="primary" busy={busy} disabled={!v.name || !v.project_id} onClick={save}>Create schedule</Button></>}>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Name">{(id) => <Input id={id} {...s("name")} placeholder="Nightly orders restore test" />}</Field>
        <Field label="Project">{(id) => <Select id={id} {...s("project_id")}><option value="">Select a project</option>{projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</Select>}</Field>
        <Field label="Cron expression" hint="Five fields, evaluated in the time zone below. Minimum interval 5 minutes.">{(id) => <Input id={id} {...s("cron_expr")} className="mono" />}</Field>
        <Field label="Time zone" hint="IANA name. Daylight saving follows local wall-clock time.">{(id) => <Input id={id} {...s("timezone")} />}</Field>
        <Field label="If runs were missed" hint="The scheduler was not running when a slot came due.">{(id) => <Select id={id} {...s("missed_run_policy")}><option value="skip">Skip and record them</option><option value="run_once">Run once to catch up</option></Select>}</Field>
        <div className="grid grid-cols-2 gap-3"><Field label="Retries">{(id) => <Input id={id} inputMode="numeric" {...s("retry_max_attempts")} />}</Field><Field label="Backoff (s)">{(id) => <Input id={id} inputMode="numeric" {...s("retry_backoff_seconds")} />}</Field></div>
        {err && <p role="alert" className="text-sm text-fail sm:col-span-2">{err}</p>}
      </div>
    </Dialog>
  );
}

export default function SchedulesPage() {
  const ws = useWorkspace();
  const toast = useToast();
  const q = useQuery<{ items: Schedule[] }>(`${ws.base}/schedules`, { pollMs: 10000 });
  const projects = useQuery<Page<Project>>(`${ws.base}/projects?page_size=100`);
  const [open, setOpen] = useState(false);
  const [hist, setHist] = useState<Schedule>();
  const [remove, setRemove] = useState<Schedule>();
  const firings = useQuery<{ items: Firing[] }>(hist ? `${ws.base}/schedules/${hist.id}/firings${qs({ page_size: 25 })}` : null);
  const writable = can(ws.role, "schedule.write");
  const toggle = async (s: Schedule) => { try { await patch(`${ws.base}/schedules/${s.id}`, { enabled: !s.enabled }); q.reload(); } catch (e) { toast(describeError(e), "fail"); } };
  return (
    <>
      <PageHeader title="Schedules" subtitle="Recurring recovery tests run in the worker, so they do not need anyone signed in. Overlaps, missed runs and retries are recorded." actions={writable && <Button variant="primary" onClick={() => setOpen(true)}><Plus className="size-4" />New schedule</Button>} />
      <Panel flush>
        <Async q={q} isEmpty={(d) => d.items.length === 0} empty={<EmptyState icon={<CalendarClock className="size-8" />} title="No schedules" action={writable ? <Button variant="primary" onClick={() => setOpen(true)}>Create a schedule</Button> : undefined}>A backup is only as trustworthy as its most recent restore test. Schedule one.</EmptyState>}>
          {(d) => <div className={tableCls.wrap}><table className={tableCls.table}><thead><tr>{["Schedule", "When", "Next run", "Last fired", "Policy", ""].map((h) => <th key={h} className={tableCls.th}>{h}</th>)}</tr></thead><tbody>{d.items.map((s) => (
            <tr key={s.id} className={tableCls.tr}>
              <td className={tableCls.td}><div className="font-medium">{s.name}</div><div className="text-xs text-muted">{s.project_name}</div></td>
              <td className={tableCls.td}><span className="mono">{s.cron_expr}</span><div className="text-xs text-muted">{s.timezone}</div></td>
              <td className={tableCls.td}>{s.enabled ? <span title={dateTime(s.next_run_at)}>{s.next_run_at ? dateTime(s.next_run_at) : "-"}</span> : <Pill tone="idle" icon={false}>paused</Pill>}</td>
              <td className={tableCls.td + " text-muted"}>{relative(s.last_fired_at)}</td>
              <td className={tableCls.td + " text-xs text-muted"}>missed: {s.missed_run_policy.replace("_", " ")}<br />retries: {s.retry_max_attempts}</td>
              <td className={tableCls.td + " text-right whitespace-nowrap"}><Button size="sm" variant="ghost" onClick={() => setHist(s)}>History</Button>{writable && <><Button size="sm" variant="ghost" onClick={() => toggle(s)}>{s.enabled ? "Pause" : "Resume"}</Button><Button size="sm" variant="ghost" onClick={() => setRemove(s)}>Delete</Button></>}</td>
            </tr>))}</tbody></table></div>}
        </Async>
      </Panel>
      <Form open={open} onClose={() => setOpen(false)} onSaved={() => { q.reload(); toast("Schedule created."); }} projects={projects.data?.items ?? []} />
      <Dialog open={!!hist} onClose={() => setHist(undefined)} title={`History: ${hist?.name ?? ""}`} wide>
        <Async q={firings} isEmpty={(d) => d.items.length === 0} empty={<p className="text-sm text-muted">This schedule has not fired yet.</p>}>{(d) => <ul className="divide-y divide-line text-sm">{d.items.map((f) => <li key={f.id} className="flex items-start justify-between gap-3 py-2.5"><div><div>{dateTime(f.scheduled_for)}</div>{f.detail && <div className="text-xs text-muted">{f.detail}</div>}</div><Pill tone={FIRING[f.outcome] ?? "idle"} icon={false}>{f.outcome.replace("_", " ")}</Pill></li>)}</ul>}</Async>
      </Dialog>
      <ConfirmDialog open={!!remove} title="Delete this schedule?" danger confirmLabel="Delete" onClose={() => setRemove(undefined)} onConfirm={async () => { if (remove) { await del(`${ws.base}/schedules/${remove.id}`); setRemove(undefined); q.reload(); toast("Schedule deleted."); } }} body={<>Future runs stop. Existing runs and their reports are kept.</>} />
    </>
  );
}
