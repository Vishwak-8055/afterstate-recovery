"use client";
import clsx from "clsx";
import Link from "next/link";
import { Ban, CircleCheck, CircleDashed, CircleSlash, CircleX, LoaderCircle, TriangleAlert, X } from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useId, useRef, useState, type ComponentProps, type ReactNode } from "react";
import { ApiError, describeError } from "@/lib/api";
import type { Query } from "@/lib/hooks";
import type { Outcome, RunState } from "@/lib/types";

export const cn = clsx;
export type Tone = "pass" | "warn" | "fail" | "idle" | "active";
const toneClass: Record<Tone, string> = {
  pass: "bg-pass-bg text-pass border-pass-line", warn: "bg-warn-bg text-warn border-warn-line", fail: "bg-fail-bg text-fail border-fail-line",
  idle: "bg-idle-bg text-idle border-idle-line", active: "bg-active-bg text-active border-active-line",
};

export const RUN_STATE: Record<RunState, { label: string; tone: Tone }> = {
  QUEUED: { label: "Queued", tone: "idle" }, PREPARING: { label: "Preparing", tone: "active" }, RESTORING: { label: "Restoring", tone: "active" },
  VALIDATING: { label: "Validating", tone: "active" }, FINALIZING: { label: "Finalizing", tone: "active" }, SUCCEEDED: { label: "Succeeded", tone: "pass" },
  FAILED: { label: "Failed", tone: "fail" }, CANCELLED: { label: "Cancelled", tone: "idle" }, NEEDS_CLEANUP: { label: "Needs cleanup", tone: "warn" }, NEEDS_ATTENTION: { label: "Needs attention", tone: "warn" },
};
export const OUTCOME: Record<Outcome, { label: string; tone: Tone }> = {
  PASS: { label: "Pass", tone: "pass" }, FAIL: { label: "Fail", tone: "fail" }, SKIPPED: { label: "Skipped", tone: "idle" }, INCONCLUSIVE: { label: "Inconclusive", tone: "warn" },
};
const ICON: Record<Tone, typeof CircleCheck> = { pass: CircleCheck, warn: TriangleAlert, fail: CircleX, idle: CircleDashed, active: LoaderCircle };

export function Pill({ tone, children, icon = true, spin = false, className }: { tone: Tone; children: ReactNode; icon?: boolean; spin?: boolean; className?: string }) {
  const Icon = ICON[tone];
  return (
    <span className={cn("inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs font-medium whitespace-nowrap", toneClass[tone], className)}>
      {icon && <Icon aria-hidden className={cn("size-3.5", (spin || tone === "active") && "motion-safe:animate-spin")} />}
      {children}
    </span>
  );
}
export const StatePill = ({ state }: { state: RunState }) => <Pill tone={RUN_STATE[state].tone}>{RUN_STATE[state].label}</Pill>;
export const OutcomePill = ({ outcome }: { outcome: Outcome }) => <Pill tone={OUTCOME[outcome].tone}>{OUTCOME[outcome].label}</Pill>;

type BtnProps = { variant?: "primary" | "secondary" | "ghost" | "danger"; size?: "sm" | "md"; busy?: boolean } & ComponentProps<"button">;
const btn = (variant: string, size: string) =>
  cn("inline-flex items-center justify-center gap-2 rounded-md border font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50",
    size === "sm" ? "h-8 px-2.5 text-[13px]" : "h-9 px-3.5 text-sm",
    variant === "primary" && "border-accent bg-accent text-accent-ink hover:opacity-90",
    variant === "secondary" && "border-line-strong bg-s2 text-ink hover:bg-s3",
    variant === "ghost" && "border-transparent text-muted hover:bg-s2 hover:text-ink",
    variant === "danger" && "border-fail-line bg-fail-bg text-fail hover:opacity-90");
export function Button({ variant = "secondary", size = "md", busy, className, children, disabled, ...rest }: BtnProps) {
  return <button type="button" {...rest} disabled={disabled || busy} className={cn(btn(variant, size), className)}>{busy && <LoaderCircle aria-hidden className="size-4 motion-safe:animate-spin" />}{children}</button>;
}
export function LinkButton({ href, variant = "secondary", size = "md", className, children, ...rest }: { href: string; variant?: BtnProps["variant"]; size?: BtnProps["size"] } & Omit<ComponentProps<typeof Link>, "href">) {
  return <Link href={href} {...rest} className={cn(btn(variant, size), className)}>{children}</Link>;
}

