// Installed plugins: order (pluginOrder), sort by name, and a Manage page per plugin.
// ReorderableList resets its local order (and remounts the Manage buttons) whenever `entries`
// or `interactables` change identity, so entries are memoized on what changes the list itself
// (names, versions, disabled, order) and the badges read the stores through context instead.
import * as DFL from '@decky/ui';
import { ReorderableList, type ReorderableEntry } from '@decky/ui';
import { createContext, useContext, useMemo } from 'react';
import { DialogButton, Field, Focusable, ToggleField } from '../../hooks/components';
import { openPage } from '../../hooks/navigation';
import { orderPlugins } from '../../runtime/ordering';
import type { PluginLoader } from '../../runtime/plugin-loader';
import { statusBadges } from '../common/badges';
import { t } from '../i18n';
import { useStore } from '../react-utils';

export const pluginPagePath = (name: string) => `/remora/plugin/${encodeURIComponent(name)}`;

const LoaderContext = createContext<PluginLoader | null>(null);

function Manage({ entry }: { entry: ReorderableEntry<string> }) {
  return (
    <DialogButton style={{ minWidth: 0, width: 'auto', padding: '6px 12px' }} onClick={() => openPage(pluginPagePath(entry.data!))}>
      {t('plugins.actions')}
    </DialogButton>
  );
}

function EntryLabel({ name, version, disabled }: { name: string; version?: string; disabled: boolean }) {
  const loader = useContext(LoaderContext)!;
  const settings = useStore(loader.settings.values);
  const statuses = useStore(loader.status.plugins);
  const updates = useStore(loader.updates.available);
  const loaded = useStore(loader.plugins);
  return (
    <span style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap' }}>
      {name}
      <span style={{ opacity: 0.6, marginLeft: 8, fontSize: 12 }}>{version}</span>
      {statusBadges({
        status: disabled ? { ...(statuses[name] ?? ({} as never)), state: 'disabled' } : statuses[name],
        update: Boolean(updates[name]),
        loadError: Boolean(loaded.find((l) => l.name === name)?.loadError),
      })}
      {settings.hiddenPlugins.includes(name) && <span style={{ opacity: 0.6, marginLeft: 8, fontSize: 12 }}>{t('plugins.hidden')}</span>}
      {settings.frozenPlugins.includes(name) && <span style={{ opacity: 0.6, marginLeft: 8, fontSize: 12 }}>{t('plugins.frozen')}</span>}
    </span>
  );
}

export function PluginsPage({ loader }: { loader: PluginLoader }) {
  const settings = useStore(loader.settings.values);
  const installed = useStore(loader.installed);
  const ordered = orderPlugins(installed, { ...settings, sortPlugins: false });
  const listKey = ordered.map((p) => `${p.name}\u0000${p.version ?? ''}\u0000${p.disabled ? 1 : 0}`).join('\u0001');

  const entries: ReorderableEntry<string>[] = useMemo(
    () =>
      ordered.map((p, i) => ({
        position: i,
        data: p.name,
        label: <EntryLabel name={p.name} version={p.version} disabled={p.disabled} />,
      })),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [listKey],
  );

  return (
    <LoaderContext.Provider value={loader}>
      <Focusable>
        <ToggleField label={t('plugins.sortByName')} description={t('plugins.sortByNameDesc')} checked={settings.sortPlugins} onChange={(v) => void loader.settings.set('sortPlugins', v)} />
        {entries.length === 0 ? (
          <p>{t('plugins.none')}</p>
        ) : !canUseReorderableList() ? (
          <SimpleOrderList entries={entries} onSave={(names) => void loader.settings.set('pluginOrder', names)} />
        ) : (
          <ReorderableList<string>
            entries={entries}
            interactables={Manage}
            animate
            onSave={(list) => void loader.settings.set('pluginOrder', [...list].sort((a, b) => a.position - b.position).map((e) => e.data!))}
          />
        )}
      </Focusable>
    </LoaderContext.Provider>
  );
}

// @decky/ui's ReorderableList is built from Steam's Field and Focusable; without them it cannot
// render, so the list falls back to up/down buttons.
function canUseReorderableList(): boolean {
  const d = DFL as unknown as Record<string, unknown>;
  return Boolean(d.Field && d.Focusable);
}

function SimpleOrderList({ entries, onSave }: { entries: ReorderableEntry<string>[]; onSave: (names: string[]) => void }) {
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
