import { preserveExistingInstall } from "../launchOptions";
import { useCallback, useEffect, useRef, useState } from "react";
import { readConfig, setFsr4Build, writeConfig } from "../api";
import { AUTO } from "../config/values";
import { optionsInSection } from "../config/tabs";
import {
  beginTargetRead,
  beginTargetFlush,
  finishTargetFlush,
  failTargetChanges,
  forgetWrites,
  getTargetConfigQueue,
  invalidateTargetReads,
  isCurrentTargetRead,
  overlayWrites,
  queueTargetChanges,
  rememberWrites,
  serializeTargetWrite,
  waitForTargetWrites,
} from "../config/writeRecord";
import type { TargetConfigQueue } from "../config/writeRecord";
import type {
  ConfigValues,
  LiveApplyResult,
  OptionChange,
  OptionMeta,
} from "../types";

const FLUSH_DELAY_MS = 400;

const changeId = (change: OptionChange) => `${change.section}.${change.key}`;

interface ConfigView {
  identity: string;
  values: ConfigValues;
  fsr4Build: string | null;
  runtimeCurrent: boolean | undefined;
  loading: boolean;
  error: string | null;
  dirty: boolean;
  saving: boolean;
  live: LiveApplyResult | null;
}

function emptyView(identity: string, loading = false): ConfigView {
  return {
    identity,
    values: {},
    fsr4Build: null,
    runtimeCurrent: undefined,
    loading,
    error: null,
    dirty: false,
    saving: false,
    live: null,
  };
}

/**
 * Edits one OptiScaler.ini at a time. Writes for a target are serialized across
 * hook instances, and values are always tagged with the game identity that
 * requested them so a delayed read can never populate another game's controls.
 */
