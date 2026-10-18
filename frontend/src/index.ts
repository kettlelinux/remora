// Entry point, imported by the CDP bootstrap from http://127.0.0.1:1337/frontend/index.js.
// Stage one needs neither React nor @decky/ui: wait for Steam, publish SP_REACT/SP_REACTDOM/
// SP_JSX, then load @decky/ui (its own file, dfl.js, unmodified) and the rest of Remora.
import { preflightDFL } from './boot/dfl-preflight';
import { ensureReactGlobals } from './boot/react-globals';
import { waitForSteamInit } from './boot/steam-ready';
import { logger } from './util/logger';

const log = logger('boot');

async function boot(): Promise<void> {
  await waitForSteamInit();
  if (!ensureReactGlobals()) {
    log.error("Steam's React was not found; Remora cannot run in this Steam build");
    return;
  }
  if (!preflightDFL()) return;
  try {
    // The build maps @decky/ui to ./dfl.js (a separate file), so this loads it only now.
    window.DFL = await import('@decky/ui');
  } catch (e) {
    log.error('@decky/ui (dfl.js) failed to load; plugins cannot run', e);
    return;
  }
  const { start } = await import('./runtime/start');
  await start();
}

boot().catch((e) => log.error('boot failed', e));