const field = "w-full rounded-md border border-line-strong bg-s1 px-3 py-2 text-sm text-ink placeholder:text-faint focus:border-focus disabled:opacity-60";
export const Input = (p: ComponentProps<"input">) => <input {...p} className={cn(field, "h-9 py-0", p.className)} />;
export const Textarea = (p: ComponentProps<"textarea">) => <textarea {...p} className={cn(field, "min-h-24 font-mono text-[13px]", p.className)} />;
export const Select = (p: ComponentProps<"select">) => <select {...p} className={cn(field, "h-9 py-0", p.className)} />;
export function Field({ label, hint, error, children, className }: { label: string; hint?: string; error?: string; children: (id: string) => ReactNode; className?: string }) {
  const id = useId();
  return (
    <div className={cn("space-y-1.5", className)}>
      <label htmlFor={id} className="block text-[13px] font-medium text-ink">{label}</label>
      {children(id)}
      {hint && !error && <p className="text-xs text-muted">{hint}</p>}
      {error && <p role="alert" className="text-xs text-fail">{error}</p>}
    </div>
  );
}

export const Panel = ({ title, actions, children, className, flush }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; flush?: boolean }) => (
  <section className={cn("rise rounded-lg border border-line bg-s1", className)}>
    {(title || actions) && (
      <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-3">
        <h2 className="text-sm font-semibold tracking-tight">{title}</h2>{actions}
      </header>
    )}
    <div className={flush ? "" : "p-4"}>{children}</div>
  </section>
);

export const PageHeader = ({ title, subtitle, actions }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode }) => (
  <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
    <div className="min-w-0"><h1 className="text-2xl font-semibold tracking-tight">{title}</h1>{subtitle && <p className="mt-1 max-w-3xl text-sm text-muted">{subtitle}</p>}</div>
    {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
  </div>
);

export const Skeleton = ({ className }: { className?: string }) => <div aria-hidden className={cn("working rounded-md bg-s2", className)} />;
export const Mono = ({ children, className }: { children: ReactNode; className?: string }) => <span className={cn("mono break-all", className)}>{children}</span>;
export const KV = ({ k, children }: { k: string; children: ReactNode }) => (<div className="grid grid-cols-[10rem_1fr] gap-3 py-1.5 text-sm"><dt className="text-muted">{k}</dt><dd className="min-w-0 break-words">{children}</dd></div>);

export function EmptyState({ icon, title, children, action }: { icon?: ReactNode; title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-3 px-6 py-12 text-center">
      {icon && <div className="text-faint">{icon}</div>}
      <h3 className="text-base font-semibold">{title}</h3>
      {children && <p className="max-w-md text-sm text-muted">{children}</p>}
      {action}
    </div>
  );
}

export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  const status = error instanceof ApiError ? error.status : 0;
  const denied = status === 403, missing = status === 404;
  return (
    <div role="alert" className="flex flex-col items-center gap-3 px-6 py-12 text-center">
      {denied ? <Ban aria-hidden className="size-8 text-warn" /> : <TriangleAlert aria-hidden className="size-8 text-fail" />}
      <h3 className="text-base font-semibold">{denied ? "You do not have permission" : missing ? "Not found" : "This could not be loaded"}</h3>
      <p className="max-w-md text-sm text-muted">{denied ? describeError(error) + " Ask a workspace owner to change your role if you need access." : missing ? "It may have been removed, or it belongs to another workspace." : describeError(error)}</p>
      {retry && !denied && !missing && <Button onClick={retry}>Try again</Button>}
    </div>
  );
}

/** Renders loading, error, optional empty, then data. Every data screen goes through here. */
export function Async<T>({ q, children, isEmpty, empty, skeleton }: { q: Query<T>; children: (d: T) => ReactNode; isEmpty?: (d: T) => boolean; empty?: ReactNode; skeleton?: ReactNode }) {
  if (q.error && !q.data) return <ErrorState error={q.error} retry={q.reload} />;
  if (!q.data) return <>{skeleton ?? <div className="space-y-3 p-4" aria-busy="true" aria-label="Loading"><Skeleton className="h-6 w-1/3" /><Skeleton className="h-24" /><Skeleton className="h-24" /></div>}</>;
  if (isEmpty?.(q.data)) return <>{empty}</>;
  return <>{children(q.data)}</>;
}

