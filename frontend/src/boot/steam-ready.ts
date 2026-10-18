// Lookup 3: Steam init readiness. @decky/ui builds its webpack cache when it is evaluated, so it
// must not load before Steam has registered its modules.
import { HookHandle } from '../hooks/status';
import { steamApp, webpackChunks } from '../steam/globals';
import { sleep } from '../util/sleep';

const hook = new HookHandle('steam-init', 'Remora waits for this before it starts');

const checks: { name: string; test: () => boolean | undefined }[] = [
  { name: 'App.BFinishedInitBeforeLogin', test: () => steamApp()?.BFinishedInitBeforeLogin?.() },
  { name: 'App.BFinishedInitStageOne', test: () => steamApp()?.BFinishedInitStageOne?.() },
];

/** Resolves once Steam's UI has initialised; after `giveUpMs` it continues with a warning. */
export async function waitForSteamInit(giveUpMs = 60_000): Promise<void> {
  const start = Date.now();
  for (;;) {
    for (const c of checks) {
      try {
        if (c.test()) {
          hook.ok(c.name, `after ${Date.now() - start} ms`);
          return;
        }
      } catch {
        // App not built yet
      }
    }
    if (Date.now() - start > giveUpMs && Array.isArray(webpackChunks())) {
      hook.degraded('timeout', `no init flag after ${giveUpMs / 1000} s; starting anyway`);
      return;
    }
    await sleep(50);
  }
}
