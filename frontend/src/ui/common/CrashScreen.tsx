// Remora's crash screens: the full one Steam's patched ErrorBoundary shows, and the small card
// a plugin surface shows when it throws.
import { useState } from 'react';
import { DialogButton, Focusable } from '../../hooks/components';
import { callSteam } from '../../steam/globals';
import { attribute, errorStack, errorText } from '../../util/errors';
import { t } from '../i18n';
import { WarningIcon } from '../icons';

const backendCall = (route: string, ...args: unknown[]) => {
  void window.DeckyBackend?.call(route, ...args).catch((e) => console.error('[Remora]', route, e));
};

/** Steam's DialogButton takes autoFocus; @decky/ui's types only know preferredFocus. */
const focusFirst = { preferredFocus: true, ...({ autoFocus: true } as object) };

const pre = { whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 12, maxHeight: '40vh', overflowY: 'auto', background: 'rgba(0,0,0,0.3)', padding: 8, borderRadius: 4 } as const;

export function CrashScreen({ error, reset }: { error: unknown; reset: () => void }) {
  const [details, setDetails] = useState(false);
  const [busy, setBusy] = useState(false);
  // Reset only once the plugin is really gone: resetting first re-renders the crashing tree.
  const removeThenReset = (route: string, name: string) => async () => {
    if (busy) return;
    setBusy(true);
    try {
      await window.DeckyBackend?.call(route, name);
    } catch (e) {
      console.error('[Remora]', route, e);
    }
    try {
      window.DeckyPluginLoader?.unloadPlugin(name);
    } catch (e) {
      console.error('[Remora] unload failed', e);
    }
    setBusy(false);
    reset();
  };
  const stack = errorStack(error);
  const src = attribute(stack);
  const blame = src.kind === 'plugin' ? t('crash.likelyPlugin', { name: src.name }) : src.kind === 'loader' ? t('crash.likelyRemora') : t('crash.unknown');
  const btn = { marginTop: 8 };
  return (
    <div style={{ padding: '48px 32px', overflowY: 'auto', height: '100%', boxSizing: 'border-box' }}>
      <h1 style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
        <WarningIcon size={32} /> {t('crash.title')}
      </h1>
      <p>{t('crash.likely', { source: blame })}</p>
      <p style={{ fontFamily: 'monospace' }}>{errorText(error)}</p>
      <Focusable style={{ display: 'flex', flexDirection: 'column', maxWidth: 480 }} flow-children="vertical">
        <DialogButton style={btn} onClick={reset} {...focusFirst}>
          {t('common.retry')}
        </DialogButton>
        {src.kind === 'plugin' && (
          <>
            <DialogButton style={btn} disabled={busy} onClick={() => void removeThenReset('utilities/disable_plugin', src.name)()}>
              {t('crash.disablePlugin', { name: src.name })}
            </DialogButton>
            <DialogButton style={btn} disabled={busy} onClick={() => void removeThenReset('utilities/uninstall_plugin', src.name)()}>
              {t('crash.uninstallPlugin', { name: src.name })}
            </DialogButton>
          </>
        )}
        <DialogButton style={btn} onClick={() => backendCall('utilities/set_all_plugins_disabled')}>
          {t('crash.disableAll')}
        </DialogButton>
        <DialogButton style={btn} onClick={() => backendCall('utilities/restart_webhelper')}>
          {t('crash.reloadUI')}
        </DialogButton>
        <DialogButton style={btn} onClick={() => callSteam('User.StartRestart', false)}>
          {t('crash.restartSteam')}
        </DialogButton>
        <DialogButton style={btn} onClick={() => setDetails(!details)}>
          {t('crash.details')}
        </DialogButton>
      </Focusable>
      {details && <pre style={pre}>{stack}</pre>}
    </div>
  );
}

export function PluginCrashCard({ error, plugin, retry }: { error: unknown; plugin?: string; retry: () => void }) {
  const [details, setDetails] = useState(false);
  return (
    <div style={{ padding: 12 }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', fontWeight: 700 }}>
        <WarningIcon size={18} /> {plugin ? t('crash.pluginCard', { name: plugin }) : t('crash.remoraCard')}
      </div>
      <div style={{ fontSize: 12, margin: '6px 0', opacity: 0.8 }}>{errorText(error)}</div>
      <Focusable style={{ display: 'flex', gap: 8 }} flow-children="horizontal">
        <DialogButton style={{ minWidth: 0 }} onClick={retry} {...focusFirst}>
          {t('common.retry')}
        </DialogButton>
        <DialogButton style={{ minWidth: 0 }} onClick={() => setDetails(!details)}>
          {t('crash.details')}
        </DialogButton>
      </Focusable>
      {details && <pre style={pre}>{errorStack(error)}</pre>}
    </div>
  );
}
