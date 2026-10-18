// Loads, reloads and unloads plugin frontends, and owns the shared hooks. Published as
// window.DeckyPluginLoader: TabMaster reads pluginReloadQueue and routerHook.routerState, and
// Kettle's welcome plugin calls deckyState.setActivePlugin(name).
import { EUIMode } from '@decky/ui';
import { createServerAPI } from '../api/server-api';
import { PluginApiHost } from '../api/init-hook';
import { ErrorBoundaryHook } from '../hooks/error-boundary';
import { ErrorReportFilter } from '../hooks/error-reporting';
import { TabsHook } from '../hooks/qam-tab';
import { RouterHook } from '../hooks/router';
import { currentSP, waitForSP } from '../hooks/sp-window';
import { Toaster } from '../hooks/toaster';
import { firstUIMode } from '../hooks/ui-mode';
import { PluginLoadError } from '../ui/common/PluginLoadError';
import { t } from '../ui/i18n';
import { RemoraIcon, WarningIcon } from '../ui/icons';
import { QamPanel } from '../ui/qam/QamPanel';
import { errorStack, errorText } from '../util/errors';
import { logger } from '../util/logger';
import { sleep, withTimeout } from '../util/sleep';
import { Store } from '../util/store';
import type { Backend } from './backend';
import { InstallFlow } from './install-flow';
import { LOAD_ESM, type InstalledPlugin, type LoadedPlugin, type PluginDefinition, type QueuedImport } from './plugin';
import { activePlugin } from './qam-state';
import { Settings } from './settings';
import { StatusStore } from './status-store';
import { currentToken } from './token';
import { Updates } from './updates';

const log = logger('loader');
const PLUGIN_BASE = 'http://127.0.0.1:1337/plugins/';
/** Boot and resync imports: generous, but one hung plugin must not hold up the rest. */
const BOOT_TIMEOUT_MS = 60_000;
const EVENT_TIMEOUT_MS = 15_000;

class TimeoutError extends Error {}

interface ImportRequest extends QueuedImport {
  gen: number;
}

export class PluginLoader {
  readonly plugins = new Store<LoadedPlugin[]>([]);
  readonly installed = new Store<InstalledPlugin[]>([]);
  readonly pluginReloadQueue: QueuedImport[] = [];
  readonly routerHook = new RouterHook();
  readonly toaster = new Toaster();
  readonly settings: Settings;
  readonly status: StatusStore;
  readonly updates: Updates;
  readonly installs: InstallFlow;
  readonly api: PluginApiHost;
  readonly deckyState = {
    setActivePlugin: (name: string | null) => activePlugin.set(name),
    closeActivePlugin: () => activePlugin.set(null),
    get activePlugin(): string | null {
      return activePlugin.get();
    },
  };

  private tabsHook = new TabsHook(
    () => <RemoraIcon size={22} />,
    () => <QamPanel loader={this} />,
  );
  private errorBoundaryHook = new ErrorBoundaryHook();
  private errorReportFilter = new ErrorReportFilter();
  private reloadLock = false;
  private offs: (() => void)[] = [];
  private stopped = false;
  /**
   * Per-name generation: every import request and every unload bumps it. An import whose
   * generation is no longer current when its turn comes is skipped; one that finishes after it
   * was superseded gets onDismount() instead of being added. So an unload or disable that
   * arrives while an import is in flight wins, and the newest of two imports wins.
   */
  private gens = new Map<string, number>();
  /** Per-name chain: imports of one name run one after the other, never interleaved. */
  private chains = new Map<string, Promise<void>>();
  /** Import requests not finished yet, by name (for the reconnect resync). */
  private requested = new Map<string, { gen: number; version?: string }>();
  private bootLoad?: Promise<void>;
  private resyncing = false;
  private resyncAgain = false;
  private installedSeq = 0;
  private spRestarting = false;

