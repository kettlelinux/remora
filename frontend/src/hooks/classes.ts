// Lookup 12: CSS class maps. Missing maps give empty class names, so things look plainer but work.
import { findClassModule, gamepadDialogClasses, quickAccessMenuClasses, staticClasses } from '@decky/ui';
import { SteamAny } from '../steam/globals';
import { HookHandle } from './status';

const hook = new HookHandle('class-maps', 'Steam styling of Remora pages (they fall back to plain styles)');

type ClassMap = Record<string, string>;
const empty: ClassMap = new Proxy({}, { get: () => '' });

function safe(m: unknown): ClassMap {
  return m && typeof m === 'object' ? (m as ClassMap) : empty;
}

export const classes = {
  static: safe(staticClasses),
  qam: safe(quickAccessMenuClasses),
  dialog: safe(gamepadDialogClasses),
  toast: empty as ClassMap,
};

export function checkClassMaps(): void {
  const missing: string[] = [];
  if (!staticClasses?.Title) missing.push('staticClasses');
  if (!quickAccessMenuClasses?.Title) missing.push('quickAccessMenuClasses');
  if (!gamepadDialogClasses?.Field) missing.push('gamepadDialogClasses');
  try {
    const toast = findClassModule((m: SteamAny) => m.ShortTemplate && m.StandardTemplateContainer) ?? findClassModule((m: SteamAny) => m.ShortTemplate);
    if (toast) classes.toast = toast as ClassMap;
    else missing.push('toast classes');
  } catch {
    missing.push('toast classes');
  }
  if (!missing.length) hook.ok('key presence');
  else if (missing.length < 4) hook.degraded('key presence', `missing: ${missing.join(', ')}`);
  else hook.fail(`missing: ${missing.join(', ')}`);
}
