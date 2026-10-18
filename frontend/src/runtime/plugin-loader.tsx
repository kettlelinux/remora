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

class TimeoutError extends Error {}

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

  constructor(readonly backend: Backend) {
    this.settings = new Settings(backend);
    this.status = new StatusStore(backend);
    this.installs = new InstallFlow(backend, this.toaster, () => void this.refreshInstalled().then(() => this.updates.check()));
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
    await this.loadPlugins();
    const spAfterLoad = currentSP();
    setTimeout(() => this.checkSP(spAfterLoad), 5000);
    this.updates.start();
  }

  private listen(): void {
    const on = (event: string, fn: (...a: unknown[]) => unknown) => {
      this.backend.addEventListener(event, fn);
      this.offs.push(() => this.backend.removeEventListener(event, fn));
    };
    on('loader/import_plugin', (name, version, loadType, useQueue = true, timeoutMs = 15000) =>
      this.importPlugin(String(name), version as string | undefined, Number(loadType), useQueue !== false, Number(timeoutMs) || 15000)
        .catch((e) => log.error(`import of ${String(name)} failed`, e))
        .finally(() => void this.refreshInstalled()),
    );
    on('loader/unload_plugin', (name) => {
      this.unloadPlugin(String(name));
      void this.refreshInstalled();
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
    this.installs.listen();
  }

  async loadPlugins(): Promise<void> {
    const mode = await firstUIMode();
    if (mode === EUIMode.GamePad) await waitForSP();
    let list: InstalledPlugin[] | undefined;
    for (let attempt = 0; attempt < 5 && !list; attempt++) {
      try {
        list = await this.backend.call<InstalledPlugin[]>('loader/get_plugins');
      } catch (e) {
        log.warn('loader/get_plugins failed; retrying', e);
        await sleep(2000);
      }
    }
    if (!list) return;
    this.installed.set(list);
    await Promise.allSettled(list.filter((p) => !p.disabled).map((p) => this.importPlugin(p.name, p.version, p.load_type, false)));
    log.info(`${this.plugins.get().length} plugin(s) loaded`);
  }

  /** Re-reads loader/get_plugins (after install, uninstall, enable). */
  async refreshInstalled(): Promise<void> {
    try {
      this.installed.set(await this.backend.call<InstalledPlugin[]>('loader/get_plugins'));
    } catch (e) {
      log.warn('loader/get_plugins failed', e);
    }
  }

  async importPlugin(name: string, version: string | undefined, loadType: number, useQueue = true, timeoutMs?: number): Promise<void> {
    if (this.stopped) return;
    if (useQueue && this.reloadLock) {
      this.pluginReloadQueue.push({ name, version, loadType, timeoutMs });
      return;
    }
    if (useQueue) this.reloadLock = true;
    try {
      await this.doImport(name, version, loadType, timeoutMs);
    } finally {
      if (useQueue) {
        this.reloadLock = false;
        const next = this.pluginReloadQueue.shift();
        if (next) void this.importPlugin(next.name, next.version, next.loadType, true, next.timeoutMs).catch((e) => log.error(`queued import of ${next.name} failed`, e));
      }
    }
  }

  private async doImport(name: string, version: string | undefined, loadType: number, timeoutMs?: number): Promise<void> {
    this.unloadPlugin(name);
    const spBefore = currentSP();
    const started = performance.now();
    try {
      const def = loadType === LOAD_ESM ? await this.importESM(name, timeoutMs) : await this.importLegacy(name, timeoutMs);
      if (!def || typeof def !== 'object') throw new Error('the plugin factory returned no plugin object');
      this.add({ ...def, name, version, loadType });
      log.info(`loaded ${name} ${version ?? ''} in ${Math.round(performance.now() - started)} ms`);
    } catch (e) {
      if (e instanceof TimeoutError) {
        log.error(`${name}: ${e.message}`);
        throw e;
      }
      log.error(`failed to load ${name}`, e);
      this.add({
        name,
        version,
        loadType,
        icon: <WarningIcon />,
        content: <PluginLoadError name={name} error={e} />,
        loadError: { message: errorText(e), stack: errorStack(e) },
      });
      this.toaster.toast({ title: t('plugin.loadFailed', { name }), body: errorText(e) || t('plugin.loadFailedBody'), logo: <WarningIcon /> });
    } finally {
      this.checkSP(spBefore);
    }
  }

  private async importESM(name: string, timeoutMs?: number): Promise<PluginDefinition> {
    const url = `${PLUGIN_BASE}${name}/dist/index.js?t=${Date.now()}`;
    const p = import(/* webpackIgnore: true */ url) as Promise<{ default?: unknown }>;
    const mod = timeoutMs ? await withTimeout(p, timeoutMs, `import timed out after ${timeoutMs} ms`).catch(rethrowTimeout) : await p;
    if (typeof mod.default !== 'function') throw new Error('dist/index.js has no default export function');
    return (await (mod.default as () => unknown)()) as PluginDefinition;
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

  unloadPlugin(name: string): void {
    const p = this.plugins.get().find((x) => x.name === name);
    if (!p) return;
    try {
      p.onDismount?.();
    } catch (e) {
      log.error(`${name}: onDismount failed`, e);
    }
    this.api.forget(name);
    this.plugins.update((all) => all.filter((x) => x !== p));
  }

  /** Steam's SP window vanishing right after a plugin loaded means the plugin crashed it. */
  private checkSP(before: Window | undefined): void {
    if (before && !currentSP()) {
      log.error('the Game Mode window disappeared; restarting the Steam web helper');
      void this.backend.call('utilities/restart_webhelper').catch(() => {});
    }
  }

  async reloadPlugin(name: string): Promise<void> {
    await this.backend.call('loader/reload_plugin', name);
  }

  dismountAll(): void {
    for (const p of this.plugins.get()) {
      try {
        p.onDismount?.();
      } catch (e) {
        log.error(`${p.name}: onDismount failed`, e);
      }
    }
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

function rethrowTimeout(e: unknown): never {
  if (e instanceof Error && e.message.startsWith('import timed out')) throw new TimeoutError(e.message);
  throw e;
}

