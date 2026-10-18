// loader.json settings through utilities/settings/get|set. Keys Decky uses keep their names so a
// Decky install's ordering, hidden and frozen lists carry over.
import { logger } from '../util/logger';
import { Store } from '../util/store';
import type { Backend } from './backend';

const log = logger('settings');

export interface LoaderSettings {
  pluginOrder: string[];
  hiddenPlugins: string[];
  frozenPlugins: string[];
  sortPlugins: boolean;
  'developer.enabled': boolean;
  'remora.verbose': boolean;
  'remora.storeTelemetry': boolean;
}

const DEFAULTS: LoaderSettings = {
  pluginOrder: [],
  hiddenPlugins: [],
  frozenPlugins: [],
  sortPlugins: false,
  'developer.enabled': false,
  'remora.verbose': false,
  'remora.storeTelemetry': false,
};

export class Settings {
  readonly values = new Store<LoaderSettings>({ ...DEFAULTS });
  constructor(private backend: Backend) {}

  async load(): Promise<void> {
    const keys = Object.keys(DEFAULTS) as (keyof LoaderSettings)[];
    const results = await Promise.allSettled(keys.map((k) => this.backend.call('utilities/settings/get', k, DEFAULTS[k])));
    const next = { ...DEFAULTS } as Record<string, unknown>;
    results.forEach((r, i) => {
      const k = keys[i]!;
      if (r.status === 'fulfilled' && r.value !== null && r.value !== undefined && typeof r.value === typeof DEFAULTS[k]) next[k] = r.value;
      else if (r.status === 'rejected') log.warn(`could not read ${k}`, r.reason);
    });
    this.values.set(next as unknown as LoaderSettings);
  }

  get<K extends keyof LoaderSettings>(key: K): LoaderSettings[K] {
    return this.values.get()[key];
  }

  async set<K extends keyof LoaderSettings>(key: K, value: LoaderSettings[K]): Promise<void> {
    this.values.update((v) => ({ ...v, [key]: value }));
    try {
      await this.backend.call('utilities/settings/set', key, value);
    } catch (e) {
      log.error(`could not save ${key}`, e);
    }
  }

  async toggleInList(key: 'hiddenPlugins' | 'frozenPlugins', name: string, on: boolean): Promise<void> {
    const list = this.get(key).filter((n) => n !== name);
    if (on) list.push(name);
    await this.set(key, list);
  }
}
