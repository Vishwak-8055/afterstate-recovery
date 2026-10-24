"use client";
import Link from "next/link";
import { CircleCheck, CircleDashed, CircleX, ExternalLink, FileText, LoaderCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { describeError, get, post } from "@/lib/api";
import { bytes, dateTime, duration } from "@/lib/format";
import { useQuery } from "@/lib/hooks";
import { can } from "@/lib/rbac";
import type { CheckResult, EventsResponse, Outcome, RunDetail, RunEvent, Objective } from "@/lib/types";
import { ObjectiveCard, RecoveryRuler, segmentsFrom } from "./recovery";
import { useWorkspace } from "./shell";
import { Async, Button, cn, KV, Mono, OutcomePill, Panel, Pill, StatePill, tableCls, Tabs, useToast } from "./ui";

const DONE = ["SUCCEEDED", "FAILED", "CANCELLED", "NEEDS_ATTENTION"];
const verdictTone = { SUCCEEDED: "border-pass-line bg-pass-bg text-pass", FAILED: "border-fail-line bg-fail-bg text-fail", NEEDS_ATTENTION: "border-warn-line bg-warn-bg text-warn", NEEDS_CLEANUP: "border-warn-line bg-warn-bg text-warn", CANCELLED: "border-idle-line bg-idle-bg text-idle" } as Record<string, string>;
const HEADLINE = { SUCCEEDED: "Recovery verified", FAILED: "Recovery failed", NEEDS_ATTENTION: "Recovery not verified", NEEDS_CLEANUP: "Result recorded, cleanup incomplete", CANCELLED: "Run cancelled" } as Record<string, string>;

const stageNote = (s: { error: string | null; state: string; details: Record<string, unknown> }): string => s.error ?? (s.state === "SKIPPED" && s.details.reason ? String(s.details.reason) : "");

function StageIcon({ state }: { state: string }) {
  if (state === "SUCCEEDED") return <CircleCheck aria-label="succeeded" className="size-4 text-pass" />;
  if (state === "FAILED") return <CircleX aria-label="failed" className="size-4 text-fail" />;
  if (state === "RUNNING") return <LoaderCircle aria-label="running" className="size-4 text-active motion-safe:animate-spin" />;
  return <CircleDashed aria-label={state.toLowerCase()} className="size-4 text-faint" />;
}

function useEvents(base: string, runId: string, active: boolean) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [meta, setMeta] = useState<EventsResponse>();
  const after = useRef(0);
  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        const r = await get<EventsResponse>(`${base}/runs/${runId}/events?after=${after.current}&limit=500`);
        if (stop) return;
        if (r.events.length) { after.current = r.next_after; setEvents((e) => [...e, ...r.events].slice(-3000)); }
        setMeta(r);
        if (r.terminal && r.events.length === 0) return;
      } catch { /* retry */ }
      timer = setTimeout(tick, active ? 1000 : 4000);
    };
    tick();
    return () => { stop = true; clearTimeout(timer); };
  }, [base, runId, active]);
  return { events, meta };
}

function LogView({ events }: { events: RunEvent[] }) {
  const box = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);
  const [level, setLevel] = useState<"all" | "warn">("all");
  useEffect(() => { if (follow && box.current) box.current.scrollTop = box.current.scrollHeight; }, [events, follow]);
  const shown = level === "warn" ? events.filter((e) => e.level !== "INFO") : events;
  const color = (e: RunEvent) => e.kind === "fault" ? "text-warn" : e.level === "ERROR" ? "text-fail" : e.level === "WARN" ? "text-warn" : e.kind === "state" || e.kind === "stage" ? "text-ink" : "text-muted";
  return (
    <Panel title={`Execution log (${events.length})`} flush actions={<div className="flex items-center gap-3 text-xs text-muted"><label className="flex items-center gap-1.5"><input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />Follow</label><label className="flex items-center gap-1.5"><input type="checkbox" checked={level === "warn"} onChange={(e) => setLevel(e.target.checked ? "warn" : "all")} />Warnings only</label></div>}>
      <div ref={box} role="log" aria-live="off" tabIndex={0} className="mono max-h-80 overflow-auto bg-bg/60 p-3 text-[12px] leading-5">
        {shown.length === 0 && <p className="text-faint">Waiting for the first event.</p>}
        {shown.map((e) => <div key={e.id} className={cn("grid grid-cols-[5.2rem_1fr] gap-3 whitespace-pre-wrap", color(e))}><time className="text-faint">{new Date(e.ts).toLocaleTimeString([], { hour12: false })}</time><span>{e.kind === "fault" ? "FAULT " : ""}{e.message}</span></div>)}
      </div>
    </Panel>
  );
}

