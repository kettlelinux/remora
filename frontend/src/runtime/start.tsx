// Second boot stage: runs once SP_REACT and DFL exist. Sets up the backend connection, the
// plugin loader, Remora's pages and the plugin API, replacing a previous instance if there is one.
import { checkClassMaps } from '../hooks/classes';
import { checkComponents } from '../hooks/components';
import { checkModals } from '../hooks/modal';
import { checkNavigation } from '../hooks/navigation';
import { hookStatuses } from '../hooks/status';
import { watchUIMode } from '../hooks/ui-mode';
import { setLanguage } from '../ui/i18n';
import { settingsPage } from '../ui/settings/SettingsPage';
import { PluginDetailPage } from '../ui/settings/PluginDetailPage';
import { storePage } from '../ui/store/StorePage';
import { logger, setVerbose } from '../util/logger';
import { Backend } from './backend';
import { PluginLoader } from './plugin-loader';

declare const __REMORA_VERSION__: string;
const log = logger('start');

function replacePrevious(): void {
  const prev = window.DeckyPluginLoader as Partial<PluginLoader> | undefined;
  try {
    prev?.dismountAll?.();
  } catch (e) {
    log.warn('previous dismountAll failed', e);
  }
  try {
    prev?.deinit?.();
  } catch (e) {
    log.warn('previous deinit failed', e);
  }
  try {
    (window.DeckyBackend as Partial<Backend> | undefined)?.deinit?.();
  } catch (e) {
    log.warn('previous backend deinit failed', e);
  }
}

function registerPages(loader: PluginLoader): void {
  const r = loader.routerHook;
  for (const base of ['/decky/settings', '/remora/settings']) r.addRoute(base, settingsPage(loader, base));
  const store = storePage(loader);
  for (const base of ['/decky/store', '/remora/store']) r.addRoute(base, store);
  r.addRoute('/remora/plugin/:name', () => <PluginDetailPage loader={loader} />, { exact: true });
}

export async function start(): Promise<void> {
  replacePrevious();
  try {
    setLanguage(navigator.language);
  } catch {
    // English
  }
  for (const check of [checkNavigation, checkModals, checkClassMaps, checkComponents]) {
    try {
      check();
    } catch (e) {
      log.error('hook check failed', e);
    }
  }
  watchUIMode();

  const backend = new Backend();
  window.DeckyBackend = backend;
  await backend.connect();

  const loader = new PluginLoader(backend);
  window.DeckyPluginLoader = loader;
  window.remora = { version: __REMORA_VERSION__, hooks: () => hookStatuses.get() };
  loader.settings.values.subscribe((v) => setVerbose(v['remora.verbose']));
  await loader.init(registerPages);
  log.info(`Remora ${__REMORA_VERSION__} ready`);
}
