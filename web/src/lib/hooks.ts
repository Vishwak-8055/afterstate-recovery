"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, get } from "./api";

export interface Query<T> { data: T | undefined; error: ApiError | Error | undefined; loading: boolean; reload: () => void }

/** Minimal data hook: fetch on mount/path change, optional polling that stops when `stop(data)` is true. */
export function useQuery<T>(path: string | null, opts: { pollMs?: number; stop?: (d: T) => boolean } = {}): Query<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<ApiError | Error>();
  const [loading, setLoading] = useState(path !== null);
  const [tick, setTick] = useState(0);
  const stopRef = useRef(opts.stop);
  useEffect(() => { stopRef.current = opts.stop; });

  useEffect(() => {
    if (path === null) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const run = async (first: boolean) => {
      try {
        const d = await get<T>(path);
        if (cancelled) return;
        setData(d); setError(undefined);
        if (opts.pollMs && !(stopRef.current?.(d))) timer = setTimeout(() => run(false), opts.pollMs);
      } catch (e) {
        if (cancelled) return;
        setError(e as Error);
        if (opts.pollMs && !first) timer = setTimeout(() => run(false), opts.pollMs * 3);
      } finally { if (!cancelled) setLoading(false); }
    };
    setLoading(true);
    run(true);
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [path, tick, opts.pollMs]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload };
}
