"use client";
import { duration } from "@/lib/format";
import { cn, Pill } from "./ui";
import type { Objective, Stage } from "@/lib/types";

export interface Segment { label: string; seconds: number; tone: "active" | "pass" | "warn" | "idle" }

/** The signature visual: measured stage durations laid end to end against the RTO budget. Real data only. */
export function RecoveryRuler({ segments, rto }: { segments: Segment[]; rto?: number | null }) {
  const total = segments.reduce((a, s) => a + s.seconds, 0);
  const scale = Math.max(total, rto ?? 0) * 1.08 || 1;
  const over = rto != null && total > rto;
  const fills: Record<Segment["tone"], string> = { active: "bg-active", pass: "bg-pass", warn: "bg-warn", idle: "bg-idle" };
  const ticks = 6;
  return (
    <figure aria-label={`Recovery timeline, ${duration(total)} measured${rto ? `, target ${duration(rto)}` : ""}`}>
      <div className="relative h-9 rounded-md border border-line bg-s2">
        <div className="absolute inset-y-1 left-1 flex overflow-hidden rounded-[4px]" style={{ width: `calc(${(total / scale) * 100}% - 4px)` }}>
          {segments.filter((s) => s.seconds > 0).map((s, i) => (
            <div key={i} title={`${s.label}: ${duration(s.seconds)}`} className={cn("h-full border-r border-bg/70 last:border-r-0", fills[s.tone])} style={{ width: `${(s.seconds / (total || 1)) * 100}%`, minWidth: 3 }} />
          ))}
        </div>
        {rto != null && (
          <div className="absolute inset-y-0" style={{ left: `${(rto / scale) * 100}%` }}>
            <div className={cn("h-full w-px", over ? "bg-fail" : "bg-ink")} />
            <span className={cn("mono absolute -top-5 -translate-x-1/2 text-[11px] whitespace-nowrap", over ? "text-fail" : "text-muted")}>RTO {duration(rto)}</span>
          </div>
        )}
      </div>
      <div className="relative mt-1 h-3 text-[10px] text-faint" aria-hidden>
        {Array.from({ length: ticks + 1 }, (_, i) => (<span key={i} className="mono absolute -translate-x-1/2" style={{ left: `${(i / ticks) * 100}%` }}>{duration((scale * i) / ticks).replace(" s", "s")}</span>))}
      </div>
      <figcaption className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted">
        {segments.map((s, i) => (<span key={i} className="inline-flex items-center gap-1.5"><span className={cn("size-2 rounded-[2px]", fills[s.tone])} />{s.label} <span className="mono text-ink">{duration(s.seconds)}</span></span>))}
      </figcaption>
    </figure>
  );
}

export function segmentsFrom(stages: Stage[]): Segment[] {
  const sec = (n: string) => (stages.find((s) => s.name === n)?.duration_ms ?? 0) / 1000;
  const validation = sec("structural_validation") + sec("data_validation") + sec("application_validation");
  return [
    { label: "Artifact verification", seconds: sec("artifact_verification"), tone: "idle" },
    { label: "Provisioning", seconds: sec("environment_provisioning"), tone: "warn" },
    { label: "Restore", seconds: sec("restore"), tone: "active" },
    { label: "Validation", seconds: validation, tone: "pass" },
  ];
}

const objTone = { MET: "pass", MISSED: "fail", UNKNOWN: "warn", NOT_CONFIGURED: "idle" } as const;
export function ObjectiveCard({ name, o }: { name: string; o: Objective }) {
  return (
    <div className="rounded-md border border-line bg-s2/50 p-3">
      <div className="flex items-center justify-between gap-2"><span className="text-sm font-semibold">{name}</span><Pill tone={objTone[o.status]}>{o.status.replace("_", " ").toLowerCase()}</Pill></div>
      <div className="tnum mt-2 flex gap-5 text-sm"><div><div className="text-xs text-muted">Target</div>{o.target_seconds != null ? duration(o.target_seconds) : "-"}</div><div><div className="text-xs text-muted">Measured</div>{o.measured_seconds != null ? duration(o.measured_seconds) : "-"}</div></div>
      <p className="mt-2 text-xs text-muted">{o.detail}</p>
      {o.basis && <p className="mt-1 text-xs text-faint">Basis: {o.basis}</p>}
    </div>
  );
}
