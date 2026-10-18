// One installed plugin: state, actions and its log.
import { useEffect, useMemo, useState } from 'react';
import { ButtonItem, Field, Focusable, PanelSection, PanelSectionRow } from '../../hooks/components';
import { navigateBack } from '../../hooks/navigation';
import type { PluginLoader } from '../../runtime/plugin-loader';
import { errorText } from '../../util/errors';
import { ConfirmButton } from '../common/ConfirmButton';
import { t } from '../i18n';
import { useStore } from '../react-utils';
import { usePluginNameParam } from './use-route-param';

/** Lines per focusable log chunk: the gamepad scrolls the log by moving focus chunk to chunk. */
const LOG_CHUNK = 20;
/** Set once the backend answers remora/store/rollback with RouteNotFoundError (older backend). */
let rollbackUnsupported = false;
const isRouteNotFound = (e: unknown) => e instanceof Error && e.name.endsWith('RouteNotFoundError');

export function PluginDetailPage({ loader }: { loader: PluginLoader }) {
  const name = usePluginNameParam();
  const settings = useStore(loader.settings.values);
  const installed = useStore(loader.installed);
  const statuses = useStore(loader.status.plugins);
  const updates = useStore(loader.updates.available);
  const [log, setLog] = useState<string>();
  const [busy, setBusy] = useState<string>();
  const [error, setError] = useState<string>();
  const [canRollback, setCanRollback] = useState(!rollbackUnsupported);
  const logChunks = useMemo(() => {
    if (!log) return [];
    const lines = log.split('\n');
    const out: { from: number; text: string }[] = [];
    for (let i = 0; i < lines.length; i += LOG_CHUNK) out.push({ from: i + 1, text: lines.slice(i, i + LOG_CHUNK).join('\n') });
    return out;
  }, [log]);

  const p = installed.find((x) => x.name === name);
  const st = name ? statuses[name] : undefined;
  const update = name ? updates[name] : undefined;

  const loadLog = () => {
    if (!name) return;
    loader.backend.call<string>('remora/logs', name, 500).then(
      (l) => setLog(l ?? ''),
      (e) => setLog(errorText(e)),
    );
  };
  useEffect(loadLog, [name]);

  if (!name || !p) {
    return (
      <div style={{ padding: '60px 24px' }}>
        <p>{name ?? '?'}</p>
        <ButtonItem onClick={navigateBack}>{t('common.back')}</ButtonItem>
      </div>
    );
  }

  const run = (label: string, fn: () => Promise<unknown>) => async () => {
    setBusy(label);
    setError(undefined);
    try {
      await fn();
    } catch (e) {
      setError(errorText(e));
    }
    setBusy(undefined);
    void loader.refreshInstalled();
    void loader.status.refresh();
  };
  const call = (route: string, ...args: unknown[]) => () => loader.backend.call(route, ...args);
  const hidden = settings.hiddenPlugins.includes(name);
  const frozen = settings.frozenPlugins.includes(name);

  return (
    <Focusable style={{ padding: '60px 24px 24px', height: '100%', boxSizing: 'border-box', overflowY: 'auto' }}>
      <h2 style={{ marginTop: 0 }}>
        {name} <span style={{ opacity: 0.6, fontSize: 16 }}>{p.version}</span>
      </h2>
      {st && (
        <Field label={t('plugins.state', { state: p.disabled ? 'disabled' : st.state })} focusable description={st.error ?? undefined}>
          {t('plugins.restarts', { count: st.restarts })}
        </Field>
      )}
      {error && <p style={{ color: '#ff6b6b' }}>{error}</p>}
      <PanelSection title={t('plugins.actions')}>
        {update && (
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={!!busy} onClick={run('update', call('utilities/install_plugin', update.artifact, name, update.version, update.hash, 2))}>
              {t('plugins.update', { version: update.version })}
            </ButtonItem>
          </PanelSectionRow>
        )}
        {canRollback && (
          <PanelSectionRow>
            <ConfirmButton
              label={t('plugins.rollback')}
              description={t('plugins.rollbackDesc')}
              confirmText={t('plugins.rollbackDesc')}
              disabled={!!busy}
              onConfirm={run('rollback', async () => {
                try {
                  await loader.backend.call('remora/store/rollback', name);
                } catch (e) {
                  if (!isRouteNotFound(e)) throw e;
                  rollbackUnsupported = true;
                  setCanRollback(false);
                }
              })}
            />
          </PanelSectionRow>
        )}
        {!p.disabled && (
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={!!busy} onClick={run('reload', () => loader.reloadPlugin(name))}>
              {t('plugins.reload')}
            </ButtonItem>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => void loader.settings.toggleInList('hiddenPlugins', name, !hidden)}>
            {hidden ? t('plugins.show') : t('plugins.hide')}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => void loader.settings.toggleInList('frozenPlugins', name, !frozen).then(() => loader.updates.check())}
          >
            {frozen ? t('plugins.unfreeze') : t('plugins.freeze')}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          {p.disabled ? (
            <ButtonItem layout="below" disabled={!!busy} onClick={run('enable', call('utilities/enable_plugin', name))}>
              {t('plugins.enable')}
            </ButtonItem>
          ) : (
            <ConfirmButton label={t('plugins.disable')} confirmText={t('plugins.confirmDisableBody')} onConfirm={run('disable', call('utilities/disable_plugin', name))} />
          )}
        </PanelSectionRow>
        <PanelSectionRow>
          <ConfirmButton
            label={t('plugins.uninstall')}
            confirmText={t('plugins.confirmUninstallBody')}
            onConfirm={run('uninstall', async () => {
              await loader.backend.call('utilities/uninstall_plugin', name);
              navigateBack();
            })}
          />
        </PanelSectionRow>
      </PanelSection>
      <PanelSection title={t('logs.title', { name })}>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={loadLog}>
            {t('logs.refresh')}
          </ButtonItem>
        </PanelSectionRow>
        {logChunks.length === 0 ? (
          <Focusable onActivate={() => {}} style={logBox}>
            <pre style={logPre}>{log === undefined ? t('common.loading') : t('logs.empty')}</pre>
          </Focusable>
        ) : (
          // one focusable per chunk, so D-pad down walks (and scrolls) through a long log
          <Focusable flow-children="vertical" style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            {logChunks.map((c) => (
              <Focusable key={c.from} onActivate={() => {}} style={logBox} aria-label={t('logs.lines', { from: c.from, to: c.from + LOG_CHUNK - 1 })}>
                <pre style={logPre}>{c.text}</pre>
              </Focusable>
            ))}
          </Focusable>
        )}
      </PanelSection>
    </Focusable>
  );
}

const logBox = { background: 'rgba(0,0,0,0.35)', borderRadius: 4, padding: 8 } as const;
const logPre = { whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 11, margin: 0 } as const;