  constructor(readonly backend: Backend) {
    this.settings = new Settings(backend);
    this.status = new StatusStore(backend);
    this.installs = new InstallFlow(backend, this.toaster, () => {
      void this.refreshInstalled().then(() => this.updates.check());
      void this.settings.load();
    });
    this.updates = new Updates(backend, this.settings, (name) => this.installed.get().find((p) => p.name === name)?.version);
    this.api = new PluginApiHost({ backend, routerHook: this.routerHook, toaster: this.toaster });
  }

  /** Each step is isolated: a failing hook or request turns off only its own feature. */
  async init(registerPages: (loader: PluginLoader) => void): Promise<void> {
    this.listen();
    for (const [name, fn] of [
      ['error boundary', () => this.errorBoundaryHook.init()],
      ['error report filter', () => this.errorReportFilter.init()],
      ['toaster', () => this.toaster.init()],
      ['QAM tab', () => this.tabsHook.init()],
      ['router', () => this.routerHook.init()],
      ['pages', () => registerPages(this)],
    ] as const) {
      try {
        fn();
      } catch (e) {
        log.error(`${name} setup failed`, e);
      }
    }
    window.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit = this.api;

    await Promise.allSettled([this.settings.load(), this.status.refresh()]);
    // Neither the update timers nor the SP check wait for plugin imports.
    this.updates.start();
    const bootLoad = (this.bootLoad = this.loadPlugins().catch((e) => log.error('loading plugins failed', e)));
    void Promise.race([bootLoad, sleep(BOOT_TIMEOUT_MS + 5000)]).then(() => {
      const sp = currentSP();
      setTimeout(() => void this.checkSP(sp), 5000);
    });
    await bootLoad;
  }

  private listen(): void {
    const on = (event: string, fn: (...a: unknown[]) => unknown) => {
      this.backend.addEventListener(event, fn);
      this.offs.push(() => this.backend.removeEventListener(event, fn));
    };
    on('loader/import_plugin', (name, version, loadType, useQueue = true, timeoutMs = EVENT_TIMEOUT_MS) =>
      this.importPlugin(String(name), version as string | undefined, Number(loadType), useQueue !== false, Number(timeoutMs) || EVENT_TIMEOUT_MS)
        .catch((e) => log.error(`import of ${String(name)} failed`, e))
        .finally(() => void this.refreshInstalled()),
    );
    on('loader/unload_plugin', (name) => {
      this.unloadPlugin(String(name));
      void this.refreshInstalled();
      void this.settings.load();
    });
    on('loader/disable_plugin', (name) => {
      this.unloadPlugin(String(name));
      this.installed.update((all) => all.map((p) => (p.name === name ? { ...p, disabled: true } : p)));
    });
    on('loader/plugin_event', (payload) => {
      const p = payload as { plugin?: string; event?: string; args?: unknown[] } | undefined;
      if (p?.plugin && p.event) this.api.dispatch(p.plugin, p.event, Array.isArray(p.args) ? p.args : []);
    });
    this.offs.push(
      this.status.listen((s, prev) => {
        if (s.state === 'crashed' && prev?.state !== 'crashed') {
          this.toaster.toast({ title: t('plugin.crashed', { name: s.name }), body: s.error ?? t('plugin.restarted', { count: s.restarts }), logo: <WarningIcon /> });
        }
      }),
    );
    // Events sent while the socket was down are lost: after every reconnect, re-read the state.
    const baseline = Math.max(this.backend.opens.get(), 1);
    this.offs.push(
      this.backend.opens.subscribe((n) => {
        if (n > baseline) void this.resync();
      }),
    );
    this.installs.listen();
  }

  async loadPlugins(): Promise<void> {
    const mode = await firstUIMode();
    if (mode === EUIMode.GamePad) await waitForSP();
    let list: InstalledPlugin[] | undefined;
    for (let attempt = 0; attempt < 5 && !list && !this.stopped; attempt++) {
      try {
        list = await this.backend.call<InstalledPlugin[]>('loader/get_plugins');
      } catch (e) {
        log.warn('loader/get_plugins failed; retrying', e);
        await sleep(2000);
      }
    }
    if (!list || this.stopped) return;
    this.installed.set(list);
    await Promise.allSettled(list.filter((p) => !p.disabled).map((p) => this.importPlugin(p.name, p.version, p.load_type, false, BOOT_TIMEOUT_MS)));
    log.info(`${this.plugins.get().length} plugin(s) loaded`);
  }

