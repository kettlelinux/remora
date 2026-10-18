// State of Remora's Quick Access tab: whether the QAM is visible and which plugin is open in it.
import { Store } from '../util/store';

export const qamVisible = new Store<boolean>(false);
export const activePlugin = new Store<string | null>(null);

// Steam's QAM component passed `visible` as a render argument in the builds Decky was written
// against; the current one doesn't. Until it shows up, the documents the mounted panels render
// in decide: the QAM is visible while any live panel's document is visible. More than one panel
// can be mounted (a stale tab element, a second window), so each one reports for itself and
// drops out when it unmounts.
let renderArgSeen = false;
const panels = new Map<symbol, boolean>();

let desired = false;
let queued = false;

function publish(visible: boolean): void {
  desired = visible;
  if (queued) return;
  queued = true;
  queueMicrotask(() => {
    queued = false;
    qamVisible.set(desired);
  });
}

/** Steam calls this from render; listeners run after the render, never during it. */
export function reportQamVisible(visible: unknown): void {
  if (typeof visible !== 'boolean') return;
  renderArgSeen = true;
  publish(visible);
}

function recompute(): void {
  if (renderArgSeen) return;
  publish([...panels.values()].some(Boolean));
}

/** One mounted QAM panel: report() its document's visibility, dispose() on unmount. */
export function registerQamPanel(): { report: (visible: boolean) => void; dispose: () => void } {
  const id = Symbol('qam-panel');
  panels.set(id, false);
  return {
    report: (visible) => {
      if (!panels.has(id)) return;
      panels.set(id, visible);
      recompute();
    },
    dispose: () => {
      panels.delete(id);
      recompute();
    },
  };
}
