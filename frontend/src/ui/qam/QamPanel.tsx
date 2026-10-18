// Remora's Quick Access tab: the plugin list, or the open plugin's panel.
import { PluginBoundary } from '../../hooks/error-boundary';
import { classes } from '../../hooks/classes';
import { ButtonItem, Focusable, PanelSection, PanelSectionRow } from '../../hooks/components';
import { openPage } from '../../hooks/navigation';
import { hookStatuses } from '../../hooks/status';
import { qamPlugins } from '../../runtime/ordering';
import type { LoadedPlugin } from '../../runtime/plugin';
import type { PluginLoader } from '../../runtime/plugin-loader';
import { useEffect, useRef } from 'react';
import { activePlugin, qamVisible, reportQamDocumentVisible } from '../../runtime/qam-state';
import { statusBadges } from '../common/badges';
import { PendingPrompts } from '../common/PendingPrompts';
import { TitleBar } from '../common/TitleBar';
import { t } from '../i18n';
import { BackIcon, GearIcon, StoreIcon, WarningIcon } from '../icons';
import { useStore } from '../react-utils';
import { QuickAccessVisibleContext } from './visibility';

export function QamPanel({ loader }: { loader: PluginLoader }) {
  const visible = useStore(qamVisible);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // the panel is rendered into the QAM window, whose document is hidden while the QAM is closed
    const doc = ref.current?.ownerDocument;
    if (!doc) return;
    const update = () => reportQamDocumentVisible(doc.visibilityState === 'visible');
    update();
    doc.addEventListener('visibilitychange', update);
    return () => doc.removeEventListener('visibilitychange', update);
  }, []);
  return (
    <QuickAccessVisibleContext.Provider value={visible}>
      <div ref={ref} style={{ height: '100%' }}>
        <PluginBoundary>
          <QamContent loader={loader} visible={visible} />
        </PluginBoundary>
      </div>
    </QuickAccessVisibleContext.Provider>
  );
}

function QamContent({ loader, visible }: { loader: PluginLoader; visible: boolean }) {
  const active = useStore(activePlugin);
  const plugins = useStore(loader.plugins);
  const plugin = active ? plugins.find((p) => p.name === active) : undefined;
  if (plugin) return <PluginView plugin={plugin} visible={visible} />;
  return <PluginList loader={loader} />;
}

function PluginView({ plugin, visible }: { plugin: LoadedPlugin; visible: boolean }) {
  const back = () => activePlugin.set(null);
  const title = plugin.titleView ?? plugin.title ?? <div className={classes.static.Title}>{plugin.name}</div>;
  return (
    <Focusable onCancelButton={back} style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <TitleBar
        left={{ key: 'back', label: t('common.back'), icon: <BackIcon size={18} />, onClick: back }}
        title={
          <PluginBoundary plugin={plugin.name} fallback={plugin.name}>
            {title}
          </PluginBoundary>
        }
      />
      <div style={{ flex: 1, minHeight: 0 }}>
        <PluginBoundary plugin={plugin.name}>{visible || plugin.alwaysRender ? plugin.content : null}</PluginBoundary>
      </div>
    </Focusable>
  );
}

function PluginList({ loader }: { loader: PluginLoader }) {
  const plugins = useStore(loader.plugins);
  const settings = useStore(loader.settings.values);
  const statuses = useStore(loader.status.plugins);
  const updates = useStore(loader.updates.available);
  const installed = useStore(loader.installed);
  const hooks = useStore(hookStatuses);
  const connected = useStore(loader.backend.connected);
  const { shown, hidden } = qamPlugins(plugins, settings);
  const disabled = installed.filter((p) => p.disabled).length;
  // error-reporting only keeps plugin errors out of Valve's reports; nothing visible breaks
  const broken = hooks.some((h) => h.state === 'failed' && h.id !== 'error-reporting');

  return (
    <div>
      <TitleBar
        title={t('remora')}
        right={[
          { key: 'store', label: t('qam.store'), icon: <StoreIcon size={18} />, onClick: () => openPage('/decky/store') },
          { key: 'settings', label: t('qam.settings'), icon: <GearIcon size={18} />, onClick: () => openPage('/decky/settings') },
        ]}
      />
      {!connected && <Notice text={t('qam.backendDown')} />}
      {broken && <Notice text={t('qam.hooksWarning')} />}
      <PendingPrompts flow={loader.installs} />
      <PanelSection>
        {shown.length === 0 && (
          <PanelSectionRow>
            <div style={{ opacity: 0.7, padding: '8px 0' }}>{t('qam.empty')}</div>
          </PanelSectionRow>
        )}
        {shown.map((p) => (
          <PanelSectionRow key={p.name}>
            <ButtonItem layout="below" onClick={() => activePlugin.set(p.name)}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
                <PluginBoundary plugin={p.name} fallback={null}>
                  <div style={{ display: 'flex', flexShrink: 0 }}>{p.icon}</div>
                </PluginBoundary>
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{p.name}</span>
                {statusBadges({ status: statuses[p.name], update: Boolean(updates[p.name]), loadError: Boolean(p.loadError) })}
              </div>
            </ButtonItem>
          </PanelSectionRow>
        ))}
        {(hidden > 0 || disabled > 0) && (
          <PanelSectionRow>
            <div style={{ opacity: 0.6, fontSize: 12, padding: '4px 0' }}>
              {[hidden > 0 && t('qam.hidden', { count: hidden }), disabled > 0 && t('qam.disabled', { count: disabled })].filter(Boolean).join(' · ')}
            </div>
          </PanelSectionRow>
        )}
      </PanelSection>
    </div>
  );
}

function Notice({ text }: { text: string }) {
  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'center', margin: '4px 16px', padding: 8, borderRadius: 4, background: 'rgba(229,165,10,0.15)', fontSize: 12 }}>
      <WarningIcon size={16} style={{ flexShrink: 0 }} />
      <span>{text}</span>
    </div>
  );
}
