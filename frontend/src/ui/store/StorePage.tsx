// /decky/store (and /remora/store): plugins from every enabled source.
import { useEffect, useMemo, useState } from 'react';
import { Dropdown, Field, Focusable, PanelSection, PanelSectionRow, SteamSpinner, TextField } from '../../hooks/components';
import { PluginBoundary } from '../../hooks/error-boundary';
import type { PluginLoader } from '../../runtime/plugin-loader';
import { errorText } from '../../util/errors';
import { PendingPrompts } from '../common/PendingPrompts';
import { t, type StringKey } from '../i18n';
import { useStore } from '../react-utils';
import type { StoreSource } from '../settings/SourcesPage';
import { PluginCard, type StorePlugin } from './PluginCard';

interface StoreList {
  sources: { id: string; ok: boolean; error?: string }[];
  plugins: StorePlugin[];
}

const SORTS = ['name_asc', 'name_desc', 'date_desc', 'date_asc', 'downloads_desc', 'downloads_asc'] as const;
type Sort = (typeof SORTS)[number];

export function storePage(loader: PluginLoader) {
  return function RemoraStore() {
    return (
      <PluginBoundary>
        <Store loader={loader} />
      </PluginBoundary>
    );
  };
}

function matches(p: StorePlugin, q: string): boolean {
  if (!q) return true;
  const needle = q.toLowerCase();
  return [p.name, p.description, p.author, ...(p.tags ?? [])].some((s) => (s ?? '').toLowerCase().includes(needle));
}

function Store({ loader }: { loader: PluginLoader }) {
  const [sort, setSort] = useState<Sort>('name_asc');
  const [query, setQuery] = useState('');
  const [source, setSource] = useState<string>('');
  const [list, setList] = useState<StoreList>();
  const [sources, setSources] = useState<StoreSource[]>([]);
  const [error, setError] = useState<string>();
  const installed = useStore(loader.installed);
  const developer = useStore(loader.settings.values)['developer.enabled'];

  useEffect(() => {
    loader.backend.call<StoreSource[]>('remora/store/sources').then(setSources, () => {});
  }, []);

  useEffect(() => {
    setList(undefined);
    setError(undefined);
    const [by, dir] = sort.split('_') as [string, string];
    loader.backend.call<StoreList>('remora/store/list', by, dir).then(setList, (e) => setError(errorText(e)));
  }, [sort]);

  const shown = useMemo(
    () => (list?.plugins ?? []).filter((p) => (!source || p.source === source) && matches(p, query)),
    [list, source, query],
  );
  const sourceName = (id: string) => sources.find((s) => s.id === id)?.name ?? id;

  return (
    <Focusable style={{ padding: '60px 24px 24px', height: '100%', boxSizing: 'border-box', overflowY: 'auto' }}>
      <h2 style={{ marginTop: 0 }}>{t('store.title')}</h2>
      <PendingPrompts flow={loader.installs} />
      <Focusable style={{ display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'flex-end' }} flow-children="horizontal">
        <div style={{ flex: '2 1 240px' }}>
          <TextField label={t('store.search')} value={query} onChange={(e) => setQuery(e.currentTarget.value)} />
        </div>
        <div style={{ flex: '1 1 160px' }}>
          <Field label={t('store.source')} childrenLayout="below" bottomSeparator="none">
            <Dropdown
              rgOptions={[{ data: '', label: t('store.allSources') }, ...sources.map((s) => ({ data: s.id, label: s.name }))]}
              selectedOption={source}
              onChange={(o) => setSource(o.data)}
            />
          </Field>
        </div>
        <div style={{ flex: '1 1 160px' }}>
          <Field label={t('store.sort')} childrenLayout="below" bottomSeparator="none">
            <Dropdown rgOptions={SORTS.map((s) => ({ data: s, label: t(`store.sort.${s}` as StringKey) }))} selectedOption={sort} onChange={(o) => setSort(o.data)} />
          </Field>
        </div>
      </Focusable>
      {error && <p style={{ color: '#ff6b6b' }}>{error}</p>}
      {list?.sources
        .filter((s) => !s.ok)
        .map((s) => (
          <p key={s.id} style={{ color: '#e5a50a' }}>
            {t('store.sourceError', { source: sourceName(s.id), error: s.error ?? '?' })}
          </p>
        ))}
      {!list && !error && <SteamSpinner style={{ height: 64 }} />}
      {list && shown.length === 0 && <p>{t('store.empty')}</p>}
      <PanelSection>
        {shown.map((p) => (
          <PanelSectionRow key={`${p.source}:${p.name}`}>
            <PluginBoundary fallback={null}>
              <PluginCard plugin={p} sourceName={sourceName(p.source)} installed={installed.find((i) => i.name === p.name)} backend={loader.backend} developer={developer} />
            </PluginBoundary>
          </PanelSectionRow>
        ))}
      </PanelSection>
    </Focusable>
  );
}
