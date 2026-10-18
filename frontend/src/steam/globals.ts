// Loose access to Steam's own globals. Their shapes change between client builds, so they are
// typed as `any` here and only here; callers check what they use.
/* eslint-disable @typescript-eslint/no-explicit-any */
export type SteamAny = any;

const w = (): SteamAny => window as SteamAny;

export const steamClient = (): SteamAny => w().SteamClient;
export const steamApp = (): SteamAny => w().App;
export const notificationStore = (): SteamAny => w().NotificationStore;
export const securityStore = (): SteamAny => w().securitystore;
export const steamUIStore = (): SteamAny => w().SteamUIStore;
export const webpackChunks = (): SteamAny => w().webpackChunksteamui;
export const anyGlobal = (name: string): SteamAny => w()[name];

/** Calls a SteamClient method by path, returning undefined if any part is missing or it throws. */
export function callSteam(path: string, ...args: unknown[]): unknown {
  try {
    const parts = path.split('.');
    let obj: SteamAny = steamClient();
    for (let i = 0; i < parts.length - 1; i++) obj = obj?.[parts[i]!];
    const fn = obj?.[parts[parts.length - 1]!];
    return typeof fn === 'function' ? fn.apply(obj, args) : undefined;
  } catch (e) {
    console.warn(`[Remora] SteamClient.${path} failed`, e);
    return undefined;
  }
}
