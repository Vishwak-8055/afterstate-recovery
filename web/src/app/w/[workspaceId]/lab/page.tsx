"use client";
import { FlaskConical, Play, RotateCcw } from "lucide-react";
import { useState } from "react";
import { describeError, post } from "@/lib/api";
import { useQuery } from "@/lib/hooks";
import { can } from "@/lib/rbac";
import type { LabScenario } from "@/lib/types";
import { RunWorkspace } from "@/components/run-workspace";
import { useWorkspace } from "@/components/shell";
import { Async, Button, cn, PageHeader, Panel, Pill, type Tone, useToast } from "@/components/ui";

const STATUS: Record<LabScenario["status"], { label: string; tone: Tone }> = { not_run: { label: "Not run", tone: "idle" }, running: { label: "Running", tone: "active" }, matched: { label: "Matched expectation", tone: "pass" }, mismatch: { label: "Unexpected result", tone: "fail" } };

export default function LabPage() {
  const ws = useWorkspace();
  const toast = useToast();
  const q = useQuery<{ items: LabScenario[]; isolation: string }>(`${ws.base}/lab/scenarios`, { pollMs: 2000 });
  const [sel, setSel] = useState("successful_restore");
  const [busy, setBusy] = useState("");
  const [attempt, setAttempt] = useState(-1);
  const act = async (key: string, what: "run" | "reset") => {
    setBusy(key + what);
    try { await post(`${ws.base}/lab/scenarios/${key}/${what}`); toast(what === "run" ? "Scenario started on the real engine." : "Scenario resources removed."); setAttempt(-1); q.reload(); }
    catch (e) { toast(describeError(e), "fail"); } finally { setBusy(""); }
  };
  return (
    <>
      <PageHeader title="Recovery Lab" subtitle="Ten scenarios that run the real recovery engine on real PostgreSQL archives in disposable environments. Failures are produced by damaged archives, wrong configuration or injected faults. Nothing is a simulated status." />
      <Async q={q}>{(d) => {
        const s = d.items.find((x) => x.key === sel) ?? d.items[0]!;
        const run = s.runs.at(attempt) ?? s.runs.at(-1);
        const writable = can(ws.role, "lab.run");
        return (
          <div className="grid gap-6 xl:grid-cols-[19rem_1fr]">
            <nav aria-label="Scenarios" className="space-y-1.5">
              {d.items.map((x, i) => (
                <button key={x.key} onClick={() => { setSel(x.key); setAttempt(-1); }} aria-current={x.key === s.key} className={cn("w-full rounded-lg border p-3 text-left transition-colors", x.key === s.key ? "border-line-strong bg-s3" : "border-line bg-s1 hover:bg-s2")}>
                  <div className="flex items-center justify-between gap-2"><span className="mono text-[11px] text-faint">{String(i + 1).padStart(2, "0")}</span><Pill tone={STATUS[x.status].tone} className="px-1.5">{STATUS[x.status].label}</Pill></div>
                  <div className="mt-1 text-sm font-medium">{x.title}</div>
                </button>))}
            </nav>
            <div className="min-w-0 space-y-5">
              <Panel title={<span className="flex items-center gap-2"><FlaskConical className="size-4" />{s.title}</span>} actions={writable && <div className="flex gap-2"><Button size="sm" variant="primary" busy={busy === s.key + "run"} disabled={s.status === "running"} onClick={() => act(s.key, "run")}><Play className="size-3.5" />{s.runs.length ? "Run again" : "Run scenario"}</Button><Button size="sm" busy={busy === s.key + "reset"} disabled={s.status === "running" || s.runs.length === 0} onClick={() => act(s.key, "reset")}><RotateCcw className="size-3.5" />Reset</Button></div>}>
                <p className="text-sm">{s.summary}</p>
                <div className="mt-4 grid gap-5 md:grid-cols-2">
                  <div><h3 className="mb-1.5 text-xs font-medium text-muted">Initial conditions</h3><ul className="list-disc space-y-1 pl-4 text-[13px]">{s.initial_conditions.map((c) => <li key={c}>{c}</li>)}</ul>{s.fault && <p className="mt-3 rounded-md border border-warn-line bg-warn-bg px-3 py-2 text-[13px] text-warn"><strong>Injected fault:</strong> {s.fault}</p>}</div>
                  <div><h3 className="mb-1.5 text-xs font-medium text-muted">Expected</h3><p className="text-[13px]">{s.expected}</p><h3 className="mt-3 mb-1.5 text-xs font-medium text-muted">Observed</h3><p className="text-[13px]"><Pill tone={STATUS[s.status].tone}>{STATUS[s.status].label}</Pill> <span className="text-muted">{s.note}</span></p></div>
                </div>
                {!writable && <p className="mt-4 text-xs text-muted">Your role can watch scenarios but not start them. Operators and above can run the Lab.</p>}
              </Panel>
              {s.runs.length > 1 && <div className="flex gap-2" role="group" aria-label="Attempts">{s.runs.map((r, i) => <Button key={r.id} size="sm" variant={(attempt === -1 ? s.runs.length - 1 : attempt) === i ? "primary" : "secondary"} onClick={() => setAttempt(i)}>Attempt {r.attempt}: {r.state.replace("_", " ").toLowerCase()}</Button>)}</div>}
              {run ? <RunWorkspace key={run.id} runId={run.id} showTitle={false} compact /> : <Panel><p className="py-8 text-center text-sm text-muted">Run the scenario to watch the real stages, checks, metrics and cleanup appear here.</p></Panel>}
            </div>
          </div>);
      }}</Async>
    </>
  );
}
