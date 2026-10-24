"use client";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { duration } from "@/lib/format";
import { useQuery } from "@/lib/hooks";
import type { Outcome, RunSummary } from "@/lib/types";
import { useWorkspace } from "@/components/shell";
import { Async, OutcomePill, PageHeader, Panel, StatePill, tableCls } from "@/components/ui";

interface Cmp { a: RunSummary; b: RunSummary; metric_deltas: Record<string, { a: number | null; b: number | null; delta: number | null; percent: number | null }>; check_changes: { check: string; key: string; a: Outcome | null; b: Outcome | null }[]; same_artifact: boolean; same_project: boolean }
const LABEL: Record<string, string> = { recovery_seconds: "Recovery time (s)", restore_seconds: "Restore (s)", validation_seconds: "Validation (s)", total_seconds: "Total run (s)", restore_throughput_mb_s: "Restore throughput (MiB/s)", restored_database_bytes: "Restored database (bytes)", artifact_size_bytes: "Backup size (bytes)" };

function Inner() {
  const ws = useWorkspace();
  const sp = useSearchParams();
  const q = useQuery<Cmp>(`${ws.base}/runs/compare?a=${sp.get("a")}&b=${sp.get("b")}`);
  return <Async q={q}>{(c) => (
    <>
      <PageHeader title="Compare runs" subtitle={`${c.same_project ? "Same project" : "Different projects"} · ${c.same_artifact ? "same backup" : "different backups"}. Differences are computed from recorded results.`} />
      <div className="mb-5 grid gap-4 sm:grid-cols-2">{[c.a, c.b].map((r, i) => <Panel key={r.id} title={i === 0 ? "Run A" : "Run B"}><Link className="font-medium hover:underline" href={ws.href(`/runs/${r.id}`)}>{r.project_name}</Link> <StatePill state={r.state} /><p className="mono mt-1 text-xs text-muted">{r.id.slice(0, 8)} · {r.started_at}</p></Panel>)}</div>
      <Panel title="Measured metrics" flush className="mb-5"><table className={tableCls.table}><thead><tr><th className={tableCls.th}>Metric</th><th className={tableCls.th + " text-right"}>A</th><th className={tableCls.th + " text-right"}>B</th><th className={tableCls.th + " text-right"}>Change</th></tr></thead><tbody>{Object.entries(c.metric_deltas).map(([k, d]) => (
        <tr key={k} className={tableCls.tr}><td className={tableCls.td}>{LABEL[k] ?? k}</td><td className={tableCls.td + " tnum text-right"}>{d.a ?? "-"}</td><td className={tableCls.td + " tnum text-right"}>{d.b ?? "-"}</td><td className={tableCls.td + " tnum text-right"}>{d.delta == null ? "-" : `${d.delta > 0 ? "+" : ""}${d.delta}${d.percent != null ? ` (${d.percent > 0 ? "+" : ""}${d.percent}%)` : ""}`}</td></tr>))}</tbody></table></Panel>
      <Panel title={`Checks whose result changed (${c.check_changes.length})`} flush>{c.check_changes.length === 0 ? <p className="p-4 text-sm text-muted">Every check has the same result in both runs.</p> : <table className={tableCls.table}><tbody>{c.check_changes.map((x) => <tr key={x.key + x.check} className={tableCls.tr}><td className={tableCls.td}>{x.check}</td><td className={tableCls.td}>{x.a ? <OutcomePill outcome={x.a} /> : <span className="text-muted">not present</span>}</td><td className={tableCls.td}>{x.b ? <OutcomePill outcome={x.b} /> : <span className="text-muted">not present</span>}</td></tr>)}</tbody></table>}</Panel>
    </>)}</Async>;
}
export default function ComparePage() { return <Suspense><Inner /></Suspense>; }
void duration;
