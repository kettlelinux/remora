// Per-plugin process state from remora/status and remora/plugin_status.
import { logger } from '../util/logger';
import { Store } from '../util/store';
import type { Backend } from './backend';

export type PluginState = 'running' | 'starting' | 'stopped' | 'crashed' | 'disabled' | 'passive' | 'blocked';

export interface PluginStatus {
  name: string;
  dir: string;
  version: string;
  loadType: number;
  state: PluginState;
  restarts: number;
  error: string | null;
  root: boolean;
  approved: boolean;
}

export interface RemoraStatus {
  version: string;
  deckyVersion: string;
}

const log = logger('status');

export class StatusStore {
  readonly plugins = new Store<Record<string, PluginStatus>>({});
  readonly loader = new Store<RemoraStatus | undefined>(undefined);

  constructor(private backend: Backend) {}

  listen(onChange: (s: PluginStatus, prev?: PluginStatus) => void): () => void {
    const l = (s: unknown) => {
      if (!s || typeof s !== 'object' || typeof (s as PluginStatus).name !== 'string') return;
      const st = s as PluginStatus;
      const prev = this.plugins.get()[st.name];
      this.plugins.update((all) => ({ ...all, [st.name]: st }));
      onChange(st, prev);
    };
    this.backend.addEventListener('remora/plugin_status', l);
    return () => this.backend.removeEventListener('remora/plugin_status', l);
  }

  async refresh(): Promise<void> {
    try {
      const r = await this.backend.call<{ version: string; deckyVersion: string; plugins: PluginStatus[] }>('remora/status');
      this.loader.set({ version: r.version, deckyVersion: r.deckyVersion });
      const map: Record<string, PluginStatus> = {};
      for (const p of r.plugins ?? []) map[p.name] = p;
      this.plugins.set(map);
    } catch (e) {
      log.warn('remora/status failed', e);
    }
  }
}
