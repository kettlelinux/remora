// Globals Remora reads or publishes in Steam's SharedJSContext. Steam's own objects are typed
// loosely on purpose: their shape changes between client builds, and every access goes through
// a defensive lookup.

import type { InitHook } from './api/init-hook';
import type { Backend } from './runtime/backend';
import type { PluginLoader } from './runtime/plugin-loader';

declare global {
  interface Window {
    // Set by the CDP bootstrap.
    deckyHasLoaded?: boolean;
    deckyAuthToken?: string;
    remoraAuthToken?: string;
    deckyFallbackLoaded?: boolean;

    // Published by Remora (names kept for plugin compatibility).
    DFL?: typeof import('@decky/ui');
    DeckyBackend?: Backend;
    DeckyPluginLoader?: PluginLoader;
    __DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit?: InitHook;
    __ROUTER_HOOK_INSTANCE?: { deinit?: () => void };
    __TABS_HOOK_INSTANCE?: { deinit?: () => void };
    __TOASTER_INSTANCE?: { deinit?: () => void };
    __ERRORBOUNDARY_HOOK_INSTANCE?: { deinit?: () => void };
    remora?: { version: string; hooks: () => unknown };
  }
}

export {};
