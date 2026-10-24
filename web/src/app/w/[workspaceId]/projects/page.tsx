"use client";
import Link from "next/link";
import { ListChecks, Plus } from "lucide-react";
import { useState } from "react";
import { qs } from "@/lib/api";
import { relative } from "@/lib/format";
import { useQuery } from "@/lib/hooks";
import { can } from "@/lib/rbac";
import type { Page, Project } from "@/lib/types";
import { useWorkspace } from "@/components/shell";
import { Async, EmptyState, Input, LinkButton, PageHeader, Panel, Pagination, StatePill, tableCls } from "@/components/ui";

export default function ProjectsPage() {
  const ws = useWorkspace();
  const [page, setPage] = useState(1);
  const [text, setText] = useState("");
  const q = useQuery<Page<Project>>(`${ws.base}/projects${qs({ page, page_size: 15, q: text })}`);
  return (
    <>
      <PageHeader title="Recovery projects" subtitle="A project says which backup to restore, how, what must be true afterwards, and what objectives apply." actions={can(ws.role, "project.write") && <LinkButton variant="primary" href={ws.href("/projects/new")}><Plus className="size-4" />New project</LinkButton>} />
      <Panel flush>
        <div className="border-b border-line p-3"><Input aria-label="Search projects" value={text} onChange={(e) => { setText(e.target.value); setPage(1); }} placeholder="Search projects" className="max-w-sm" /></div>
        <Async q={q} isEmpty={(d) => d.total === 0} empty={<EmptyState icon={<ListChecks className="size-8" />} title={text ? "No projects match" : "No recovery projects yet"} action={!text && can(ws.role, "project.write") ? <LinkButton variant="primary" href={ws.href("/projects/new")}>Create a project</LinkButton> : undefined}>{text ? "Try a different search." : "A project connects a backup to the checks that prove it recovers correctly."}</EmptyState>}>
          {(d) => (<><div className={tableCls.wrap}><table className={tableCls.table}>
            <thead><tr>{["Project", "Backup source", "Rules", "Objectives", "Latest run"].map((h) => <th key={h} className={tableCls.th}>{h}</th>)}</tr></thead>
            <tbody>{d.items.map((p) => (
              <tr key={p.id} className={tableCls.tr}>
                <td className={tableCls.td}><Link className="font-medium hover:underline" href={ws.href(`/projects/${p.id}`)}>{p.name}</Link>{p.description && <div className="max-w-sm truncate text-xs text-muted">{p.description}</div>}</td>
                <td className={tableCls.td + " text-muted"}>{p.artifact_id ? "Pinned backup" : p.artifact_label ? <>Latest <span className="mono">{p.artifact_label}</span></> : <span className="text-warn">none selected</span>}</td>
                <td className={tableCls.td + " tnum"}>{p.rule_count}</td>
                <td className={tableCls.td + " tnum text-muted"}>{p.rto_seconds ? `RTO ${p.rto_seconds}s` : "no RTO"} · {p.rpo_seconds ? `RPO ${p.rpo_seconds}s` : "no RPO"}</td>
                <td className={tableCls.td}>{p.latest_run ? <Link href={ws.href(`/runs/${p.latest_run.id}`)} className="inline-flex items-center gap-2"><StatePill state={p.latest_run.state} /><span className="text-xs text-muted">{relative(p.latest_run.queued_at)}</span></Link> : <span className="text-muted">never run</span>}</td>
              </tr>))}</tbody></table></div><Pagination page={d.page} pageSize={d.page_size} total={d.total} onPage={setPage} /></>)}
        </Async>
      </Panel>
    </>
  );
}
