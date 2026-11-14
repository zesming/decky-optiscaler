import { useCallback, useEffect, useRef, useState } from "react";
import { getLiveStatus } from "../api";
import type { LiveStatus } from "../types";

const POLL_MS = 4000;

/** Tracks live support and connection state for one install, ignoring stale replies. */
export function useLiveStatus(targetDir: string | null, active: boolean) {
  const [snapshot, setSnapshot] = useState<{ target: string | null; status: LiveStatus | null }>({
    target: null,
    status: null,
  });
  const targetRef = useRef(targetDir);
  targetRef.current = targetDir;
  const request = useRef(0);

  const refresh = useCallback(async () => {
    const target = targetDir;
    const sequence = ++request.current;
    if (!target) {
      setSnapshot({ target: null, status: null });
      return;
    }
    try {
      const status = await getLiveStatus(target);
      if (targetRef.current === target && request.current === sequence) {
        setSnapshot({ target, status });
      }
    } catch {
      // Keep the last answer for this target; the panel can still explain that
      // it has not reported, and another explicit refresh can retry it.
    }
  }, [targetDir]);

  useEffect(() => {
    void refresh();
    if (!active || !targetDir) return;
    const handle = window.setInterval(() => void refresh(), POLL_MS);
    return () => window.clearInterval(handle);
  }, [refresh, active, targetDir]);

  return {
    status: snapshot.target === targetDir ? snapshot.status : null,
    refresh,
  };
}
