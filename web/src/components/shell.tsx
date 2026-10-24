"use client";
import Link from "next/link";
import { useParams, usePathname, useRouter } from "next/navigation";
import { Archive, History, LayoutDashboard, ListChecks, LogOut, Moon, ScrollText, Settings, Sun, FlaskConical, CalendarClock, ShieldCheck } from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { post } from "@/lib/api";
import { useQuery } from "@/lib/hooks";
import { can } from "@/lib/rbac";
import type { Me, Membership } from "@/lib/types";
import { Button, cn, ErrorState, Skeleton, ToastProvider } from "./ui";

export function Logo({ className }: { className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-2.5", className)}>
      <svg width="26" height="18" viewBox="0 0 26 18" fill="none" aria-hidden>
        <path d="M1 13h24" stroke="currentColor" strokeWidth="1.5" /><path d="M4 13v-3M8 13v-3M12 13v-3M16 13v-3M20 13v-3" stroke="currentColor" strokeWidth="1.2" opacity=".55" />
        <path d="M20 13V3" stroke="currentColor" strokeWidth="1.5" /><path d="M20 3h5v5h-5z" fill="currentColor" />
      </svg>
      <span className="text-[13px] font-semibold tracking-[0.14em]">AFTERSTATE</span>
    </span>
  );
}

interface AuthCtx { me: Me | undefined; loading: boolean; refresh: () => void; logout: () => Promise<void> }
const Ctx = createContext<AuthCtx>({ me: undefined, loading: true, refresh: () => undefined, logout: async () => undefined });
export const useAuth = () => useContext(Ctx);

export function ThemeToggle() {
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  useEffect(() => { setTheme((document.documentElement.dataset.theme as "dark" | "light") || "dark"); }, []);
  const flip = () => { const t = theme === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = t; localStorage.setItem("afterstate-theme", t); setTheme(t); };
  return <Button variant="ghost" size="sm" onClick={flip} aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}>{theme === "dark" ? <Sun className="size-4" /> : <Moon className="size-4" />}</Button>;
}

export function Providers({ children }: { children: ReactNode }) {
  const q = useQuery<Me>("/auth/me");
  const router = useRouter();
  const logout = useCallback(async () => { await post("/auth/logout"); router.push("/login"); q.reload(); }, [q, router]);
  return <Ctx.Provider value={{ me: q.data, loading: q.loading, refresh: q.reload, logout }}><ToastProvider>{children}</ToastProvider></Ctx.Provider>;
}

const NAV = [
  { href: "overview", label: "Overview", icon: LayoutDashboard }, { href: "backups", label: "Backups", icon: Archive }, { href: "projects", label: "Projects", icon: ListChecks },
  { href: "runs", label: "Recovery runs", icon: History }, { href: "lab", label: "Recovery Lab", icon: FlaskConical }, { href: "schedules", label: "Schedules", icon: CalendarClock },
  { href: "audit", label: "Audit", icon: ScrollText, need: "audit.read" as const }, { href: "settings", label: "Settings", icon: Settings },
];

export function AppShell({ workspaceId, children }: { workspaceId: string; children: ReactNode }) {
  const { me, loading, logout } = useAuth();
  const pathname = usePathname();
  const router = useRouter();
  useEffect(() => { if (!loading && !me) router.replace(`/login?next=${encodeURIComponent(pathname)}`); }, [loading, me, pathname, router]);
  if (!me) return <div className="grid min-h-dvh place-items-center"><Skeleton className="h-8 w-48" /></div>;
  const membership: Membership | undefined = me.memberships.find((m) => m.workspace_id === workspaceId);
  if (!membership) return <div className="grid min-h-dvh place-items-center"><ErrorState error={{ status: 404 }} /></div>;
  const items = NAV.filter((n) => !n.need || can(membership.role, n.need));
  return (
    <div className="min-h-dvh md:grid md:grid-cols-[15.5rem_1fr]">
      <aside className="border-b border-line bg-s1 md:sticky md:top-0 md:h-dvh md:border-r md:border-b-0">
        <div className="flex h-full flex-col gap-1 p-3">
          <Link href="/" className="px-2 py-3 text-ink"><Logo /></Link>
          <div className="mb-2 rounded-md border border-line bg-s2/60 px-3 py-2">
            <div className="text-[11px] text-faint">Workspace</div>
            {me.memberships.length > 1 ? (
              <select aria-label="Switch workspace" value={workspaceId} onChange={(e) => router.push(`/w/${e.target.value}/overview`)} className="mt-0.5 w-full bg-transparent text-sm font-medium focus:outline-none">
                {me.memberships.map((m) => <option key={m.workspace_id} value={m.workspace_id} className="bg-s1">{m.name}</option>)}
              </select>
            ) : <div className="truncate text-sm font-medium">{membership.name}</div>}
            <div className="text-xs text-muted">{membership.role}</div>
          </div>
          <nav aria-label="Primary" className="flex gap-1 overflow-x-auto md:flex-col">
            {items.map(({ href, label, icon: Icon }) => {
              const to = `/w/${workspaceId}/${href}`;
              const active = pathname === to || pathname.startsWith(`${to}/`);
              return (
                <Link key={href} href={to} aria-current={active ? "page" : undefined}
                  className={cn("relative flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm whitespace-nowrap transition-colors", active ? "bg-s3 font-medium text-ink before:absolute before:inset-y-1.5 before:-left-1 before:w-0.5 before:rounded before:bg-ink" : "text-muted hover:bg-s2 hover:text-ink")}>
                  <Icon aria-hidden className="size-4" />{label}
                </Link>
              );
            })}
          </nav>
          <div className="mt-auto hidden items-center justify-between gap-2 border-t border-line pt-3 md:flex">
            <div className="min-w-0"><div className="truncate text-[13px] font-medium">{me.user.display_name}</div><div className="truncate text-xs text-muted">{me.user.email}</div></div>
            <div className="flex"><ThemeToggle /><Button variant="ghost" size="sm" onClick={logout} aria-label="Sign out"><LogOut className="size-4" /></Button></div>
          </div>
        </div>
      </aside>
      <main className="min-w-0 px-5 py-7 md:px-9"><div className="mx-auto max-w-[78rem]">{children}</div></main>
    </div>
  );
}

export function useWorkspace() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const { me } = useAuth();
  const id = workspaceId ?? "";
  const membership = me?.memberships.find((m) => m.workspace_id === id);
  return { id, role: membership?.role, name: membership?.name, base: `/workspaces/${id}`, href: (p: string) => `/w/${id}${p}` };
}
export { ShieldCheck };
