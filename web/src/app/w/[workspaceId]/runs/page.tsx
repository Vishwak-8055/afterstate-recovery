"use client";
import Link from "next/link";
import { GitCompare, History } from "lucide-react";
import { useState } from "react";
import { qs } from "@/lib/api";
import { dateTime, duration } from "@/lib/format";
import { useQuery } from "@/lib/hooks";
import type { Page, Project, RunState, RunSummary } from "@/lib/types";
import { useWorkspace } from "@/components/shell";
import { Async, Button, EmptyState, LinkButton, PageHeader, Panel, Pagination, Select, StatePill, tableCls } from "@/components/ui";

const STATES: RunState[] = ["SUCCEEDED", "FAILED", "NEEDS_ATTENTION", "NEEDS_CLEANUP", "CANCELLED", "QUEUED", "RESTORING", "VALIDATING"];

export default function RunsPage() {
  const ws = useWorkspace();
  const [page, setPage] = useState(1);
  const [f, setF] = useState({ project_id: "", state: "", trigger: "", lab: false });
  const [picked, setPicked] = useState<string[]>([]);
  const projects = useQuery<Page<Project>>(`${ws.base}/projects?page_size=100`);
  const q = useQuery<Page<RunSummary>>(`${ws.base}/runs${qs({ page, page_size: 15, project_id: f.project_id, state: f.state ? [f.state] : undefined, trigger: f.trigger, include_lab: f.lab })}`, { pollMs: 4000 });
  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p.slice(-1), id]));
  const set = (k: string, v: string | boolean) => { setF({ ...f, [k]: v }); setPage(1); };
  return (
    <>
      <PageHeader title="Recovery history" subtitle="Every run with its verdict, measured recovery time and cleanup state. Select two runs to compare them." actions={<LinkButton href={picked.length === 2 ? ws.href(`/runs/compare?a=${picked[0]}&b=${picked[1]}`) : "#"} aria-disabled={picked.length !== 2} className={picked.length !== 2 ? "pointer-events-none opacity-50" : ""}><GitCompare className="size-4" />Compare selected ({picked.length}/2)</LinkButton>} />
      <Panel flush>
        <div className="flex flex-wrap items-center gap-3 border-b border-line p-3">
          <Select aria-label="Project" value={f.project_id} onChange={(e) => set("project_id", e.target.value)} className="w-52"><option value="">All projects</option>{projects.data?.items.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</Select>
          <Select aria-label="State" value={f.state} onChange={(e) => set("state", e.target.value)} className="w-44"><option value="">Any state</option>{STATES.map((s) => <option key={s} value={s}>{s.replace("_", " ").toLowerCase()}</option>)}</Select>
          <Select aria-label="Trigger" value={f.trigger} onChange={(e) => set("trigger", e.target.value)} className="w-36"><option value="">Any trigger</option>{["manual", "schedule", "retry", "lab"].map((t) => <option key={t}>{t}</option>)}</Select>
          <label className="flex items-center gap-2 text-sm text-muted"><input type="checkbox" checked={f.lab} onChange={(e) => set("lab", e.target.checked)} />Include Recovery Lab runs</label>
          {picked.length > 0 && <Button size="sm" variant="ghost" onClick={() => setPicked([])}>Clear selection</Button>}
        </div>
        <Async q={q} isEmpty={(d) => d.total === 0} empty={<EmptyState icon={<History className="size-8" />} title="No runs match" action={<LinkButton href={ws.href("/projects")}>Open projects</LinkButton>}>Start a run from a project, or from the Recovery Lab.</EmptyState>}>
          {(d) => (<><div className={tableCls.wrap}><table className={tableCls.table}>
            <thead><tr><th className={tableCls.th}><span className="sr-only">Select</span></th>{["Run", "Result", "Recovery time", "Backup", "Started"].map((h) => <th key={h} className={tableCls.th}>{h}</th>)}</tr></thead>
            <tbody>{d.items.map((r) => (
              <tr key={r.id} className={tableCls.tr}>
                <td className={tableCls.td}><input type="checkbox" aria-label={`Select run ${r.id.slice(0, 8)} for comparison`} checked={picked.includes(r.id)} onChange={() => toggle(r.id)} /></td>
                <td className={tableCls.td}><Link className="font-medium hover:underline" href={ws.href(`/runs/${r.id}`)}>{r.project_name}</Link><div className="mono text-xs text-muted">{r.id.slice(0, 8)} · {r.trigger}{r.attempt > 1 ? ` · attempt ${r.attempt}` : ""}</div></td>
                <td className={tableCls.td}><StatePill state={r.state} />{r.failure_class && r.state !== "SUCCEEDED" && <div className="mono mt-1 text-[11px] text-muted">{r.failure_class}</div>}</td>
                <td className={tableCls.td + " tnum"}>{duration(r.metrics.recovery_seconds)}{Object.entries(r.objectives).filter(([, s]) => s === "MISSED").map(([k]) => <div key={k} className="text-xs text-fail">{k} missed</div>)}</td>
                <td className={tableCls.td + " max-w-44 truncate text-muted"}>{r.artifact_name}</td><td className={tableCls.td + " text-muted"}>{dateTime(r.started_at ?? r.queued_at)}</td>
              </tr>))}</tbody></table></div><Pagination page={d.page} pageSize={d.page_size} total={d.total} onPage={setPage} /></>)}
        </Async>
      </Panel>
    </>
  );
}