  /** Re-reads loader/get_plugins (after install, uninstall, enable). The newest answer wins. */
  async refreshInstalled(): Promise<void> {
    const seq = ++this.installedSeq;
    try {
      const list = await this.backend.call<InstalledPlugin[]>('loader/get_plugins');
      if (seq === this.installedSeq) this.installed.set(list);
    } catch (e) {
      log.warn('loader/get_plugins failed', e);
    }
  }

  /** After a reconnect: refresh state, then import missing, reload changed, unload removed plugins. */
  private async resync(): Promise<void> {
    if (this.stopped) return;
    if (this.resyncing) {
      this.resyncAgain = true;
      return;
    }
    this.resyncing = true;
    try {
      await this.bootLoad; // diff against a finished boot, not a half-loaded one
      do {
        this.resyncAgain = false;
        await Promise.allSettled([this.status.refresh(), this.settings.load()]);
        let list: InstalledPlugin[];
        try {
          list = await this.backend.call<InstalledPlugin[]>('loader/get_plugins');
        } catch (e) {
          log.warn('resync: loader/get_plugins failed', e);
          return;
        }
        if (this.stopped) return;
        this.installedSeq++;
        this.installed.set(list);
        this.applyDiff(list);
      } while (this.resyncAgain && !this.stopped);
    } finally {
      this.resyncing = false;
    }
  }

  private applyDiff(list: InstalledPlugin[]): void {
    const want = new Map(list.filter((p) => !p.disabled).map((p) => [p.name, p]));
    const known = new Set([...this.plugins.get().map((p) => p.name), ...this.requested.keys()]);
    for (const name of known) {
      if (want.has(name)) continue;
      log.info(`resync: unloading ${name}`);
      this.unloadPlugin(name);
    }
    for (const p of want.values()) {
      const req = this.requested.get(p.name);
      const loaded = this.plugins.get().find((x) => x.name === p.name);
      if (req ? req.version === p.version : loaded && loaded.version === p.version && !loaded.loadError) continue;
      log.info(`resync: ${loaded || req ? 'reloading' : 'importing'} ${p.name} ${p.version ?? ''}`);
      void this.importPlugin(p.name, p.version, p.load_type, false, BOOT_TIMEOUT_MS).catch((e) => log.error(`resync import of ${p.name} failed`, e));
    }
  }

  private bump(name: string): number {
    const gen = (this.gens.get(name) ?? 0) + 1;
    this.gens.set(name, gen);
    return gen;
  }

  private isCurrent(name: string, gen: number): boolean {
    return !this.stopped && this.gens.get(name) === gen;
  }

  async importPlugin(name: string, version: string | undefined, loadType: number, useQueue = true, timeoutMs?: number): Promise<void> {
    if (this.stopped) return;
    const gen = this.bump(name);
    this.requested.set(name, { gen, version });
    return this.runImport({ name, version, loadType, timeoutMs, gen }, useQueue);
  }

  private async runImport(req: ImportRequest, useQueue: boolean): Promise<void> {
    if (useQueue && this.reloadLock) {
      this.pluginReloadQueue.push(req);
      return;
    }
    if (useQueue) this.reloadLock = true;
    try {
      await this.serialImport(req);
    } finally {
      if (useQueue) {
        this.reloadLock = false;
        const next = this.pluginReloadQueue.shift();
        if (next) {
          // entries pushed by someone else carry no generation: they count as a new request
          const queued: ImportRequest = typeof (next as Partial<ImportRequest>).gen === 'number' ? (next as ImportRequest) : { ...next, gen: this.bump(next.name) };
          void this.runImport(queued, true).catch((e) => log.error(`queued import of ${next.name} failed`, e));
        }
      }
    }
  }

