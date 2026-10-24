"use client";
import { ScrollText } from "lucide-react";
import { useState } from "react";
import { qs } from "@/lib/api";
import { dateTime } from "@/lib/format";
import { useQuery } from "@/lib/hooks";
import type { Page } from "@/lib/types";
import { useWorkspace } from "@/components/shell";
import { Async, EmptyState, Input, PageHeader, Panel, Pagination, Pill, Select, tableCls } from "@/components/ui";

interface Event { id: number; at: string; actor: string; action: string; target_type: string; target_id: string | null; outcome: string; ip: string | null; correlation_id: string | null; details: Record<string, unknown> }

export default function AuditPage() {
  const ws = useWorkspace();
  const [page, setPage] = useState(1);
  const [f, setF] = useState({ action: "", actor: "", outcome: "" });
  const set = (k: string, v: string) => { setF({ ...f, [k]: v }); setPage(1); };
  const q = useQuery<Page<Event>>(`${ws.base}/audit${qs({ page, page_size: 25, ...f })}`);
  return (
    <>
      <PageHeader title="Audit history" subtitle="Who did what, when, from where, and with what outcome. Sensitive values are redacted before they are recorded." />
      <Panel flush>
        <div className="flex flex-wrap gap-3 border-b border-line p-3">
          <Input aria-label="Action prefix" value={f.action} onChange={(e) => set("action", e.target.value)} placeholder="Action, e.g. run. or artifact." className="w-56" />
          <Input aria-label="Actor" value={f.actor} onChange={(e) => set("actor", e.target.value)} placeholder="Actor email" className="w-56" />
          <Select aria-label="Outcome" value={f.outcome} onChange={(e) => set("outcome", e.target.value)} className="w-40"><option value="">Any outcome</option><option value="success">Success</option><option value="failure">Failure</option><option value="denied">Denied</option></Select>
        </div>
        <Async q={q} isEmpty={(d) => d.total === 0} empty={<EmptyState icon={<ScrollText className="size-8" />} title="No events match">Adjust the filters.</EmptyState>}>
          {(d) => (<><div className={tableCls.wrap}><table className={tableCls.table}><thead><tr>{["When", "Actor", "Action", "Target", "Outcome"].map((h) => <th key={h} className={tableCls.th}>{h}</th>)}</tr></thead><tbody>{d.items.map((e) => (
            <tr key={e.id} className={tableCls.tr}>
              <td className={tableCls.td + " whitespace-nowrap text-muted"}>{dateTime(e.at)}</td><td className={tableCls.td}>{e.actor}{e.ip && <div className="mono text-[11px] text-faint">{e.ip}</div>}</td>
              <td className={tableCls.td}><span className="mono">{e.action}</span>{Object.keys(e.details).length > 0 && <details className="mt-1"><summary className="cursor-pointer text-xs text-muted">Details</summary><pre className="mono mt-1 max-w-md overflow-x-auto rounded bg-s2 p-2 text-[11px]">{JSON.stringify(e.details, null, 1)}</pre></details>}</td>
              <td className={tableCls.td + " text-muted"}>{e.target_type}{e.target_id && <div className="mono text-[11px]">{e.target_id.slice(0, 8)}</div>}</td>
              <td className={tableCls.td}><Pill tone={e.outcome === "success" ? "pass" : e.outcome === "denied" ? "warn" : "fail"} icon={false}>{e.outcome}</Pill></td>
            </tr>))}</tbody></table></div><Pagination page={d.page} pageSize={d.page_size} total={d.total} onPage={setPage} /></>)}
        </Async>
      </Panel>
    </>
  );
}