export function Pagination({ page, pageSize, total, onPage }: { page: number; pageSize: number; total: number; onPage: (p: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  return (
    <nav aria-label="Pagination" className="flex items-center justify-between gap-3 border-t border-line px-4 py-2.5 text-sm text-muted">
      <span className="tnum">{total === 0 ? "No results" : `${(page - 1) * pageSize + 1}-${Math.min(page * pageSize, total)} of ${total}`}</span>
      <div className="flex gap-2"><Button size="sm" disabled={page <= 1} onClick={() => onPage(page - 1)}>Previous</Button><Button size="sm" disabled={page >= pages} onClick={() => onPage(page + 1)}>Next</Button></div>
    </nav>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: { id: T; label: string; count?: number }[]; value: T; onChange: (t: T) => void }) {
  return (
    <div role="tablist" className="mb-4 flex gap-1 border-b border-line">
      {tabs.map((t) => (
        <button key={t.id} type="button" role="tab" aria-selected={value === t.id} onClick={() => onChange(t.id)}
          className={cn("-mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors", value === t.id ? "border-accent text-ink" : "border-transparent text-muted hover:text-ink")}>
          {t.label}{t.count !== undefined && <span className="tnum ml-1.5 text-xs text-faint">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}

export const tableCls = { wrap: "overflow-x-auto", table: "w-full border-collapse text-sm", th: "border-b border-line px-4 py-2.5 text-left text-xs font-medium text-muted", td: "border-b border-line px-4 py-3 align-top last:border-b-0", tr: "hover:bg-s2/60" };

export function Dialog({ open, onClose, title, children, footer, wide }: { open: boolean; onClose: () => void; title: string; children: ReactNode; footer?: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const d = ref.current; if (!d) return; if (open && !d.open) d.showModal(); if (!open && d.open) d.close(); }, [open]);
  return (
    <dialog ref={ref} onClose={onClose} onClick={(e) => { if (e.target === ref.current) onClose(); }}
      className={cn("m-auto w-[calc(100%-2rem)] rounded-lg border border-line-strong bg-s1 p-0 shadow-[var(--shadow)]", wide ? "max-w-3xl" : "max-w-lg")}>
      {open && (
        <div>
          <header className="flex items-center justify-between border-b border-line px-5 py-3.5"><h2 className="text-base font-semibold">{title}</h2><button aria-label="Close dialog" onClick={onClose} className="rounded p-1 text-muted hover:bg-s2 hover:text-ink"><X className="size-4" /></button></header>
          <div className="max-h-[70vh] overflow-y-auto p-5">{children}</div>
          {footer && <footer className="flex justify-end gap-2 border-t border-line px-5 py-3.5">{footer}</footer>}
        </div>
      )}
    </dialog>
  );
}

export function ConfirmDialog({ open, title, body, confirmLabel, danger, busy, onConfirm, onClose }: { open: boolean; title: string; body: ReactNode; confirmLabel: string; danger?: boolean; busy?: boolean; onConfirm: () => void; onClose: () => void }) {
  return (
    <Dialog open={open} onClose={onClose} title={title} footer={<><Button onClick={onClose}>Cancel</Button><Button variant={danger ? "danger" : "primary"} busy={busy} onClick={onConfirm}>{confirmLabel}</Button></>}>
      <div className="text-sm text-muted">{body}</div>
    </Dialog>
  );
}

type Toast = { id: number; tone: Tone; text: string };
const ToastCtx = createContext<(text: string, tone?: Tone) => void>(() => undefined);
export const useToast = () => useContext(ToastCtx);
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((text: string, tone: Tone = "pass") => {
    const id = Date.now() + Math.random();
    setItems((x) => [...x, { id, tone, text }]);
    setTimeout(() => setItems((x) => x.filter((t) => t.id !== id)), 6000);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div aria-live="polite" className="fixed right-4 bottom-4 z-50 flex w-[22rem] max-w-[calc(100vw-2rem)] flex-col gap-2">
        {items.map((t) => <div key={t.id} role="status" className={cn("rise rounded-md border px-3.5 py-2.5 text-sm shadow-[var(--shadow)]", toneClass[t.tone])}>{t.text}</div>)}
      </div>
    </ToastCtx.Provider>
  );
}
export { CircleSlash };
