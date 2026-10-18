// Store sources (remora/store/sources, remora/store/set_sources).
import { useEffect, useState } from 'react';
import { ButtonItem, Dropdown, Field, Focusable, PanelSection, PanelSectionRow, TextField, ToggleField } from '../../hooks/components';
import type { PluginLoader } from '../../runtime/plugin-loader';
import { errorText } from '../../util/errors';
import { ConfirmButton } from '../common/ConfirmButton';
import { t } from '../i18n';
import { useStore } from '../react-utils';

export interface StoreSource {
  id: string;
  name: string;
  kind: 'decky' | 'index';
  url: string;
  enabled: boolean;
  builtin: boolean;
  keys?: { id: string; alg: string; pub: string }[];
}

export function SourcesPage({ loader }: { loader: PluginLoader }) {
  const [sources, setSources] = useState<StoreSource[]>();
  const [error, setError] = useState<string>();
  const [name, setName] = useState('');
  const [url, setUrl] = useState('');
  const [kind, setKind] = useState<StoreSource['kind']>('index');
  const telemetry = useStore(loader.settings.values)['remora.storeTelemetry'];

  useEffect(() => {
    loader.backend.call<StoreSource[]>('remora/store/sources').then(setSources, (e) => setError(errorText(e)));
  }, []);

  const save = async (next: StoreSource[]) => {
    setSources(next);
    setError(undefined);
    try {
      setSources(await loader.backend.call<StoreSource[]>('remora/store/set_sources', next));
    } catch (e) {
      setError(errorText(e));
    }
  };

  const add = () => {
    const u = url.trim();
    if (!u || !sources) return;
    const id = `${(name.trim() || new URL(u, 'https://x').hostname).toLowerCase().replace(/[^a-z0-9]+/g, '-')}-${Date.now().toString(36)}`;
    void save([...sources, { id, name: name.trim() || u, kind, url: /^[a-z]+:\/\//i.test(u) ? u : `https://${u}`, enabled: true, builtin: false }]);
    setName('');
    setUrl('');
  };

  return (
    <Focusable>
      <p style={{ opacity: 0.8 }}>{t('sources.intro')}</p>
      <ToggleField
        label={t('sources.telemetry')}
        description={t('sources.telemetryDesc')}
        checked={telemetry}
        onChange={(v) => void loader.settings.set('remora.storeTelemetry', v)}
      />
      {error && <p style={{ color: '#ff6b6b' }}>{error}</p>}
      {sources?.map((s) => (
        <PanelSection key={s.id} title={s.name}>
          <PanelSectionRow>
            <Field label={t('sources.url')} focusable description={s.builtin ? t('sources.builtin') : undefined}>
              <span style={{ fontSize: 12, wordBreak: 'break-all' }}>{s.url}</span>
            </Field>
          </PanelSectionRow>
          <PanelSectionRow>
            <ToggleField label={t('sources.enabled')} checked={s.enabled} onChange={(v) => void save(sources.map((x) => (x.id === s.id ? { ...x, enabled: v } : x)))} />
          </PanelSectionRow>
          {!s.builtin && (
            <PanelSectionRow>
              <ConfirmButton label={t('common.remove')} confirmText={s.url} onConfirm={() => void save(sources.filter((x) => x.id !== s.id))} />
            </PanelSectionRow>
          )}
        </PanelSection>
      ))}
      <PanelSection title={t('sources.add')}>
        <PanelSectionRow>
          <TextField label={t('sources.name')} value={name} onChange={(e) => setName(e.currentTarget.value)} />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField label={t('sources.url')} value={url} onChange={(e) => setUrl(e.currentTarget.value)} />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label={t('sources.kind')}>
            <Dropdown
              rgOptions={[
                { data: 'index', label: t('sources.kindIndex') },
                { data: 'decky', label: t('sources.kindDecky') },
              ]}
              selectedOption={kind}
              onChange={(o) => setKind(o.data)}
            />
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={!url.trim() || !sources} onClick={add}>
            {t('common.add')}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </Focusable>
  );
}
