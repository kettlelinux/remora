// Lookup 10: navigation. @decky/ui's Navigation goes through SteamUIStore and its Router module;
// when that is missing, Remora talks to the focused window instance itself.
import { Navigation, QuickAccessTab, Router } from '@decky/ui';
import { SteamAny, steamUIStore } from '../steam/globals';
import { HookHandle } from './status';

const hook = new HookHandle('navigation', 'opening Remora pages and the Quick Access menu from code');

function windowInstance(): SteamAny {
  try {
    const w = steamUIStore()?.GetFocusedWindowInstance?.();
    if (w) return w;
  } catch {
    // fall through
  }
  const r = Router as SteamAny;
  return r?.WindowStore?.GamepadUIMainWindowInstance ?? r?.WindowStore?.SteamUIWindows?.[0] ?? steamUIStore()?.WindowStore?.GamepadUIMainWindowInstance;
}

export function checkNavigation(): void {
  const nav = Navigation as SteamAny;
  if (typeof nav?.Navigate === 'function' && Router) hook.ok('@decky/ui Navigation');
  else if (windowInstance()) hook.degraded('SteamUIStore window instance', '@decky/ui Router not found');
  else hook.fail('no Router module and no window instance');
}

function viaWindow(fn: (w: SteamAny) => void): boolean {
  const w = windowInstance();
  if (!w) return false;
  try {
    fn(w);
    return true;
  } catch (e) {
    console.warn('[Remora] navigation fallback failed', e);
    return false;
  }
}

export function navigate(path: string): void {
  const nav = Navigation as SteamAny;
  try {
    if (typeof nav?.Navigate === 'function' && Router) return nav.Navigate(path);
  } catch {
    // fall back below
  }
  viaWindow((w) => w.Navigate(path));
}

export function navigateBack(): void {
  const nav = Navigation as SteamAny;
  try {
    if (typeof nav?.NavigateBack === 'function' && Router) return nav.NavigateBack();
  } catch {
    // fall back below
  }
  viaWindow((w) => w.NavigateBack());
}

export function closeSideMenus(): void {
  const nav = Navigation as SteamAny;
  try {
    if (typeof nav?.CloseSideMenus === 'function' && Router) return nav.CloseSideMenus();
  } catch {
    // fall back below
  }
  viaWindow((w) => w.MenuStore.CloseSideMenus());
}

export function openQuickAccess(tab: number = QuickAccessTab.Decky): void {
  const nav = Navigation as SteamAny;
  try {
    if (typeof nav?.OpenQuickAccessMenu === 'function' && Router) return nav.OpenQuickAccessMenu(tab);
  } catch {
    // fall back below
  }
  viaWindow((w) => w.MenuStore.OpenQuickAccessMenu(tab));
}

/** Opens a full-screen page and closes the side menus, the way Steam's own menus do. */
export function openPage(path: string): void {
  navigate(path);
  closeSideMenus();
}
