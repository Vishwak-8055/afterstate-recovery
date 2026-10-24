"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { Download, ExternalLink, Printer } from "lucide-react";
import { bytes, dateTime, duration } from "@/lib/format";
import { useQuery } from "@/lib/hooks";
import type { CheckResult, Objective, Stage } from "@/lib/types";
import { useWorkspace } from "@/components/shell";
import { Async, Button, KV, Mono, OutcomePill, PageHeader, Panel, Pill, tableCls } from "@/components/ui";

interface Report {
  headline: string; generated_at: string;
  run: { id: string; project: { name: string }; state: string; summary: string | null; failure_class: string | null; failure_message: string | null; trigger: string; attempt: number; queued_at: string; started_at: string; finished_at: string; lab_scenario: string | null };
  artifact: { id: string; name: string; sha256: string; size_bytes: number; format: string; source_database: string | null; dump_server_version: string | null; backup_created_at: string | null };
  software: Record<string, string>; stages: Stage[]; metrics: Record<string, unknown>; objectives: Record<string, Objective>; checks: CheckResult[];
  summary: { counts: Record<string, number>; required_not_passing: number; project_rules_evaluated: number; complete_dataset_comparisons_passed: number; partial_checks_passed: number };
  warnings: string[]; cleanup: { state: string; tasks: { resource_type: string; state: string; attempts: number; error: string | null }[] }; evidence: { kind: string; sha256: string; size_bytes: number }[]; limitations: string[];
}

