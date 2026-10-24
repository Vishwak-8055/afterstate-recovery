"use client";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { describeError, post } from "@/lib/api";
import type { Me } from "@/lib/types";
import { Logo, useAuth } from "@/components/shell";
import { Button, Field, Input } from "@/components/ui";

export default function Onboarding() {
  const { me, loading, refresh } = useAuth();
  const router = useRouter();
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (!loading && !me) router.replace("/login?next=/onboarding"); }, [loading, me, router]);
  const create = async () => {
    setBusy(true); setError("");
    try { const r = await post<Me>("/workspaces", { name }); refresh(); const ws = r.memberships.at(-1); router.push(`/w/${ws?.workspace_id}/overview`); }
    catch (e) { setError(describeError(e)); setBusy(false); }
  };
  return (
    <main className="grid min-h-dvh place-items-center p-6">
      <div className="w-full max-w-md space-y-6">
        <Logo />
        <div><h1 className="text-2xl font-semibold tracking-tight">Name your workspace</h1><p className="mt-2 text-sm text-muted">A workspace holds your backups, recovery projects, runs and audit history. Members and roles are managed inside it. You will be its owner.</p></div>
        <Field label="Workspace name" hint="For example, the team or system you are protecting.">{(id) => <Input id={id} value={name} onChange={(e) => setName(e.target.value)} placeholder="Platform Reliability" maxLength={120} autoFocus />}</Field>
        {error && <p role="alert" className="text-sm text-fail">{error}</p>}
        <Button variant="primary" onClick={create} busy={busy} disabled={!name.trim()}>Create workspace</Button>
      </div>
    </main>
  );
}