  private serialImport(req: ImportRequest): Promise<void> {
    const prev = this.chains.get(req.name) ?? Promise.resolve();
    const run = prev.then(() => this.doImport(req));
    const tail = run.catch(() => {});
    this.chains.set(req.name, tail);
    void tail.then(() => {
      if (this.chains.get(req.name) === tail) this.chains.delete(req.name);
      const r = this.requested.get(req.name);
      if (r?.gen === req.gen) this.requested.delete(req.name);
    });
    return run;
  }

  private async doImport(req: ImportRequest): Promise<void> {
    const { name, version, loadType, timeoutMs, gen } = req;
    if (!this.isCurrent(name, gen)) {
      log.debug(`${name}: import superseded before it started`);
      return;
    }
    this.removeLoaded(name);
    const spBefore = currentSP();
    const started = performance.now();
    let loading: Promise<PluginDefinition> | undefined;
    try {
      let def: PluginDefinition;
      if (loadType === LOAD_ESM) {
        loading = this.importESM(name);
        def = timeoutMs ? await withTimeout(loading, timeoutMs, `import timed out after ${timeoutMs} ms`).catch(rethrowTimeout) : await loading;
      } else {
        def = await this.importLegacy(name, timeoutMs);
      }
      if (!def || typeof def !== 'object') throw new Error('the plugin factory returned no plugin object');
      if (this.settle(req, def)) log.info(`loaded ${name} ${version ?? ''} in ${Math.round(performance.now() - started)} ms`);
    } catch (e) {
      if (e instanceof TimeoutError) {
        // A late import still has to end up somewhere: shown if nothing replaced it, else dismounted.
        loading?.then(
          (def) => {
            if (!def || typeof def !== 'object') return;
            if (this.settle(req, def)) log.warn(`${name} finished loading after its timeout`);
          },
          () => {},
        );
      }
      if (!this.isCurrent(name, gen)) {
        log.info(`${name}: a superseded import failed`, e);
        return;
      }
      if (e instanceof TimeoutError) {
        // The contract re-throws and adds nothing; Remora also shows why the plugin is missing.
        log.error(`${name}: ${e.message}`);
        this.addLoadError(req, e, t('plugin.loadTimeout', { seconds: Math.round((timeoutMs ?? 0) / 1000) }));
        throw e;
      }
      log.error(`failed to load ${name}`, e);
      this.addLoadError(req, e, errorText(e) || t('plugin.loadFailedBody'));
    } finally {
      void this.checkSP(spBefore);
    }
  }

  /** Adds the plugin if its import is still the current one, else dismounts it. True if added. */
  private settle(req: ImportRequest, def: PluginDefinition): boolean {
    if (!this.isCurrent(req.name, req.gen)) {
      log.info(`${req.name}: import was superseded; dismounting its result`);
      safeDismount(req.name, def);
      return false;
    }
    this.add({ ...def, name: req.name, version: req.version, loadType: req.loadType });
    return true;
  }

  private addLoadError(req: ImportRequest, e: unknown, toastBody: string): void {
    const { name, version, loadType } = req;
    this.add({
      name,
      version,
      loadType,
      icon: <WarningIcon />,
      content: <PluginLoadError name={name} error={e} />,
      loadError: { message: errorText(e), stack: errorStack(e) },
    });
    this.toaster.toast({ title: t('plugin.loadFailed', { name }), body: toastBody, logo: <WarningIcon /> });
  }

  private importESM(name: string): Promise<PluginDefinition> {
    const url = `${PLUGIN_BASE}${name}/dist/index.js?t=${Date.now()}`;
    return (import(/* webpackIgnore: true */ url) as Promise<{ default?: unknown }>).then(async (mod) => {
      if (typeof mod.default !== 'function') throw new Error('dist/index.js has no default export function');
      return (await (mod.default as () => unknown)()) as PluginDefinition;
    });
  }