function Checks({ results }: { results: CheckResult[] }) {
  const [filter, setFilter] = useState<"all" | Outcome>("all");
  const counts = results.reduce((a, r) => ({ ...a, [r.outcome]: (a[r.outcome] ?? 0) + 1 }), {} as Record<string, number>);
  const rows = filter === "all" ? results : results.filter((r) => r.outcome === filter);
  return (
    <Panel title={`Verification checks (${results.length})`} flush>
      <div className="flex flex-wrap gap-2 border-b border-line p-3" role="group" aria-label="Filter checks">
        {(["all", "FAIL", "INCONCLUSIVE", "SKIPPED", "PASS"] as const).map((f) => <Button key={f} size="sm" variant={filter === f ? "primary" : "secondary"} aria-pressed={filter === f} onClick={() => setFilter(f)}>{f === "all" ? "All" : f.charAt(0) + f.slice(1).toLowerCase()} <span className="tnum opacity-70">{f === "all" ? results.length : counts[f] ?? 0}</span></Button>)}
      </div>
      {rows.length === 0 ? <p className="p-5 text-sm text-muted">No checks in this view.</p> : <div className={tableCls.wrap}><table className={tableCls.table}>
        <thead><tr>{["Result", "Check", "Detail", "Expected", "Actual", "Coverage"].map((h) => <th key={h} className={tableCls.th}>{h}</th>)}</tr></thead>
        <tbody>{rows.map((r) => (
          <tr key={r.position} className={tableCls.tr}>
            <td className={tableCls.td}><OutcomePill outcome={r.outcome} />{!r.required && <div className="mt-1 text-[11px] text-faint">optional</div>}</td>
            <td className={tableCls.td}>{r.name}<div className="mono text-[11px] text-faint">{r.key}</div></td>
            <td className={tableCls.td + " max-w-sm text-muted"}>{r.message}</td>
            <td className={tableCls.td + " mono max-w-48 text-[11px] break-all text-muted"}>{r.expected == null ? "-" : JSON.stringify(r.expected).slice(0, 160)}</td>
            <td className={tableCls.td + " mono max-w-48 text-[11px] break-all text-muted"}>{r.actual == null ? "-" : JSON.stringify(r.actual).slice(0, 160)}</td>
            <td className={tableCls.td + " text-xs text-muted"}>{r.coverage ?? "-"}</td>
          </tr>))}</tbody></table></div>}
    </Panel>
  );
}

