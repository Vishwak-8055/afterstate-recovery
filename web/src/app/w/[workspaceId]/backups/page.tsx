"use client";
import Link from "next/link";
import { Archive, Search, Upload } from "lucide-react";
import { useState } from "react";
import { api, describeError, qs } from "@/lib/api";
import { bytes, relative } from "@/lib/format";
import { useQuery } from "@/lib/hooks";
import { can } from "@/lib/rbac";
import type { Artifact, Page } from "@/lib/types";
import { useWorkspace } from "@/components/shell";
import { Async, Button, Dialog, EmptyState, Field, Input, OutcomePill, PageHeader, Panel, Pagination, Pill, Select, tableCls, useToast } from "@/components/ui";

export const ArtifactStatus = ({ s }: { s: Artifact["status"] }) => s === "verified" ? <Pill tone="pass">Verified</Pill> : s === "rejected" ? <Pill tone="fail">Rejected</Pill> : <Pill tone="warn">Unverified</Pill>;
interface UploadResult extends Artifact { verification: { ok: boolean; message: string | null; checks: { key: string; name: string; outcome: "PASS" | "FAIL" | "SKIPPED" | "INCONCLUSIVE"; message: string }[] } }

function UploadDialog({ open, onClose, onDone }: { open: boolean; onClose: () => void; onDone: () => void }) {
  const ws = useWorkspace();
  const toast = useToast();
  const [file, setFile] = useState<File | null>(null);
  const [f, setF] = useState({ name: "", label: "", created: "", sha: "", notes: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<UploadResult>();
  const submit = async () => {
    if (!file) return;
    setBusy(true); setError("");
    try {
      const params = qs({ name: f.name || file.name, label: f.label, filename: file.name, expected_sha256: f.sha.trim(), backup_created_at: f.created ? new Date(f.created).toISOString() : undefined, notes: f.notes });
      const r = await api<UploadResult>(`${ws.base}/artifacts${params}`, { method: "POST", rawBody: file, headers: { "Content-Type": "application/octet-stream" } });
      setResult(r); onDone();
      toast(r.status === "verified" ? "Backup registered and verified." : "Backup registered but rejected by verification.", r.status === "verified" ? "pass" : "warn");
    } catch (e) { setError(describeError(e)); } finally { setBusy(false); }
  };
  const close = () => { setResult(undefined); setFile(null); setError(""); onClose(); };
  return (
    <Dialog open={open} onClose={close} title="Register a backup" wide footer={result ? <Button variant="primary" onClick={close}>Done</Button> : <><Button onClick={close}>Cancel</Button><Button variant="primary" busy={busy} disabled={!file} onClick={submit}><Upload className="size-4" />Upload and verify</Button></>}>
      {result ? (
        <div className="space-y-3"><div className="flex items-center gap-3"><ArtifactStatus s={result.status} /><span className="text-sm">{result.name}</span></div>
          {result.verification.message && <p className="rounded-md border border-fail-line bg-fail-bg p-3 text-sm text-fail">{result.verification.message}</p>}
          <ul className="divide-y divide-line rounded-md border border-line text-sm">{result.verification.checks.map((c) => <li key={c.key} className="flex items-start justify-between gap-3 px-3 py-2"><div><div>{c.name}</div><div className="text-xs text-muted">{c.message}</div></div><OutcomePill outcome={c.outcome} /></li>)}</ul></div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Backup file" className="sm:col-span-2" hint="pg_dump -Fc (custom) or -Ft (tar). The file is streamed, hashed and read completely before it can be used.">{(id) => <Input id={id} type="file" accept=".dump,.backup,.tar,application/octet-stream" onChange={(e) => setFile(e.target.files?.[0] ?? null)} className="h-auto py-1.5" />}</Field>
          <Field label="Name">{(id) => <Input id={id} value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder={file?.name ?? "orders-nightly"} />}</Field>
          <Field label="Series label" hint="Projects can test the newest backup with this label.">{(id) => <Input id={id} value={f.label} onChange={(e) => setF({ ...f, label: e.target.value })} placeholder="orders-nightly" />}</Field>
          <Field label="Backup taken at" hint="Used for RPO. Leave empty if unknown; it will not be guessed.">{(id) => <Input id={id} type="datetime-local" value={f.created} onChange={(e) => setF({ ...f, created: e.target.value })} />}</Field>
          <Field label="Expected SHA-256 (optional)" hint="If given, the upload is refused when the bytes differ.">{(id) => <Input id={id} value={f.sha} onChange={(e) => setF({ ...f, sha: e.target.value })} className="mono" />}</Field>
          {error && <p role="alert" className="text-sm text-fail sm:col-span-2">{error}</p>}
        </div>
      )}
    </Dialog>
  );
}

export default function BackupsPage() {
  const ws = useWorkspace();
  const [page, setPage] = useState(1);
  const [qtext, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [open, setOpen] = useState(false);
  const q = useQuery<Page<Artifact>>(`${ws.base}/artifacts${qs({ page, page_size: 15, q: qtext, status })}`);
  return (
    <>
      <PageHeader title="Backup registry" subtitle="Every backup is hashed and read completely when it is registered, and again before every restore. A rejected backup keeps its reason." actions={can(ws.role, "artifact.write") && <Button variant="primary" onClick={() => setOpen(true)}><Upload className="size-4" />Register backup</Button>} />
      <Panel flush>
        <div className="flex flex-wrap gap-3 border-b border-line p-3">
          <div className="relative min-w-60 flex-1"><Search aria-hidden className="absolute top-2.5 left-3 size-4 text-faint" /><Input aria-label="Search backups" value={qtext} onChange={(e) => { setQ(e.target.value); setPage(1); }} placeholder="Search name, label, file or checksum" className="pl-9" /></div>
          <Select aria-label="Filter by status" value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }} className="w-44"><option value="">All statuses</option><option value="verified">Verified</option><option value="rejected">Rejected</option><option value="unverified">Unverified</option></Select>
        </div>
        <Async q={q} isEmpty={(d) => d.total === 0} empty={<EmptyState icon={<Archive className="size-8" />} title={qtext || status ? "No backups match these filters" : "No backups registered"} action={!qtext && !status && can(ws.role, "artifact.write") ? <Button variant="primary" onClick={() => setOpen(true)}>Register your first backup</Button> : undefined}>{qtext || status ? "Try a different search." : "Upload a pg_dump archive to start. Only workspace admins can register backups."}</EmptyState>}>
          {(d) => (<><div className={tableCls.wrap}><table className={tableCls.table}>
            <thead><tr>{["Backup", "Verification", "Restore test", "Source", "Size", "SHA-256", "Registered"].map((h) => <th key={h} className={tableCls.th}>{h}</th>)}</tr></thead>
            <tbody>{d.items.map((a) => (
              <tr key={a.id} className={tableCls.tr}>
                <td className={tableCls.td}><Link href={ws.href(`/backups/${a.id}`)} className="font-medium hover:underline">{a.name}</Link>{a.label && <div className="mono text-xs text-muted">{a.label}</div>}</td>
                <td className={tableCls.td}><ArtifactStatus s={a.status} />{a.status === "rejected" && <p className="mt-1 max-w-56 text-xs text-muted">{a.status_detail.slice(0, 90)}</p>}</td>
                <td className={tableCls.td}>{a.restore_verified ? <Pill tone="pass" icon={false}>Restored {relative(a.last_restore_at)}</Pill> : <Pill tone="idle" icon={false}>Never restored</Pill>}</td>
                <td className={tableCls.td + " text-muted"}>{a.format}{a.dump_server_version ? ` · PG ${a.dump_server_version.split(" ")[0]}` : ""}</td>
                <td className={tableCls.td + " tnum"}>{bytes(a.size_bytes)}</td><td className={tableCls.td + " mono text-muted"}>{a.sha256.slice(0, 12)}</td><td className={tableCls.td + " text-muted"}>{relative(a.created_at)}</td>
              </tr>))}</tbody></table></div><Pagination page={d.page} pageSize={d.page_size} total={d.total} onPage={setPage} /></>)}
        </Async>
      </Panel>
      <UploadDialog open={open} onClose={() => setOpen(false)} onDone={q.reload} />
    </>
  );
}
