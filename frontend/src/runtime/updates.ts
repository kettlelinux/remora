// Plugin update badges from remora/store/updates: checked 30 s after boot, every 6 h, and after
// an install finishes. Frozen plugins are skipped.
import { isNewer } from '../util/semver';
import { logger } from '../util/logger';
import { Store } from '../util/store';
import type { Backend } from './backend';
import type { Settings } from './settings';

export interface UpdateInfo {
  version: string;
  hash: string;
  artifact: string;
  source: string;
}

const log = logger('updates');
const FIRST_CHECK_MS = 30_000;
const INTERVAL_MS = 6 * 60 * 60 * 1000;

export class Updates {
  readonly available = new Store<Record<string, UpdateInfo>>({});
  private timers: ReturnType<typeof setTimeout>[] = [];

  constructor(
    private backend: Backend,
    private settings: Settings,
    private installedVersion: (name: string) => string | undefined,
  ) {}

  start(): void {
    this.timers.push(setTimeout(() => void this.check(), FIRST_CHECK_MS));
    this.timers.push(setInterval(() => void this.check(), INTERVAL_MS));
  }

  async check(): Promise<void> {
    try {
      const all = await this.backend.call<Record<string, UpdateInfo>>('remora/store/updates');
      const frozen = new Set(this.settings.get('frozenPlugins'));
      const out: Record<string, UpdateInfo> = {};
      for (const [name, info] of Object.entries(all ?? {})) {
        if (frozen.has(name) || !info?.version) continue;
        const installed = this.installedVersion(name);
        // The backend may list every latest version or only real updates; check either way.
        if (installed !== undefined && isNewer(info.version, installed)) out[name] = info;
      }
      this.available.set(out);
      if (Object.keys(out).length) log.info(`updates available: ${Object.keys(out).join(', ')}`);
    } catch (e) {
      log.warn('update check failed', e);
    }
  }

  stop(): void {
    for (const t of this.timers) clearTimeout(t);
    this.timers = [];
  }
}