export function RunWorkspace({ runId, showTitle = true, compact = false }: { runId: string; showTitle?: boolean; compact?: boolean }) {
  const ws = useWorkspace();
  const toast = useToast();
  const q = useQuery<RunDetail>(`${ws.base}/runs/${runId}`, { pollMs: 1500, stop: (d) => DONE.includes(d.state) });
  const live = !q.data || !DONE.includes(q.data.state);
  const { events, meta } = useEvents(ws.base, runId, live);
  const [tab, setTab] = useState<"checks" | "log">("checks");
  const act = async (path: string, ok: string) => { try { await post(`${ws.base}/runs/${runId}/${path}`); toast(ok); q.reload(); } catch (e) { toast(describeError(e), "fail"); } };
  return (
    <Async q={q}>{(r) => {
      const prog = (meta?.progress ?? r.progress) as { items_done?: number; items_total?: number };
      const pct = prog.items_total ? Math.min(100, Math.round(((prog.items_done ?? 0) / prog.items_total) * 100)) : null;
      const rto = (r.config.objectives as { rto_seconds?: number } | undefined)?.rto_seconds ?? null;
      const objectives = Object.entries(r.objectives ?? {}).filter(([, o]) => o.status !== "NOT_CONFIGURED") as [string, Objective][];
      const state = r.state === "NEEDS_CLEANUP" ? "NEEDS_CLEANUP" : r.state;
      return (
        <div className="space-y-5">
          {showTitle && <div className="flex flex-wrap items-start justify-between gap-4">
            <div><div className="flex flex-wrap items-center gap-3"><h1 className="text-2xl font-semibold tracking-tight">{r.project_name}</h1><StatePill state={r.state} /></div>
              <p className="mt-1 text-sm text-muted"><Mono>run {r.id.slice(0, 8)}</Mono> · {r.trigger}{r.attempt > 1 ? ` · attempt ${r.attempt}` : ""}{r.lab_scenario ? ` · Recovery Lab: ${r.lab_scenario}` : ""} · started {dateTime(r.started_at ?? r.queued_at)}{r.retry_of && <> · retry of <Link className="underline" href={ws.href(`/runs/${r.retry_of}`)}>{r.retry_of.slice(0, 8)}</Link></>}</p></div>
            <div className="flex gap-2">
              {can(ws.role, "run.cancel") && ["QUEUED", "PREPARING", "RESTORING", "VALIDATING"].includes(r.state) && <Button variant="danger" disabled={r.cancel_requested} onClick={() => act("cancel", "Cancellation requested.")}>{r.cancel_requested ? "Cancelling..." : "Cancel run"}</Button>}
              {can(ws.role, "run.cancel") && r.state === "NEEDS_CLEANUP" && <Button onClick={() => act("cleanup", "Cleanup retry requested.")}>Retry cleanup</Button>}
              {r.evidence.some((e) => e.kind === "report_json") && <><Link className="inline-flex h-9 items-center gap-2 rounded-md border border-line-strong bg-s2 px-3.5 text-sm font-medium hover:bg-s3" href={ws.href(`/runs/${r.id}/report`)}><FileText className="size-4" />Report</Link></>}
            </div>
          </div>}

          {!live || r.state === "NEEDS_CLEANUP" ? (
            <div role="status" className={cn("rounded-lg border p-4", verdictTone[r.state] ?? verdictTone.CANCELLED)}>
              <div className="text-base font-semibold">{HEADLINE[state]}{r.state === "NEEDS_CLEANUP" && r.settled_state ? ` (would settle as ${r.settled_state.toLowerCase().replace("_", " ")})` : ""}</div>
              {r.summary && <p className="mt-1 text-sm">{r.summary}</p>}
              {r.failure_class && <p className="mono mt-2 text-xs">{r.failure_class}{r.failure_message ? `: ${r.failure_message}` : ""}</p>}
            </div>
          ) : (
            <div className="rounded-lg border border-active-line bg-active-bg p-4 text-active">
              <div className="flex items-center justify-between gap-4"><div className="text-sm font-semibold">{meta?.current_stage ? meta.current_stage.replaceAll("_", " ") : "Waiting for a worker"}</div>{pct != null && r.state === "RESTORING" && <span className="mono text-xs">{prog.items_done}/{prog.items_total} archive items</span>}</div>
              <div className="mt-3 h-1.5 overflow-hidden rounded bg-bg/50" role="progressbar" aria-label="Restore progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct ?? undefined}><div className={cn("h-full bg-active transition-[width] duration-300", pct == null && "working w-full")} style={pct != null ? { width: `${pct}%` } : undefined} /></div>
              <p className="mt-2 text-xs opacity-80">Progress is counted from real pg_restore events, not estimated.</p>
            </div>
          )}

          <Panel title="Recovery timeline"><RecoveryRuler segments={segmentsFrom(r.stages)} rto={rto} />{r.metrics.recovery_seconds != null && <p className="mt-3 text-xs text-muted">Recovery time counts artifact verification, provisioning, restore and validation. Cleanup and report generation are excluded.</p>}</Panel>
          {objectives.length > 0 && <div className="grid gap-3 sm:grid-cols-2">{objectives.map(([n, o]) => <ObjectiveCard key={n} name={n} o={o} />)}</div>}

          <div className={compact ? "space-y-5" : "grid gap-5 lg:grid-cols-[22rem_1fr]"}>
            <div className={compact ? "grid items-start gap-5 md:grid-cols-2 xl:grid-cols-3" : "space-y-5"}>
              <Panel title="Stages" flush><ul className="divide-y divide-line">{r.stages.map((s) => <li key={s.name} className="flex items-start gap-3 px-4 py-2.5 text-sm"><span className="mt-0.5"><StageIcon state={s.state} /></span><div className="min-w-0 flex-1"><div className="flex justify-between gap-2"><span>{s.name.replaceAll("_", " ")}</span><span className="tnum text-muted">{s.duration_ms != null ? duration(s.duration_ms / 1000) : ""}</span></div>{stageNote(s) && <p className="text-xs text-muted">{stageNote(s)}</p>}</div></li>)}
                {r.stages.length === 0 && <li className="px-4 py-3 text-sm text-muted">No stage has started.</li>}</ul></Panel>
              <Panel title="Cleanup"><div className="mb-2"><Pill tone={r.cleanup.state === "DONE" ? "pass" : r.cleanup.state === "FAILED" ? "warn" : "idle"}>{r.cleanup.state.replace("_", " ").toLowerCase()}</Pill></div>
                {r.cleanup.tasks.length ? <ul className="space-y-2 text-sm">{r.cleanup.tasks.map((t, i) => <li key={i}><span className="mono text-xs">{t.resource}</span> <span className="text-muted">· {t.state.toLowerCase()} · {t.attempts} attempt(s)</span>{t.error && <p className="text-xs text-warn">{t.error}</p>}</li>)}</ul> : <p className="text-sm text-muted">No temporary resources were created.</p>}</Panel>
              <Panel title="Environment"><dl className="text-xs"><KV k="Isolation">{String(r.environment.network ?? "-")}</KV><KV k="PostgreSQL">{String(r.environment.postgresql ?? "-")}</KV><KV k="Restored size">{bytes(r.metrics.restored_database_bytes as number | null)}</KV><KV k="Throughput">{r.metrics.restore_throughput_mb_s ? `${r.metrics.restore_throughput_mb_s} MiB/s` : "-"}</KV></dl></Panel>
              {r.fault && <Panel title="Injected fault (Recovery Lab)"><pre className="mono text-xs whitespace-pre-wrap text-warn">{JSON.stringify(r.fault, null, 1)}</pre></Panel>}
            </div>
            <div className="min-w-0 space-y-4">
              <Tabs value={tab} onChange={setTab} tabs={[{ id: "checks", label: "Checks", count: r.results.length }, { id: "log", label: "Execution log", count: events.length }]} />
              {tab === "checks" ? (r.results.length ? <Checks results={r.results} /> : <Panel><p className="text-sm text-muted">Checks appear here as they complete.</p></Panel>) : <LogView events={events} />}
              {r.evidence.length > 0 && <Panel title="Evidence" flush><table className={tableCls.table}><tbody>{r.evidence.map((e) => <tr key={e.kind} className={tableCls.tr}><td className={tableCls.td}>{e.kind.replace("_", " ")}</td><td className={tableCls.td + " mono text-xs text-muted"}>{e.sha256.slice(0, 20)}...</td><td className={tableCls.td + " tnum text-right text-muted"}>{bytes(e.size_bytes)}</td><td className={tableCls.td + " text-right"}>{e.kind === "report_html" && <a className="inline-flex items-center gap-1 underline" target="_blank" rel="noreferrer" href={`/api/v1${ws.base}/runs/${r.id}/report?format=html`}>Open <ExternalLink className="size-3" /></a>}{e.kind === "execution_log" && <a className="underline" href={`/api/v1${ws.base}/runs/${r.id}/evidence/execution_log`} target="_blank" rel="noreferrer">Open</a>}</td></tr>)}</tbody></table></Panel>}
            </div>
          </div>
        </div>);
    }}</Async>
  );
}
