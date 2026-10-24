import Link from "next/link";
import { Logo } from "@/components/shell";

const gaps = [
  ["Backups that were never restored", "A backup job that exits 0 proves a file was written. It does not prove the file restores, that the schema is right, or that the data is correct."],
  ["Recovery time nobody measured", "The RTO in the runbook is a hope until someone restores a real archive and times it, including validation, on current data volumes."],
  ["Data loss nobody can state", "RPO is only meaningful if you know what the newest recoverable transaction actually is, and say so when you cannot tell."],
];
const stages = [
  ["Verify the artifact", "SHA-256 against the recorded value, format, table of contents, version compatibility, then a full read of every data block."],
  ["Restore in isolation", "A private PostgreSQL cluster with no network listener. The backup is restored by an unprivileged role, never a superuser."],
  ["Verify independently", "Structure, constraints, row counts, business invariants and complete dataset comparisons, run as a read-only role."],
  ["Measure and judge", "Restore, validation and total time against your RTO. RPO from real evidence, or reported as unknown."],
  ["Clean up and keep evidence", "Every temporary resource is removed and confirmed. The run leaves a checksummed JSON and HTML report."],
];
const facts = [
  "Plain-SQL dumps are refused: they are scripts for psql, and nothing inside a backup is executed.",
  "pg_restore crashing on a malformed archive is classified as a corrupt backup, not a platform error.",
  "A required check that is skipped or inconclusive never produces a success.",
  "A killed worker is recovered from durable state; its orphaned database is found and removed.",
  "Reports state what they do not show, including partial checks and unknown objectives.",
];

export default function Landing() {
  return (
    <div className="min-h-dvh">
      <header className="mx-auto flex max-w-6xl items-center justify-between px-6 py-5">
        <Logo />
        <nav className="flex items-center gap-2 text-sm"><Link className="rounded-md px-3 py-1.5 text-muted hover:text-ink" href="/login">Sign in</Link><Link className="rounded-md border border-accent bg-accent px-3 py-1.5 font-medium text-accent-ink" href="/register">Create workspace</Link></nav>
      </header>
      <main>
        <section className="mx-auto max-w-6xl px-6 pt-16 pb-20">
          <p className="mono mb-5 text-xs text-muted">Recovery verification and resilience engineering for PostgreSQL</p>
          <h1 className="max-w-3xl text-5xl leading-[1.05] font-semibold tracking-tight">Prove recovery before it matters.</h1>
          <p className="mt-6 max-w-2xl text-lg text-muted">AFTERSTATE restores your real backups into disposable environments, checks that the recovered database is correct, measures how long recovery takes and how much data it would cost, and keeps the evidence another engineer can reproduce.</p>
          <div className="mt-8 flex flex-wrap gap-3"><Link className="rounded-md border border-accent bg-accent px-5 py-2.5 text-sm font-medium text-accent-ink" href="/register">Start verifying backups</Link><Link className="rounded-md border border-line-strong bg-s2 px-5 py-2.5 text-sm font-medium" href="/login">Open the Recovery Lab</Link></div>
          <div className="mt-12 rounded-lg border border-line bg-s1 p-5" aria-hidden>
            <div className="mono mb-3 flex justify-between text-[11px] text-faint"><span>run 4f2a9c10 · orders-db</span><span>RTO 120s</span></div>
            <div className="relative h-9 rounded-md border border-line bg-s2"><div className="absolute inset-y-1 left-1 flex w-[56%] overflow-hidden rounded-[4px]"><div className="w-[8%] bg-idle" /><div className="w-[18%] bg-warn" /><div className="w-[52%] bg-active" /><div className="w-[22%] bg-pass" /></div><div className="absolute inset-y-0 left-[78%] w-px bg-ink" /></div>
            <p className="mt-3 text-xs text-muted">The Recovery Ruler: every stage measured on the real run, laid against the objective. Illustrative layout; the app draws it from recorded durations only.</p>
          </div>
        </section>
        <section className="border-y border-line bg-s1"><div className="mx-auto grid max-w-6xl gap-px px-6 py-14 md:grid-cols-3 md:gap-10">
          {gaps.map(([t, d]) => <div key={t}><h2 className="text-base font-semibold">{t}</h2><p className="mt-2 text-sm text-muted">{d}</p></div>)}
        </div></section>
        <section className="mx-auto max-w-6xl px-6 py-16">
          <h2 className="text-2xl font-semibold tracking-tight">How a recovery test runs</h2>
          <ol className="mt-8 grid gap-4 md:grid-cols-5">{stages.map(([t, d], i) => <li key={t} className="rounded-lg border border-line bg-s1 p-4"><span className="mono text-xs text-faint">0{i + 1}</span><h3 className="mt-2 text-sm font-semibold">{t}</h3><p className="mt-2 text-[13px] text-muted">{d}</p></li>)}</ol>
        </section>
        <section className="border-t border-line bg-s1"><div className="mx-auto grid max-w-6xl gap-10 px-6 py-16 md:grid-cols-2">
          <div><h2 className="text-2xl font-semibold tracking-tight">Built to be doubted</h2><p className="mt-3 text-sm text-muted">The first release does one thing: PostgreSQL logical backups (pg_dump custom and tar formats). It does not claim to cover other databases, point-in-time recovery or regulatory compliance.</p></div>
          <ul className="space-y-3 text-sm">{facts.map((f) => <li key={f} className="flex gap-3"><span aria-hidden className="mt-2 size-1.5 shrink-0 rounded-full bg-ink" />{f}</li>)}</ul>
        </div></section>
      </main>
      <footer className="mx-auto max-w-6xl px-6 py-8 text-xs text-muted">AFTERSTATE. Recovery is an engineering capability that must be tested, measured, and evidenced, not merely documented.</footer>
    </div>
  );
}
