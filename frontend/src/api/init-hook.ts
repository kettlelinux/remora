// The @decky/api init hook: window.__DECKY_SECRET_INTERNALS_..._deckyLoaderAPIInit. Every
// modern plugin bundles @decky/api, which calls connect(2, name) (falling back to 1) when the
// plugin module is evaluated. connect never throws, and each call gives that plugin a fresh
// event listener map, so a reloaded plugin starts with no stale listeners.
import type { Backend, EventListener } from '../runtime/backend';
import type { RouterHook } from '../hooks/router';
import type { Toaster } from '../hooks/toaster';
import { logger } from '../util/logger';
import { fetchNoCors, getExternalResourceURL } from './fetch';
import { filePickerApi } from './file-picker';
import { tabApi } from './tabs';
import { useQuickAccessVisible } from '../ui/qam/visibility';

const log = logger('api');
const SUPPORTED = 2;

export interface InitHook {
  connect(version: number, pluginName: string): Record<string, unknown>;
}

interface Deps {
  backend: Backend;
  routerHook: RouterHook;
  toaster: Toaster;
}

export class PluginApiHost implements InitHook {
  private listeners = new Map<string, Map<string, Set<EventListener>>>();
  private shared: Record<string, unknown>;

  constructor(private deps: Deps) {
    const { openFilePicker } = filePickerApi(deps.backend);
    this.shared = {
      openFilePicker,
      ...tabApi(deps.backend),
      fetchNoCors,
      getExternalResourceURL,
      routerHook: deps.routerHook,
      toaster: deps.toaster,
    };
  }

  connect = (version: number, pluginName: string): Record<string, unknown> => {
    if (typeof version !== 'number' || version < 1 || version > SUPPORTED) {
      log.warn(`plugin ${pluginName} asked for API version ${version}; Remora provides 1..${SUPPORTED}`);
    }
    const name = String(pluginName);
    const map = new Map<string, Set<EventListener>>();
    this.listeners.set(name, map);
    const { backend } = this.deps;
    const call = (method: string, ...args: unknown[]) => backend.call('loader/call_plugin_method', name, method, ...args);
    const api: Record<string, unknown> = {
      ...this.shared,
      call,
      callable:
        (method: string) =>
        (...args: unknown[]) =>
          call(method, ...args),
      addEventListener: (event: string, listener: EventListener) => {
        let set = map.get(event);
        if (!set) map.set(event, (set = new Set()));
        set.add(listener);
        return listener;
      },
      removeEventListener: (event: string, listener: EventListener) => {
        map.get(event)?.delete(listener);
      },
      _version: 1,
    };
    if (version >= 2) {
      api.useQuickAccessVisible = useQuickAccessVisible;
      api._version = 2;
    }
    return api;
  };

  /** Fans a backend `loader/plugin_event` out to that plugin's listeners. */
  dispatch(plugin: string, event: string, args: unknown[]): void {
    const set = this.listeners.get(plugin)?.get(event);
    if (!set) return;
    for (const l of [...set]) {
      try {
        const r = l(...args);
        if (r && typeof (r as Promise<unknown>).catch === 'function') {
          (r as Promise<unknown>).catch((e) => log.error(`${plugin}: async listener for ${event} failed`, e));
        }
      } catch (e) {
        log.error(`${plugin}: listener for ${event} failed`, e);
      }
    }
  }

  forget(plugin: string): void {
    this.listeners.delete(plugin);
  }
}
