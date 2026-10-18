// Health of every fragile Steam lookup. Each lookup tries an ordered list of strategies, logs
// which one matched, and records the result here so the settings page can show it. A failed
// lookup only turns off its own feature.
import { logger } from '../util/logger';
import { Store } from '../util/store';

export type HookState = 'pending' | 'ok' | 'degraded' | 'failed';

export interface HookStatus {
  id: string;
  /** What stops working when this lookup fails (shown in the UI, English). */
  feature: string;
  state: HookState;
  /** Name of the strategy that matched. */
  strategy?: string;
  detail?: string;
  updated: number;
}

export const hookStatuses = new Store<HookStatus[]>([]);
const log = logger('hooks');

// The current state is kept here synchronously (HookHandle.state reads it right after a write);
// the Store is published in a microtask, because some writes happen while React renders (router
// transform, QAM render patch) and a Store listener must not set state during a render.
const current = new Map<string, HookStatus>();
let flushQueued = false;

function same(a: HookStatus | undefined, b: HookStatus): boolean {
  return !!a && a.state === b.state && a.strategy === b.strategy && a.detail === b.detail && a.feature === b.feature;
}

function put(s: HookStatus): void {
  if (same(current.get(s.id), s)) return;
  current.set(s.id, s);
  if (flushQueued) return;
  flushQueued = true;
  queueMicrotask(() => {
    flushQueued = false;
    hookStatuses.set([...current.values()]);
  });
}

export class HookHandle {
  constructor(
    readonly id: string,
    readonly feature: string,
  ) {
    if (!current.has(id)) put({ id, feature, state: 'pending', updated: Date.now() });
  }

  ok(strategy: string, detail?: string): void {
    log.info(`${this.id}: ok via "${strategy}"${detail ? ` (${detail})` : ''}`);
    put({ id: this.id, feature: this.feature, state: 'ok', strategy, detail, updated: Date.now() });
  }

  degraded(strategy: string, detail: string): void {
    log.warn(`${this.id}: degraded via "${strategy}": ${detail}`);
    put({ id: this.id, feature: this.feature, state: 'degraded', strategy, detail, updated: Date.now() });
  }

  fail(detail: string): void {
    log.error(`${this.id}: FAILED: ${detail}. Off: ${this.feature}`);
    put({ id: this.id, feature: this.feature, state: 'failed', detail, updated: Date.now() });
  }

  pending(detail?: string): void {
    put({ id: this.id, feature: this.feature, state: 'pending', detail, updated: Date.now() });
  }

  get state(): HookState {
    return current.get(this.id)?.state ?? 'pending';
  }
}

export interface Strategy<T> {
  name: string;
  find: () => T | undefined | null | false;
}

/**
 * Runs strategies in order and returns the first truthy result. A strategy that throws is
 * logged and skipped. Records the outcome on `handle` unless `record` is false.
 */
export function runStrategies<T>(handle: HookHandle, strategies: Strategy<T>[], record = true): { value: T; strategy: string } | undefined {
  const errors: string[] = [];
  for (const s of strategies) {
    try {
      const v = s.find();
      if (v) {
        if (record) handle.ok(s.name);
        return { value: v, strategy: s.name };
      }
      errors.push(`${s.name}: no match`);
    } catch (e) {
      errors.push(`${s.name}: ${e instanceof Error ? e.message : String(e)}`);
    }
  }
  if (record) handle.fail(errors.join('; ') || 'no strategies');
  return undefined;
}
