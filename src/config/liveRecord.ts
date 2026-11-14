import type { LiveApplyResult } from "../types";
import type { LiveContext } from "../types";

export interface PendingLiveChange {
  target: string;
  session: string;
  seq: string;
  /** Status requests started at or before this epoch cannot retire this send. */
  statusEpoch: number;
  keys: string[];
  values: Record<string, string>;
  revisions: Record<string, number>;
  contexts: Record<string, LiveContext>;
}

export interface TargetLiveRecord {
  restartKeys: string[];
  livePending: PendingLiveChange[];
  live: LiveApplyResult | null;
  /** Last values and target-wide revisions confirmed written to the INI. */
  persistedValues: Record<string, string>;
  persistedRevisions: Record<string, number>;
}

const records = new Map<string, TargetLiveRecord>();
const listeners = new Map<string, Set<() => void>>();
const statusRequestEpochs = new Map<string, number>();

/** Monotonic per-target request order shared by every mounted live-status hook. */
export function beginTargetLiveStatusRequest(target: string): number {
  const epoch = (statusRequestEpochs.get(target) ?? 0) + 1;
  statusRequestEpochs.set(target, epoch);
  return epoch;
}

export function getTargetLiveStatusRequestEpoch(target: string): number {
  return statusRequestEpochs.get(target) ?? 0;
}

function copy(record: TargetLiveRecord): TargetLiveRecord {
  return {
    restartKeys: [...record.restartKeys],
    livePending: record.livePending.map((entry) => ({
      ...entry,
      keys: [...entry.keys],
      values: { ...entry.values },
      revisions: { ...entry.revisions },
      contexts: Object.fromEntries(Object.entries(entry.contexts).map(([key, context]) => [key, { ...context }])),
    })),
    live: record.live ? { ...record.live } : null,
    persistedValues: { ...record.persistedValues },
    persistedRevisions: { ...record.persistedRevisions },
  };
}

export function getTargetLiveRecord(target: string): TargetLiveRecord {
  const record = records.get(target);
  return record ? copy(record) : {
    restartKeys: [], livePending: [], live: null,
    persistedValues: {}, persistedRevisions: {},
  };
}

export function setTargetLiveRecord(target: string, record: TargetLiveRecord): void {
  if (
    record.restartKeys.length === 0 && record.livePending.length === 0 && !record.live &&
    Object.keys(record.persistedValues).length === 0 && Object.keys(record.persistedRevisions).length === 0
  ) {
    records.delete(target);
  } else {
    records.set(target, copy(record));
  }
  for (const listener of listeners.get(target) ?? []) listener();
}

/** Mutate the shared per-target state before any component-local React update. */
export function updateTargetLiveRecord(
  target: string,
  update: (current: TargetLiveRecord) => TargetLiveRecord,
): TargetLiveRecord {
  const next = update(getTargetLiveRecord(target));
  setTargetLiveRecord(target, next);
  return getTargetLiveRecord(target);
}

export function subscribeTargetLiveRecord(target: string, listener: () => void): () => void {
  let targetListeners = listeners.get(target);
  if (!targetListeners) {
    targetListeners = new Set();
    listeners.set(target, targetListeners);
  }
  targetListeners.add(listener);
  return () => {
    targetListeners?.delete(listener);
    if (targetListeners?.size === 0) listeners.delete(target);
  };
}
