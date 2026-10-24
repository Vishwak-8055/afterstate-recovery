export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details?: unknown) { super(message); }
}

function csrfToken(): string {
  if (typeof document === "undefined") return "";
  const hit = document.cookie.split("; ").find((c) => c.startsWith("afterstate_csrf="));
  return hit ? decodeURIComponent(hit.split("=")[1] ?? "") : "";
}

export const BASE = "/api/v1";

export async function api<T>(path: string, init: { method?: string; body?: unknown; headers?: Record<string, string>; rawBody?: BodyInit } = {}): Promise<T> {
  const method = init.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json", ...init.headers };
  let body: BodyInit | undefined;
  if (init.rawBody !== undefined) body = init.rawBody;
  else if (init.body !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(init.body); }
  if (method !== "GET") headers["X-CSRF-Token"] = csrfToken();
  const res = await fetch(`${BASE}${path}`, { method, headers, body, credentials: "same-origin" });
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let data: unknown = undefined;
  try { data = text ? JSON.parse(text) : undefined; } catch { /* non-JSON body */ }
  if (!res.ok) {
    const err = (data as { error?: { code?: string; message?: string; details?: unknown } } | undefined)?.error;
    throw new ApiError(res.status, err?.code ?? "http_error", err?.message ?? `Request failed (${res.status})`, err?.details);
  }
  return data as T;
}

export const get = <T,>(path: string) => api<T>(path);
export const post = <T,>(path: string, body?: unknown, headers?: Record<string, string>) => api<T>(path, { method: "POST", body: body ?? {}, headers });
export const put = <T,>(path: string, body: unknown) => api<T>(path, { method: "PUT", body });
export const patch = <T,>(path: string, body: unknown) => api<T>(path, { method: "PATCH", body });
export const del = <T,>(path: string) => api<T>(path, { method: "DELETE" });

export function qs(params: Record<string, string | number | boolean | undefined | null | string[]>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => u.append(k, x)); else u.set(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : "";
}

export function describeError(e: unknown): string {
  if (e instanceof ApiError) {
    const d = Array.isArray(e.details) ? e.details.map((x: { field?: string; message?: string }) => `${x.field ? x.field + ": " : ""}${x.message ?? ""}`).join("; ") : "";
    return d ? `${e.message} ${d}` : e.message;
  }
  return e instanceof Error ? e.message : "Something went wrong.";
}
