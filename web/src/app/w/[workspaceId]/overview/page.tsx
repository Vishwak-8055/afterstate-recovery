"use client";
import Link from "next/link";
import { Archive, CircleCheck, Play, TriangleAlert } from "lucide-react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis, Bar, BarChart } from "recharts";
import { useState } from "react";
import { describeError, post } from "@/lib/api";
import { dateTime, duration, relative } from "@/lib/format";
import { useQuery } from "@/lib/hooks";
import { can } from "@/lib/rbac";
import type { Overview, RunSummary } from "@/lib/types";
import { useWorkspace } from "@/components/shell";
import { Async, Button, EmptyState, LinkButton, PageHeader, Panel, Pill, StatePill, tableCls, useToast, type Tone } from "@/components/ui";

const Flag = ({ n }: { n: number }) => (n > 0 ? <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warn" aria-label="needs a decision" /> : <CircleCheck className="mt-0.5 size-4 shrink-0 text-pass" aria-label="nothing to decide" />);

const READINESS: Record<string, { label: string; tone: Tone; meaning: string }> = {
  verified: { label: "Verified", tone: "pass", meaning: "A recovery test succeeded within the allowed age and the latest run did not fail." },
  stale: { label: "Stale", tone: "warn", meaning: "The last successful recovery test is older than this project's limit." },
  at_risk: { label: "At risk", tone: "fail", meaning: "The latest run failed, needs attention, or left cleanup undone." },
  unverified: { label: "Unverified", tone: "warn", meaning: "No recovery test has ever succeeded." },
  running: { label: "Running", tone: "active", meaning: "A recovery test is queued or in progress." },
};

export default function OverviewPage() {
  const ws = useWorkspace();
  const q = useQuery<Overview>(`${ws.base}/overview`, { pollMs: 5000 });
  const toast = useToast();
  const [busy, setBusy] = useState<string>();
  const start = async (id: string) => {
    setBusy(id);
    try { const r = await post<RunSummary>(`${ws.base}/projects/${id}/runs`); toast("Recovery run queued."); window.location.assign(ws.href(`/runs/${r.id}`)); }
    catch (e) { toast(describeError(e), "fail"); setBusy(undefined); }
  };
  return (
    <>
      <PageHeader title="Recovery readiness" subtitle="What the evidence says right now: which systems have a recently proven recovery, which do not, and what needs a decision." actions={<LinkButton href={ws.href("/projects")} variant="secondary">Projects</LinkButton>} />
      <Async q={q} isEmpty={(d) => d.projects_total === 0 && d.recent_runs.length === 0}
        empty={<Panel><EmptyState icon={<Archive className="size-8" />} title="Nothing has been tested yet" action={<div className="flex gap-2"><LinkButton href={ws.href("/backups")} variant="primary">Register a backup</LinkButton><LinkButton href={ws.href("/lab")}>Try the Recovery Lab</LinkButton></div>}>Register a PostgreSQL backup, define a recovery project, and run your first test. The Recovery Lab lets you watch the real engine work on synthetic backups first.</EmptyState></Panel>}>
        {(d) => (
          <div className="space-y-6">
            <div className="flex flex-wrap gap-2" aria-label="Readiness summary">
              {Object.entries(READINESS).map(([k, r]) => <Pill key={k} tone={r.tone} icon={false} className="px-3 py-1 text-[13px]"><span className="tnum">{d.readiness[k] ?? 0}</span> {r.label.toLowerCase()}</Pill>)}
            </div>
            <Panel title="Projects" flush>
              {d.projects.length === 0 ? <EmptyState title="No recovery projects" action={<LinkButton href={ws.href("/projects/new")} variant="primary">Create a project</LinkButton>} /> : (
                <div className={tableCls.wrap}><table className={tableCls.table}>
                  <thead><tr><th className={tableCls.th}>Project</th><th className={tableCls.th}>Readiness</th><th className={tableCls.th}>Last successful test</th><th className={tableCls.th}>Latest run</th><th className={tableCls.th}><span className="sr-only">Actions</span></th></tr></thead>
                  <tbody>{d.projects.map((p) => (
                    <tr key={p.project_id} className={tableCls.tr}>
                      <td className={tableCls.td}><Link className="font-medium hover:underline" href={ws.href(`/projects/${p.project_id}`)}>{p.name}</Link></td>
                      <td className={tableCls.td}><Pill tone={READINESS[p.readiness]!.tone}>{READINESS[p.readiness]!.label}</Pill><p className="mt-1 max-w-xs text-xs text-muted">{p.reason}</p></td>
                      <td className={tableCls.td}>{p.last_success_at ? <><span title={dateTime(p.last_success_at)}>{relative(p.last_success_at)}</span><div className="text-xs text-muted">limit {p.max_test_age_days} d</div></> : <span className="text-muted">never</span>}</td>
                      <td className={tableCls.td}>{p.latest_run ? <Link href={ws.href(`/runs/${p.latest_run.id}`)} className="inline-flex items-center gap-2 hover:underline"><StatePill state={p.latest_run.state} /><span className="mono text-xs text-muted">{duration(p.latest_run.metrics.recovery_seconds)}</span></Link> : <span className="text-muted">-</span>}</td>
                      <td className={tableCls.td + " text-right"}>{can(ws.role, "run.start") && <Button size="sm" busy={busy === p.project_id} onClick={() => start(p.project_id)}><Play className="size-3.5" />Run test</Button>}</td>
                    </tr>))}</tbody></table></div>
              )}
            </Panel>
            <div className="grid gap-6 lg:grid-cols-[1fr_20rem]">
              <div className="space-y-6">
                <Panel title="Measured recovery time">
                  {d.series.filter((s) => s.recovery_seconds != null).length < 2 ? <p className="py-6 text-center text-sm text-muted">The trend appears after two completed runs. Nothing is plotted until it has been measured.</p> : (
                    <div className="h-56" role="img" aria-label="Line chart of recovery and restore seconds per run"><ResponsiveContainer width="100%" height="100%">
                      <LineChart data={d.series} margin={{ top: 8, right: 12, bottom: 0, left: -12 }}>
                        <CartesianGrid stroke="var(--line)" vertical={false} /><XAxis dataKey="at" tickFormatter={(v: string) => new Date(v).toLocaleDateString(undefined, { month: "short", day: "numeric" })} stroke="var(--faint)" fontSize={11} /><YAxis stroke="var(--faint)" fontSize={11} unit="s" />
                        <Tooltip contentStyle={{ background: "var(--s2)", border: "1px solid var(--line-strong)", borderRadius: 6, fontSize: 12 }} labelFormatter={(v) => dateTime(String(v))} />
                        <Line type="monotone" dataKey="recovery_seconds" name="Recovery (s)" stroke="var(--active)" strokeWidth={2} dot={{ r: 2.5 }} /><Line type="monotone" dataKey="restore_seconds" name="Restore only (s)" stroke="var(--idle)" strokeWidth={1.5} strokeDasharray="4 3" dot={false} />
                      </LineChart></ResponsiveContainer></div>)}
                </Panel>
                <Panel title="Restore throughput">
                  {d.series.filter((s) => s.throughput_mb_s != null).length < 2 ? <p className="py-6 text-center text-sm text-muted">Throughput is plotted once at least two restores have completed.</p> : (
                    <div className="h-44" role="img" aria-label="Bar chart of restore throughput in MiB per second"><ResponsiveContainer width="100%" height="100%"><BarChart data={d.series} margin={{ top: 8, right: 12, bottom: 0, left: -12 }}><CartesianGrid stroke="var(--line)" vertical={false} /><XAxis dataKey="at" tickFormatter={(v: string) => new Date(v).toLocaleDateString(undefined, { month: "short", day: "numeric" })} stroke="var(--faint)" fontSize={11} /><YAxis stroke="var(--faint)" fontSize={11} unit=" MiB/s" /><Tooltip contentStyle={{ background: "var(--s2)", border: "1px solid var(--line-strong)", borderRadius: 6, fontSize: 12 }} labelFormatter={(v) => dateTime(String(v))} /><Bar dataKey="throughput_mb_s" name="MiB/s" fill="var(--pass)" radius={[2, 2, 0, 0]} /></BarChart></ResponsiveContainer></div>)}
                </Panel>
                <Panel title="Recent runs" flush>
                  <div className={tableCls.wrap}><table className={tableCls.table}><tbody>{d.recent_runs.map((r) => (
                    <tr key={r.id} className={tableCls.tr}><td className={tableCls.td}><Link className="font-medium hover:underline" href={ws.href(`/runs/${r.id}`)}>{r.project_name}</Link><div className="text-xs text-muted">{r.trigger}{r.attempt > 1 ? ` · attempt ${r.attempt}` : ""}</div></td><td className={tableCls.td}><StatePill state={r.state} /></td><td className={tableCls.td + " text-right text-muted"}>{relative(r.queued_at)}</td></tr>))}</tbody></table></div>
                </Panel>
              </div>
              <aside className="space-y-6">
                <Panel title="Needs a decision">
                  <ul className="space-y-3 text-sm">
                    <li className="flex gap-2.5"><Flag n={d.unverified_artifacts.count} /><span><strong className="tnum">{d.unverified_artifacts.count}</strong> backup artifact(s) never restored successfully{d.unverified_artifacts.items[0] && <> (latest: <Link className="underline" href={ws.href(`/backups/${d.unverified_artifacts.items[0].id}`)}>{d.unverified_artifacts.items[0].name}</Link>)</>}.</span></li>
                    <li className="flex gap-2.5"><Flag n={d.objectives_missed.length} /><span><strong className="tnum">{d.objectives_missed.length}</strong> project(s) whose latest run missed an objective.</span></li>
                    <li className="flex gap-2.5"><Flag n={d.cleanup_pending} /><span><strong className="tnum">{d.cleanup_pending}</strong> run(s) with cleanup still pending.</span></li>
                  </ul>
                </Panel>
                <Panel title="Last 30 days">
                  {d.failure_rate_30d.finished === 0 ? <p className="text-sm text-muted">No finished runs in this period.</p> : (
                    <dl className="space-y-1.5 text-sm"><div className="flex justify-between"><dt className="text-muted">Finished runs</dt><dd className="tnum">{d.failure_rate_30d.finished}</dd></div><div className="flex justify-between"><dt className="text-muted">Succeeded</dt><dd className="tnum">{d.failure_rate_30d.succeeded}</dd></div><div className="flex justify-between"><dt className="text-muted">Failed</dt><dd className="tnum">{d.failure_rate_30d.failed}</dd></div><div className="flex justify-between"><dt className="text-muted">Needs attention</dt><dd className="tnum">{d.failure_rate_30d.needs_attention}</dd></div><div className="flex justify-between border-t border-line pt-1.5"><dt className="text-muted">Not-success rate</dt><dd className="tnum font-medium">{((d.failure_rate_30d.rate ?? 0) * 100).toFixed(0)}%</dd></div></dl>)}
                </Panel>
                {d.activity.length > 0 && <Panel title="Recent activity" flush><ul className="divide-y divide-line text-[13px]">{d.activity.map((a) => <li key={a.id} className="px-4 py-2.5"><span className="mono">{a.action}</span><div className="text-xs text-muted">{a.actor} · {relative(a.at)}{a.outcome !== "success" ? ` · ${a.outcome}` : ""}</div></li>)}</ul></Panel>}
              </aside>
            </div>
            <details className="text-xs text-muted"><summary className="cursor-pointer">What the readiness labels mean</summary><dl className="mt-2 space-y-1">{Object.values(READINESS).map((r) => <div key={r.label}><dt className="inline font-medium text-ink">{r.label}: </dt><dd className="inline">{r.meaning}</dd></div>)}</dl></details>
          </div>
        )}
      </Async>
    </>
  );
}
