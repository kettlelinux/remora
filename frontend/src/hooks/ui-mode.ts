// UI mode (Game Mode vs desktop) from SteamClient.UI. Plugins only run in Game Mode's UI, so
// plugin loading and the router patch wait for the first report.
import { EUIMode } from '@decky/ui';
import { steamClient } from '../steam/globals';
import { Store } from '../util/store';
import { HookHandle } from './status';

const hook = new HookHandle('ui-mode', 'waiting for Game Mode before loading plugins (falls back to a timeout)');

/** undefined until the first report. */
export const uiMode = new Store<EUIMode | undefined>(undefined);

let unregister: (() => void) | undefined;

export function watchUIMode(fallbackMs = 10_000): void {
  try {
    const reg = steamClient()?.UI?.RegisterForUIModeChanged;
    if (typeof reg === 'function') {
      const h = reg.call(steamClient().UI, (mode: EUIMode) => uiMode.set(mode));
      unregister = () => h?.unregister?.();
      hook.ok('SteamClient.UI.RegisterForUIModeChanged');
    }
  } catch (e) {
    hook.fail(String(e));
  }
  setTimeout(() => {
    if (uiMode.get() !== undefined) return;
    hook.degraded('timeout', `no UI mode report after ${fallbackMs / 1000} s; assuming Game Mode`);
    uiMode.set(EUIMode.GamePad);
  }, fallbackMs);
}

export function stopWatchingUIMode(): void {
  unregister?.();
}

export function firstUIMode(): Promise<EUIMode> {
  return new Promise((resolve) => {
    const now = uiMode.get();
    if (now !== undefined) return resolve(now);
    const off = uiMode.subscribe((m) => {
      if (m === undefined) return;
      off();
      resolve(m);
    });
  });
}