export function useConfig(
  targetDir: string | null,
  enabled: boolean,
  reloadKey?: unknown,
  identityKey?: unknown,
  gameContext?: { gamePath: string; appid: string | null },
) {
  const identity = `${targetDir ?? ""}\u0000${String(identityKey ?? "")}\u0000${enabled}`;
  const identityRef = useRef(identity);
  const generation = useRef(0);
  if (identityRef.current !== identity) {
    identityRef.current = identity;
    generation.current += 1;
  }
  const generationAtRender = generation.current;
  const [storedView, setStoredView] = useState<ConfigView>(() => emptyView(identity));
  const view = storedView.identity === identity
    ? storedView
    : emptyView(identity, Boolean(targetDir && enabled));
  const localReadSequence = useRef(0);

  const getQueue = useCallback((dir: string): TargetConfigQueue => {
    return getTargetConfigQueue(dir);
  }, []);

  const isCurrent = useCallback(
    (requestIdentity: string, requestGeneration: number) =>
      identityRef.current === requestIdentity && generation.current === requestGeneration,
    [],
  );

  const updateView = useCallback(
    (requestIdentity: string, update: (current: ConfigView) => ConfigView) => {
      if (identityRef.current !== requestIdentity) return;
      setStoredView((current) => {
        const base = current.identity === requestIdentity ? current : emptyView(requestIdentity);
        return update(base);
      });
    },
    [],
  );

  const overlay = useCallback((dir: string, base: ConfigValues): ConfigValues => {
    const disk = overlayWrites(dir, base);
    const next: ConfigValues = {};
    for (const [section, keys] of Object.entries(disk)) next[section] = { ...keys };
    const queue = getQueue(dir);
    for (const newer of [queue.inFlight, queue.pending]) {
      for (const item of newer.values()) {
        const change = item.change;
        next[change.section] = { ...(next[change.section] ?? {}), [change.key]: change.value };
      }
    }
    return next;
  }, [getQueue]);

  const readTarget = useCallback(
    async (
      dir: string,
      requestIdentity: string,
      requestGeneration: number,
      authoritative = false,
    ): Promise<void> => {
      if (!enabled || !isCurrent(requestIdentity, requestGeneration)) return;
      const localSequence = ++localReadSequence.current;
      const targetSequence = beginTargetRead(dir);
      const mayApply = () =>
        isCurrent(requestIdentity, requestGeneration) &&
        localReadSequence.current === localSequence &&
        isCurrentTargetRead(dir, targetSequence);
      const retryIfStillCurrent = async () => {
        if (!isCurrent(requestIdentity, requestGeneration) || localReadSequence.current !== localSequence) {
          return;
        }
        await waitForTargetWrites(dir);
        if (isCurrent(requestIdentity, requestGeneration) && localReadSequence.current === localSequence) {
          await readTarget(dir, requestIdentity, requestGeneration, authoritative);
        }
      };
      updateView(requestIdentity, (current) => ({ ...current, loading: true, error: null }));
      try {
        const result = await readConfig(dir);
        if (!mayApply()) {
          await retryIfStillCurrent();
          return;
        }
        if (result.ok) {
          updateView(requestIdentity, (current) => ({
            ...current,
            values: authoritative ? result.values : overlay(dir, result.values),
            fsr4Build: result.fsr4_build ?? null,
            runtimeCurrent: result.runtime_current,
            error: null,
          }));
        } else {
          updateView(requestIdentity, (current) => ({
            ...current,
            error: result.error ?? "Could not read OptiScaler.ini",
          }));
        }
      } catch (exc) {
        if (mayApply()) {
          updateView(requestIdentity, (current) => ({ ...current, error: String(exc) }));
        } else {
          await retryIfStillCurrent();
        }
      } finally {
        if (
          isCurrent(requestIdentity, requestGeneration) &&
          localReadSequence.current === localSequence
        ) {
          updateView(requestIdentity, (current) => ({ ...current, loading: false }));
        }
      }
    },
    [enabled, isCurrent, overlay, updateView],
  );

  const load = useCallback(async () => {
    if (!targetDir || !enabled) return;
    await readTarget(targetDir, identity, generationAtRender);
  }, [targetDir, enabled, identity, generationAtRender, readTarget]);

  const flushTarget = useCallback(
    async (
      dir: string,
      requestIdentity: string,
      requestGeneration: number,
      foreground = false,
    ) => {
      const queue = getQueue(dir);
      const requestedRevision = queue.revision;
      const flushWaiter = beginTargetFlush(queue, requestedRevision, foreground);
      if (queue.timer !== null) window.clearTimeout(queue.timer);
      queue.timer = null;
      const drain = async () => {
        while (queue.pending.size > 0) {
          const batch = Array.from(queue.pending.values());
          queue.pending.clear();
          for (const item of batch) queue.inFlight.set(changeId(item.change), item);
          if (isCurrent(requestIdentity, requestGeneration)) {
            updateView(requestIdentity, (current) => ({ ...current, saving: true }));
          }
          invalidateTargetReads(dir);
          let refreshAfterFailure = false;
          let failureMessage: string | null = null;
          try {
            const result = await serializeTargetWrite(dir, () =>
              writeConfig(dir, batch.map((item) => item.change)),
            );
            const applied = result.live ?? null;
            const fullyLive = Boolean(applied?.sent) && (applied?.deferred?.length ?? 0) === 0;
            if (isCurrent(requestIdentity, requestGeneration)) {
              updateView(requestIdentity, (current) => ({
                ...current,
                live: applied,
                dirty: result.ok ? current.dirty || !fullyLive : current.dirty,
                error: result.ok ? current.error : result.error ?? "Write failed",
              }));
            }

            if (!result.ok) {
              failureMessage = result.error ?? "Write failed";
              failTargetChanges(queue, batch, failureMessage);
              for (const item of batch) {
                queue.inFlight.delete(changeId(item.change));
                forgetWrites(dir, [item.change], item.stamp);
              }
              refreshAfterFailure = true;
            } else {
              const rejectedIds = new Set((result.rejected ?? []).map(changeId));
              const rejected = batch.filter((item) => rejectedIds.has(changeId(item.change)));
              for (const item of batch) {
                if (rejectedIds.has(changeId(item.change))) {
                  queue.inFlight.delete(changeId(item.change));
                  forgetWrites(dir, [item.change], item.stamp);
                }
              }
              if (rejectedIds.size > 0) {
                refreshAfterFailure = true;
                const rejectedKeys = (result.rejected ?? []).map((item) => item.key).join(", ");
                failureMessage = `OptiScaler would not accept ${rejectedKeys}`;
                failTargetChanges(queue, rejected, failureMessage);
              }
            }
          } catch (exc) {
            failureMessage = String(exc);
            failTargetChanges(queue, batch, failureMessage);
            for (const item of batch) {
              queue.inFlight.delete(changeId(item.change));
              forgetWrites(dir, [item.change], item.stamp);
            }
            if (isCurrent(requestIdentity, requestGeneration)) {
              refreshAfterFailure = true;
            }
          } finally {
            invalidateTargetReads(dir);
            for (const item of batch) {
              const id = changeId(item.change);
              if (queue.inFlight.get(id) === item) queue.inFlight.delete(id);
            }
            if (isCurrent(requestIdentity, requestGeneration)) {
              updateView(requestIdentity, (current) => ({ ...current, saving: false }));
            }
          }
          if (refreshAfterFailure && isCurrent(requestIdentity, requestGeneration)) {
            await readTarget(dir, requestIdentity, requestGeneration);
            if (failureMessage) {
              updateView(requestIdentity, (current) => ({ ...current, error: failureMessage }));
            }
          }
        }
      };

      const startDrain = () => {
        const operation = drain();
        const wrapped = operation.finally(() => {
          if (queue.drain === wrapped) queue.drain = null;
        });
        queue.drain = wrapped;
      };
      try {
        while (queue.pending.size > 0 || queue.drain) {
          if (!queue.drain && queue.pending.size > 0) startDrain();
          if (!queue.drain) break;
          await queue.drain;
        }
        await waitForTargetWrites(dir);
        const failures = finishTargetFlush(queue, flushWaiter);
        if (failures.length > 0) throw new Error(Array.from(new Set(failures)).join("; "));
      } catch (exc) {
        finishTargetFlush(queue, flushWaiter);
        throw exc;
      }
    },
    [getQueue, isCurrent, readTarget, updateView],
  );

  const flush = useCallback(() => {
    if (!targetDir) return Promise.resolve();
    return flushTarget(targetDir, identity, generationAtRender, true);
  }, [targetDir, identity, generationAtRender, flushTarget]);

  const authoritativeReload = useCallback(async () => {
    if (!targetDir || !enabled) return;
    await flushTarget(targetDir, identity, generationAtRender, true);
    await waitForTargetWrites(targetDir);
    if (!isCurrent(identity, generationAtRender)) return;
    forgetWrites(targetDir);
    invalidateTargetReads(targetDir);
    await readTarget(targetDir, identity, generationAtRender, true);
  }, [targetDir, enabled, identity, generationAtRender, flushTarget, isCurrent, readTarget]);

  const forgetLocal = useCallback(async () => {
    if (!targetDir) return;
    await flushTarget(targetDir, identity, generationAtRender, true);
    await waitForTargetWrites(targetDir);
    if (!isCurrent(identity, generationAtRender)) return;
    forgetWrites(targetDir);
    const queue = getQueue(targetDir);
    queue.pending.clear();
    if (queue.timer !== null) window.clearTimeout(queue.timer);
    queue.timer = null;
  }, [targetDir, identity, generationAtRender, flushTarget, isCurrent, getQueue]);

  const queueChanges = useCallback(
    (changes: OptionChange[], immediate = false) => {
      if (!targetDir || changes.length === 0 || !enabled) return;
      const queue = getQueue(targetDir);
      const stamp = rememberWrites(targetDir, changes);
      queueTargetChanges(targetDir, changes, stamp);
      updateView(identity, (current) => ({ ...current, error: null }));
      if (queue.timer !== null) window.clearTimeout(queue.timer);
      if (immediate) {
        queue.timer = null;
        void flushTarget(targetDir, identity, generationAtRender).catch(() => undefined);
      } else {
        queue.timer = window.setTimeout(() => {
          queue.timer = null;
          void flushTarget(targetDir, identity, generationAtRender).catch(() => undefined);
        }, FLUSH_DELAY_MS);
      }
    },
    [targetDir, enabled, getQueue, updateView, identity, generationAtRender, flushTarget],
  );

  const setOption = useCallback(
    (option: OptionMeta, value: string) => {
      if (!targetDir || !enabled || view.runtimeCurrent !== true) return;
      updateView(identity, (current) => ({
        ...current,
        values: {
          ...current.values,
          [option.section]: { ...(current.values[option.section] ?? {}), [option.key]: value },
        },
      }));
      queueChanges([{ section: option.section, key: option.key, value }]);
    },
    [targetDir, enabled, view.runtimeCurrent, updateView, identity, queueChanges],
  );

  const setOptions = useCallback(
    (changes: OptionChange[]) => {
      if (changes.length === 0 || !targetDir || !enabled || view.runtimeCurrent !== true) return;
      updateView(identity, (current) => {
        const values = { ...current.values };
        for (const change of changes) {
          values[change.section] = {
            ...(values[change.section] ?? {}),
            [change.key]: change.value,
          };
        }
        return { ...current, values };
      });
      queueChanges(changes, true);
    },
    [targetDir, enabled, view.runtimeCurrent, updateView, identity, queueChanges],
  );

  const enableFsr4 = useCallback(async () => {
    if (!targetDir) throw new Error("Install OptiScaler first.");
    if (view.runtimeCurrent !== true) throw new Error("Upgrade runtime in Setup first.");
    updateView(identity, (current) => ({ ...current, saving: true, error: null }));
    try {
      await flushTarget(targetDir, identity, generationAtRender, true);
      if (!gameContext?.gamePath) throw new Error("Open Setup to preserve this game before changing its FSR build.");
      const preserved = await preserveExistingInstall(targetDir, gameContext.gamePath, gameContext.appid);
      if (!preserved.ok) throw new Error(preserved.error ?? "Could not preserve the previous install.");
      const result = await serializeTargetWrite(targetDir, () => setFsr4Build(targetDir, "4.1.1b"));
      if (!result.ok) throw new Error(result.error ?? "Could not enable FSR 4.1.1b");
      forgetWrites(targetDir);
      await readTarget(targetDir, identity, generationAtRender, true);
      updateView(identity, (current) => ({ ...current, dirty: true }));
    } finally {
      updateView(identity, (current) => ({ ...current, saving: false }));
    }
  }, [
    targetDir,
    view.runtimeCurrent,
    updateView,
    identity,
    generationAtRender,
    flushTarget,
    readTarget,
    gameContext?.gamePath,
    gameContext?.appid,
  ]);

  const resetSection = useCallback(
    (section: string) => {
      if (view.runtimeCurrent !== true) return;
      const options = optionsInSection(section);
      updateView(identity, (current) => {
        const next = { ...(current.values[section] ?? {}) };
        for (const option of options) next[option.key] = AUTO;
        return { ...current, values: { ...current.values, [section]: next } };
      });
      queueChanges(options.map((option) => ({ section, key: option.key, value: AUTO })));
    },
    [view.runtimeCurrent, identity, updateView, queueChanges],
  );

  useEffect(() => {
    void load();
  }, [load, reloadKey]);

  // Closing a panel or changing its target must not abandon a debounced write.
  useEffect(() => {
    if (!targetDir) return;
    const dir = targetDir;
    const requestIdentity = identity;
    const requestGeneration = generationAtRender;
    return () => {
      const queue = getQueue(dir);
      if (queue.timer !== null) window.clearTimeout(queue.timer);
      queue.timer = null;
      void flushTarget(dir, requestIdentity, requestGeneration).catch(() => undefined);
    };
  }, [targetDir, identity, generationAtRender, getQueue, flushTarget]);

  const clearDirty = useCallback(() => {
    updateView(identity, (current) => ({ ...current, dirty: false }));
  }, [identity, updateView]);
  const markDirty = useCallback(() => {
    updateView(identity, (current) => ({ ...current, dirty: true }));
  }, [identity, updateView]);
  const clearLive = useCallback(() => {
    updateView(identity, (current) => ({ ...current, live: null }));
  }, [identity, updateView]);

  return {
    values: view.values,
    fsr4Build: view.fsr4Build,
    runtimeCurrent: view.runtimeCurrent,
    enableFsr4,
    loading: view.loading || Boolean(targetDir && enabled && storedView.identity !== identity),
    error: view.error,
    dirty: view.dirty,
    saving: view.saving,
    live: view.live,
    setOption,
    setOptions,
    resetSection,
    reload: load,
    flush,
    authoritativeReload,
    forgetLocal,
    clearDirty,
    markDirty,
    clearLive,
  };
}