  private async importLegacy(name: string, timeoutMs?: number): Promise<PluginDefinition> {
    const ctl = new AbortController();
    const timer = timeoutMs ? setTimeout(() => ctl.abort(), timeoutMs) : undefined;
    let code: string;
    try {
      const res = await fetch(`${PLUGIN_BASE}${name}/frontend_bundle`, {
        credentials: 'include',
        headers: { 'X-Decky-Auth': currentToken() ?? '' },
        signal: ctl.signal,
      });
      if (!res.ok) throw new Error(`frontend_bundle: HTTP ${res.status}`);
      code = await res.text();
    } catch (e) {
      if (ctl.signal.aborted) throw new TimeoutError(`frontend_bundle fetch timed out after ${timeoutMs} ms`);
      throw e;
    } finally {
      clearTimeout(timer);
    }
    code += `\n//# sourceURL=decky://decky/legacy_plugin/${encodeURIComponent(name)}/index.js`;
    // Indirect eval: the bundle runs in global scope and only sees globals (DFL, SP_REACT...).
    const factory = (0, eval)(code) as unknown;
    if (typeof factory !== 'function') throw new Error('the legacy bundle did not evaluate to a function');
    return (await factory(createServerAPI(name, { backend: this.backend, routerHook: this.routerHook, toaster: this.toaster }))) as PluginDefinition;
  }

  private add(plugin: LoadedPlugin): void {
    this.plugins.update((all) => [...all.filter((p) => p.name !== plugin.name), plugin]);
  }

  /** Unloads `name` and cancels any import of it that is still in flight. */
  unloadPlugin(name: string): void {
    this.bump(name);
    this.requested.delete(name);
    this.removeLoaded(name);
  }

  private removeLoaded(name: string): void {
    const p = this.plugins.get().find((x) => x.name === name);
    if (!p) return;
    safeDismount(name, p);
    // Keep the plugin's listener map registered (a reload whose @decky/api module is cached
    // never calls connect() again); connect() replaces it when it does run.
    this.api.clearListeners(name);
    this.plugins.update((all) => all.filter((x) => x !== p));
  }

  /** Steam's SP window vanishing right after a plugin loaded means the plugin crashed it. */
  private async checkSP(before: Window | undefined): Promise<void> {
    if (!before || currentSP() || this.spRestarting) return;
    // One negative lookup is not enough (the window can be mid-reload): look again over ~2 s.
    for (let i = 0; i < 3; i++) {
      await sleep(700);
      if (this.stopped || currentSP()) return;
    }
    if (this.spRestarting) return;
    this.spRestarting = true;
    log.error('the Game Mode window disappeared; restarting the Steam web helper');
    void this.backend.call('utilities/restart_webhelper').catch(() => {});
  }

  async reloadPlugin(name: string): Promise<void> {
    await this.backend.call('loader/reload_plugin', name);
  }

  dismountAll(): void {
    for (const name of new Set([...this.plugins.get().map((p) => p.name), ...this.requested.keys()])) this.bump(name);
    this.requested.clear();
    for (const p of this.plugins.get()) safeDismount(p.name, p);
    this.plugins.set([]);
  }

  deinit(): void {
    this.stopped = true;
    for (const off of this.offs) off();
    this.offs = [];
    this.installs.deinit();
    this.updates.stop();
    for (const h of [this.tabsHook, this.routerHook, this.toaster, this.errorBoundaryHook, this.errorReportFilter]) {
      try {
        h.deinit();
      } catch (e) {
        log.warn('hook deinit failed', e);
      }
    }
  }
}

function safeDismount(name: string, def: PluginDefinition): void {
  try {
    def.onDismount?.();
  } catch (e) {
    log.error(`${name}: onDismount failed`, e);
  }
}

function rethrowTimeout(e: unknown): never {
  if (e instanceof Error && e.message.startsWith('import timed out')) throw new TimeoutError(e.message);
  throw e;
}

