import { useState, useEffect, useCallback, useRef } from "react";
import { useLookoutContext } from "../LookoutProvider.js";
import { HttpError } from "../api/client.js";
import type { RecorderStatus } from "../types.js";

interface SessionState {
  status: RecorderStatus;
  name: string;
  trackedSeconds: number;
  screenshotCount: number;
  startedAt: string | null;
  createdAt: string | null;
  totalActiveSeconds: number;
  error: string | null;
}

export function useSession() {
  const { client, config } = useLookoutContext();
  const pollIntervalMs = config.statusPollIntervalMs;

  const [state, setState] = useState<SessionState>({
    status: "loading",
    name: "",
    trackedSeconds: 0,
    screenshotCount: 0,
    startedAt: null,
    createdAt: null,
    totalActiveSeconds: 0,
    error: null,
  });

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const loadSession = useCallback(async () => {
    let token: string;
    try {
      token = await client.resolveToken();
    } catch {
      setState((s) => ({ ...s, status: "no-token" }));
      return;
    }
    if (!token) {
      setState((s) => ({ ...s, status: "no-token" }));
      return;
    }

    try {
      const data = await client.getSession();
      setState({
        status: data.status,
        name: data.name,
        trackedSeconds: data.trackedSeconds,
        screenshotCount: data.screenshotCount,
        startedAt: data.startedAt,
        createdAt: data.createdAt,
        totalActiveSeconds: data.totalActiveSeconds,
        error: null,
      });

      if (data.status === "compiling" || data.status === "stopped") {
        startPolling();
      }
    } catch (err) {
      setState((s) => ({
        ...s,
        status: "error",
        error: err instanceof Error ? err.message : "Unknown error",
      }));
    }
  }, [client]);

  const startPolling = useCallback(() => {
    if (pollRef.current) return;
    pollRef.current = setInterval(async () => {
      try {
        const data = await client.getStatus();
        setState((s) => ({
          ...s,
          status: data.status,
          trackedSeconds: data.trackedSeconds,
        }));
        if (data.status === "complete" || data.status === "failed") {
          if (pollRef.current) clearInterval(pollRef.current);
          pollRef.current = null;
        }
      } catch (e) {
        console.warn("[session] poll error:", e);
      }
    }, pollIntervalMs);
  }, [client, pollIntervalMs]);

  const syncStatus = useCallback(async () => {
    try {
      const data = await client.getStatus();
      setState((s) => ({ ...s, status: data.status, trackedSeconds: data.trackedSeconds }));
    } catch {
      // Best-effort sync
    }
  }, [client]);

  const pause = useCallback(async () => {
    try {
      const data = await client.pause();
      setState((s) => ({
        ...s,
        status: data.status,
        totalActiveSeconds: data.totalActiveSeconds,
      }));
    } catch (e) {
      if (e instanceof HttpError && e.status === 409) {
        console.warn("[session] pause returned 409, syncing status");
        await syncStatus();
      } else {
        throw e;
      }
    }
  }, [client, syncStatus]);

  const resume = useCallback(async () => {
    try {
      const data = await client.resume();
      setState((s) => ({ ...s, status: data.status }));
    } catch (e) {
      if (e instanceof HttpError && e.status === 409) {
        console.warn("[session] resume returned 409, syncing status");
        await syncStatus();
      } else {
        throw e;
      }
    }
  }, [client, syncStatus]);

  const stop = useCallback(async (name?: string) => {
    // Optionally name the timelapse before stopping (non-fatal if it fails)
    if (name) {
      try {
        await client.rename(name);
      } catch (e) {
        console.warn("[session] rename failed (non-fatal):", e);
      }
    }
    let stopped = false;
    const MAX_RETRIES = 3;
    const RETRY_DELAYS = [1000, 2000, 4000];
    for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
      try {
        const data = await client.stop();
        setState((s) => ({
          ...s,
          status: data.status,
          trackedSeconds: data.trackedSeconds,
          totalActiveSeconds: data.totalActiveSeconds,
        }));
        stopped = true;
        break;
      } catch (e) {
        if (e instanceof HttpError && e.status === 409) {
          console.warn("[session] stop returned 409, syncing status");
          const data = await client.getStatus();
          setState((s) => ({ ...s, status: data.status, trackedSeconds: data.trackedSeconds }));
          stopped = data.status === "stopped" || data.status === "compiling";
          break;
        }
        const isRetryable = e instanceof HttpError && e.status >= 500;
        if (isRetryable && attempt < MAX_RETRIES) {
          console.warn(`[session] stop failed (${(e as HttpError).status}), retrying in ${RETRY_DELAYS[attempt]}ms (attempt ${attempt + 1}/${MAX_RETRIES})`);
          await new Promise((r) => setTimeout(r, RETRY_DELAYS[attempt]));
          continue;
        }
        throw e;
      }
    }
    if (stopped) {
      startPolling();
    }
  }, [client, startPolling]);

  const updateTrackedSeconds = useCallback((seconds: number) => {
    setState((s) => ({ ...s, trackedSeconds: seconds }));
  }, []);

  const setError = useCallback((error: string | null) => {
    setState((s) => ({ ...s, error, ...(error ? { status: "error" as const } : {}) }));
  }, []);

  useEffect(() => {
    loadSession();
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [loadSession]);

  return {
    ...state,
    pause,
    resume,
    stop,
    reload: loadSession,
    syncStatus,
    updateTrackedSeconds,
    setError,
  };
}
