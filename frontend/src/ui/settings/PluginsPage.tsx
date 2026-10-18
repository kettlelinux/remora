// Installed plugins: order (pluginOrder), sort by name, and a Manage page per plugin.
import * as DFL from '@decky/ui';
import { ReorderableList, type ReorderableEntry } from '@decky/ui';
import { DialogButton, Field, Focusable, ToggleField } from '../../hooks/components';
import { openPage } from '../../hooks/navigation';
import { orderPlugins } from '../../runtime/ordering';
import type { PluginLoader } from '../../runtime/plugin-loader';
import { statusBadges } from '../common/badges';
import type { ReactNode } from 'react';
import { t } from '../i18n';
import { useStore } from '../react-utils';

export const pluginPagePath = (name: string) => `/remora/plugin/${encodeURIComponent(name)}`;

export function PluginsPage({ loader }: { loader: PluginLoader }) {
  const settings = useStore(loader.settings.values);
  const installed = useStore(loader.installed);
  const statuses = useStore(loader.status.plugins);
  const updates = useStore(loader.updates.available);
  const loaded = useStore(loader.plugins);
  const ordered = orderPlugins(installed, { ...settings, sortPlugins: false });
  const hidden = new Set(settings.hiddenPlugins);
  const frozen = new Set(settings.frozenPlugins);

  const entries: ReorderableEntry<string>[] = ordered.map((p, i) => ({
    position: i,
    data: p.name,
    label: (
      <span style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap' }}>
        {p.name}
        <span style={{ opacity: 0.6, marginLeft: 8, fontSize: 12 }}>{p.version}</span>
        {statusBadges({
          status: p.disabled ? { ...(statuses[p.name] ?? ({} as never)), state: 'disabled' } : statuses[p.name],
          update: Boolean(updates[p.name]),
          loadError: Boolean(loaded.find((l) => l.name === p.name)?.loadError),
        })}
        {hidden.has(p.name) && <span style={{ opacity: 0.6, marginLeft: 8, fontSize: 12 }}>{t('plugins.hidden')}</span>}
        {frozen.has(p.name) && <span style={{ opacity: 0.6, marginLeft: 8, fontSize: 12 }}>{t('plugins.frozen')}</span>}
      </span>
    ),
  }));

  const Manage = ({ entry }: { entry: ReorderableEntry<string> }) => (
    <DialogButton style={{ minWidth: 0, width: 'auto', padding: '6px 12px' }} onClick={() => openPage(pluginPagePath(entry.data!))}>
      {t('plugins.actions')}
    </DialogButton>
  );

  return (
    <Focusable>
      <ToggleField label={t('plugins.sortByName')} description={t('plugins.sortByNameDesc')} checked={settings.sortPlugins} onChange={(v) => void loader.settings.set('sortPlugins', v)} />
      {entries.length === 0 ? (
        <p>{t('plugins.none')}</p>
      ) : !canUseReorderableList() ? (
        <SimpleOrderList entries={entries} Manage={Manage} onSave={(names) => void loader.settings.set('pluginOrder', names)} />
      ) : (
        <ReorderableList<string>
          entries={entries}
          interactables={Manage}
          animate
          onSave={(list) => void loader.settings.set('pluginOrder', [...list].sort((a, b) => a.position - b.position).map((e) => e.data!))}
        />
      )}
    </Focusable>
  );
}

// @decky/ui's ReorderableList is built from Steam's Field and Focusable; without them it cannot
// render, so the list falls back to up/down buttons.
function canUseReorderableList(): boolean {
  const d = DFL as unknown as Record<string, unknown>;
  return Boolean(d.Field && d.Focusable);
}

function SimpleOrderList({ entries, Manage, onSave }: { entries: ReorderableEntry<string>[]; Manage: (p: { entry: ReorderableEntry<string> }) => ReactNode; onSave: (names: string[]) => void }) {
  const names = entries.map((e) => e.data!);
  const move = (i: number, d: number) => {
    const j = i + d;
    if (j < 0 || j >= names.length) return;
    const next = names.slice();
    [next[i], next[j]] = [next[j]!, next[i]!];
    onSave(next);
  };
  return (
    <>
      {entries.map((e, i) => (
        <Field key={e.data} label={e.label}>
          <Focusable style={{ display: 'flex', gap: 6 }} flow-children="horizontal">
            <DialogButton style={{ minWidth: 0, width: 40 }} onClick={() => move(i, -1)}>
              ↑
            </DialogButton>
            <DialogButton style={{ minWidth: 0, width: 40 }} onClick={() => move(i, 1)}>
              ↓
            </DialogButton>
            <Manage entry={e} />
          </Focusable>
        </Field>
      ))}
    </>
  );
}
