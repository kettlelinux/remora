// Lookup 4: the SP (Game Mode main) window. Plugins load once it exists, and the crash check
// compares it before and after each plugin loads.
import { findSP, getGamepadNavigationTrees } from '@decky/ui';
import { SteamAny } from '../steam/globals';
import { waitFor } from '../util/sleep';
import { HookHandle } from './status';

const hook = new HookHandle('sp-window', 'waiting for the Game Mode window before loading plugins; SP crash detection');

const ROOT_IDS = ['GamepadUI_Full_Root', 'root_1_', 'GamepadUI_VR_Full_Root'];

const strategies: { name: string; find: () => Window | undefined | null }[] = [
  { name: '@decky/ui findSP', find: () => findSP() },
  {
    name: 'nav tree root element',
    find: () => {
      const trees: SteamAny[] = getGamepadNavigationTrees() ?? [];
      const t = trees.find((x) => ROOT_IDS.includes(x?.m_ID)) ?? trees.find((x) => /GamepadUI/i.test(String(x?.m_ID)));
      return t?.Root?.Element?.ownerDocument?.defaultView;
    },
  },
  { name: 'document.title SP', find: () => (document.title === 'SP' ? window : undefined) },
];

export function currentSP(): Window | undefined {
  for (const s of strategies) {
    try {
      const w = s.find();
      if (w) return w;
    } catch {
      // try the next one
    }
  }
  return undefined;
}

/** Waits for the SP window; resolves anyway after `timeout` ms so plugins still load. */
export async function waitForSP(timeout = 30_000): Promise<Window | undefined> {
  const start = Date.now();
  let matched = '';
  const w = await waitFor(
    () => {
      for (const s of strategies) {
        try {
          const v = s.find();
          if (v) {
            matched = s.name;
            return v;
          }
        } catch {
          // next
        }
      }
      return undefined;
    },
    100,
    timeout,
  );
  if (w) hook.ok(matched, `after ${Date.now() - start} ms`);
  else hook.degraded('timeout', `no SP window after ${timeout / 1000} s; loading plugins anyway`);
  return w;
}