export default function ReportPage() {
  const { id } = useParams<{ id: string }>();
  const ws = useWorkspace();
  const q = useQuery<Report>(`${ws.base}/runs/${id}/report?format=json`);
  const raw = `/api/v1${ws.base}/runs/${id}/report`;
  return (
    <Async q={q}>{(r) => (
      <div className="print:text-black">
        <PageHeader title={r.headline} subtitle={<><Link className="underline" href={ws.href(`/runs/${id}`)}>Run {id.slice(0, 8)}</Link> · {r.run.project.name} · generated {dateTime(r.generated_at)}</>}
          actions={<><Button onClick={() => window.print()}><Printer className="size-4" />Print</Button><a className="inline-flex h-9 items-center gap-2 rounded-md border border-line-strong bg-s2 px-3.5 text-sm font-medium" href={`${raw}?format=json&download=true`}><Download className="size-4" />JSON</a><a className="inline-flex h-9 items-center gap-2 rounded-md border border-accent bg-accent px-3.5 text-sm font-medium text-accent-ink" target="_blank" rel="noreferrer" href={`${raw}?format=html`}><ExternalLink className="size-4" />HTML report</a></>} />
        {r.run.summary && <p className="mb-5 rounded-lg border border-line bg-s1 p-4 text-sm">{r.run.summary}{r.run.failure_class && <span className="mono mt-2 block text-xs text-muted">{r.run.failure_class}{r.run.failure_message ? `: ${r.run.failure_message}` : ""}</span>}</p>}
        <div className="grid gap-5 lg:grid-cols-2">
          <Panel title="Backup artifact"><dl><KV k="Name">{r.artifact.name}</KV><KV k="Artifact ID"><Mono>{r.artifact.id}</Mono></KV><KV k="SHA-256"><Mono>{r.artifact.sha256}</Mono></KV><KV k="Size">{bytes(r.artifact.size_bytes)}</KV><KV k="Format">{r.artifact.format}{r.artifact.source_database ? ` · ${r.artifact.source_database}` : ""}</KV><KV k="Taken on">PostgreSQL {r.artifact.dump_server_version ?? "unknown"}</KV></dl></Panel>
          <Panel title="Run and software"><dl><KV k="Trigger">{r.run.trigger}{r.run.attempt > 1 ? ` (attempt ${r.run.attempt})` : ""}</KV><KV k="Queued">{dateTime(r.run.queued_at)}</KV><KV k="Started">{dateTime(r.run.started_at)}</KV><KV k="Finished">{dateTime(r.run.finished_at)}</KV><KV k="AFTERSTATE">{r.software.afterstate}</KV><KV k="PostgreSQL">{r.software.postgresql}</KV><KV k="Isolation">{r.software.network}</KV></dl></Panel>
        </div>
        <Panel title="Recovery objectives" className="mt-5" flush><table className={tableCls.table}><thead><tr>{["Objective", "Status", "Target", "Measured", "Detail"].map((h) => <th key={h} className={tableCls.th}>{h}</th>)}</tr></thead><tbody>{Object.entries(r.objectives).map(([n, o]) => <tr key={n} className={tableCls.tr}><td className={tableCls.td}>{n}</td><td className={tableCls.td}><Pill tone={o.status === "MET" ? "pass" : o.status === "MISSED" ? "fail" : o.status === "UNKNOWN" ? "warn" : "idle"}>{o.status.replace("_", " ").toLowerCase()}</Pill></td><td className={tableCls.td + " tnum"}>{o.target_seconds != null ? duration(o.target_seconds) : "-"}</td><td className={tableCls.td + " tnum"}>{o.measured_seconds != null ? duration(o.measured_seconds) : "-"}</td><td className={tableCls.td + " text-muted"}>{o.detail}{o.basis && <div className="text-xs text-faint">Basis: {o.basis}</div>}</td></tr>)}</tbody></table></Panel>
        <Panel title="Stages and measured metrics" className="mt-5" flush>
          <div className="grid lg:grid-cols-2"><table className={tableCls.table}><tbody>{r.stages.map((s) => <tr key={s.name} className={tableCls.tr}><td className={tableCls.td}>{s.name.replaceAll("_", " ")}</td><td className={tableCls.td}>{s.state.toLowerCase()}</td><td className={tableCls.td + " tnum text-right"}>{s.duration_ms != null ? duration(s.duration_ms / 1000) : "-"}</td></tr>)}</tbody></table>
            <table className={tableCls.table}><tbody>{Object.entries(r.metrics).filter(([, v]) => typeof v === "number").map(([k, v]) => <tr key={k} className={tableCls.tr}><td className={tableCls.td}>{k.replaceAll("_", " ")}</td><td className={tableCls.td + " tnum mono text-right"}>{String(v)}</td></tr>)}</tbody></table></div>
        </Panel>
        <Panel title={`Verification checks: ${r.summary.counts.PASS} pass, ${r.summary.counts.FAIL} fail, ${r.summary.counts.INCONCLUSIVE} inconclusive, ${r.summary.counts.SKIPPED} skipped`} className="mt-5" flush>
          <div className={tableCls.wrap}><table className={tableCls.table}><tbody>{r.checks.map((c) => <tr key={c.position} className={tableCls.tr}><td className={tableCls.td}><OutcomePill outcome={c.outcome} /></td><td className={tableCls.td}>{c.name}<div className="mono text-[11px] text-faint">{c.key} · {c.coverage ?? "no coverage claim"}</div></td><td className={tableCls.td + " text-muted"}>{c.message}</td></tr>)}</tbody></table></div>
        </Panel>
        <div className="mt-5 grid gap-5 lg:grid-cols-2">
          <Panel title="Cleanup">{r.cleanup.tasks.length ? <ul className="space-y-1 text-sm">{r.cleanup.tasks.map((t, i) => <li key={i}>{t.resource_type}: {t.state.toLowerCase()} after {t.attempts} attempt(s){t.error && <span className="text-warn"> · {t.error}</span>}</li>)}</ul> : <p className="text-sm text-muted">No temporary resources were created.</p>}</Panel>
          <Panel title="Evidence">{r.evidence.map((e) => <div key={e.kind} className="flex justify-between gap-3 py-1 text-sm"><span>{e.kind.replace("_", " ")}</span><Mono className="text-xs text-muted">{e.sha256.slice(0, 24)}...</Mono></div>)}</Panel>
        </div>
        <Panel title="What this report does not show" className="mt-5"><ul className="list-disc space-y-1 pl-5 text-sm text-muted">{r.limitations.map((l) => <li key={l}>{l}</li>)}</ul></Panel>
      </div>)}</Async>
  );
}
