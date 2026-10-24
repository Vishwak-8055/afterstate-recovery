"use client";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { del, describeError, post } from "@/lib/api";
import { bytes, dateTime, duration } from "@/lib/format";
import { useQuery } from "@/lib/hooks";
import { can } from "@/lib/rbac";
import type { Artifact, RunSummary } from "@/lib/types";
import { useWorkspace } from "@/components/shell";
import { Async, Button, ConfirmDialog, KV, Mono, OutcomePill, PageHeader, Panel, StatePill, tableCls, useToast } from "@/components/ui";
import { ArtifactStatus } from "../page";

interface Detail extends Artifact {
  verifications: { id: string; kind: string; outcome: "PASS" | "FAIL"; started_at: string; duration_ms: number | null; sha256: string | null; failure_class: string | null; error: string | null; checks: { key: string; name: string; outcome: "PASS" | "FAIL" | "SKIPPED" | "INCONCLUSIVE"; message: string }[] }[];
  projects: { id: string; name: string }[]; runs: RunSummary[];
}

export default function BackupDetail() {
  const { id } = useParams<{ id: string }>();
  const ws = useWorkspace();
  const router = useRouter();
  const toast = useToast();
  const q = useQuery<Detail>(`${ws.base}/artifacts/${id}`);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const verify = async () => { setBusy(true); try { await post(`${ws.base}/artifacts/${id}/verify`); toast("Verification completed."); q.reload(); } catch (e) { toast(describeError(e), "fail"); } finally { setBusy(false); } };
  const remove = async () => { setBusy(true); try { await del(`${ws.base}/artifacts/${id}`); toast("Backup deleted."); router.push(ws.href("/backups")); } catch (e) { toast(describeError(e), "fail"); setBusy(false); setConfirm(false); } };
  return (
    <Async q={q}>{(a) => (
      <>
        <PageHeader title={a.name} subtitle={<span className="inline-flex items-center gap-3"><ArtifactStatus s={a.status} /><span className="mono text-xs">{a.original_filename}</span></span>}
          actions={<>{can(ws.role, "artifact.verify") && <Button busy={busy} onClick={verify}>Verify now</Button>}{can(ws.role, "artifact.write") && <Button variant="danger" onClick={() => setConfirm(true)}>Delete</Button>}</>} />
        {a.status === "rejected" && <p role="alert" className="mb-5 rounded-md border border-fail-line bg-fail-bg p-3 text-sm text-fail">{a.status_detail}</p>}
        <div className="grid gap-6 lg:grid-cols-2">
          <Panel title="Artifact"><dl>
            <KV k="SHA-256"><Mono>{a.sha256}</Mono></KV><KV k="Size">{bytes(a.size_bytes)}</KV><KV k="Format">{a.format}{a.compression ? ` (${a.compression})` : ""}</KV>
            <KV k="Taken on PostgreSQL">{a.dump_server_version ?? "-"}</KV><KV k="Written by">{a.dump_tool_version ?? "-"}</KV><KV k="Source database">{a.source_database ?? "-"}</KV>
            <KV k="Archive entries">{a.toc_entries ?? "-"}</KV><KV k="Backup taken at">{a.backup_created_at ? dateTime(a.backup_created_at) : <span className="text-muted">not declared (RPO cannot use it)</span>}</KV>
            <KV k="Registered">{dateTime(a.created_at)}</KV><KV k="Last verified">{dateTime(a.last_verified_at)}</KV></dl></Panel>
          <div className="space-y-6">
            <Panel title="Used by projects">{a.projects.length ? <ul className="space-y-1 text-sm">{a.projects.map((p) => <li key={p.id}><Link className="underline" href={ws.href(`/projects/${p.id}`)}>{p.name}</Link></li>)}</ul> : <p className="text-sm text-muted">No project is pinned to this backup.</p>}</Panel>
            <Panel title="Recent runs" flush>{a.runs.length ? <table className={tableCls.table}><tbody>{a.runs.map((r) => <tr key={r.id} className={tableCls.tr}><td className={tableCls.td}><Link className="hover:underline" href={ws.href(`/runs/${r.id}`)}>{r.project_name}</Link></td><td className={tableCls.td}><StatePill state={r.state} /></td><td className={tableCls.td + " tnum text-muted"}>{duration(r.metrics.recovery_seconds)}</td></tr>)}</tbody></table> : <p className="p-4 text-sm text-muted">This backup has not been restored by any run.</p>}</Panel>
          </div>
        </div>
        <Panel title="Verification history" className="mt-6" flush>
          <ul className="divide-y divide-line">{a.verifications.map((v) => (
            <li key={v.id} className="px-4 py-3"><details><summary className="flex cursor-pointer flex-wrap items-center gap-3 text-sm"><OutcomePill outcome={v.outcome} /><span>{dateTime(v.started_at)}</span><span className="text-muted">{v.kind}{v.failure_class ? ` · ${v.failure_class}` : ""}</span><span className="tnum text-muted">{duration((v.duration_ms ?? 0) / 1000)}</span></summary>
              <ul className="mt-3 divide-y divide-line rounded-md border border-line text-sm">{v.checks.map((c) => <li key={c.key} className="flex items-start justify-between gap-3 px-3 py-2"><div>{c.name}<div className="text-xs text-muted">{c.message}</div></div><OutcomePill outcome={c.outcome} /></li>)}</ul></details></li>))}</ul>
        </Panel>
        <ConfirmDialog open={confirm} title="Delete this backup?" danger busy={busy} confirmLabel="Delete backup" onClose={() => setConfirm(false)} onConfirm={remove} body={<>The file is removed from storage and projects pinned to it lose their selection. Existing run reports remain. This cannot be undone.</>} />
      </>)}
    </Async>
  );
}
