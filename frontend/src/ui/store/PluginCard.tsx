// One store entry: image, details, version picker and the install/update button.
import { useState } from 'react';
import { DialogButton, Dropdown, Focusable } from '../../hooks/components';
import type { Backend } from '../../runtime/backend';
import type { InstalledPlugin } from '../../runtime/plugin';
import { errorText } from '../../util/errors';
import { t, type StringKey } from '../i18n';
import { WarningIcon } from '../icons';

export interface StoreVersion {
  name: string;
  hash: string;
  artifact?: string | null;
  created?: string;
  signed?: boolean | 'invalid';
  compatible?: boolean;
  incompatible_reason?: string;
}

export interface StorePlugin {
  id?: number;
  name: string;
  author?: string;
  description?: string;
  tags?: string[];
  image_url?: string;
  versions: StoreVersion[];
  source: string;
}

// Install types: 0 install, 1 reinstall, 2 update, 3 downgrade, 4 overwrite. `versions` is newest first.
export function installType(plugin: StorePlugin, versionIndex: number, installed?: InstalledPlugin): number {
  if (!installed) return 0;
  const current = plugin.versions.findIndex((v) => v.name === installed.version);
  if (current < 0) return 4;
  if (versionIndex < current) return 2;
  if (versionIndex === current) return 1;
  return 3;
}

const LABEL: Record<number, StringKey> = { 0: 'store.install', 1: 'store.reinstall', 2: 'store.update', 3: 'store.downgrade', 4: 'store.overwrite' };

export function PluginCard({ plugin, sourceName, installed, backend, developer }: { plugin: StorePlugin; sourceName: string; installed?: InstalledPlugin; backend: Backend; developer: boolean }) {
  const [vi, setVi] = useState(0);
  const [error, setError] = useState<string>();
  const version = plugin.versions[vi];
  const type = installType(plugin, vi, installed);
  const root = plugin.tags?.includes('root');
  // The backend refuses these too; the button says why instead of failing later.
  const blocked = !version
    ? t('store.empty')
    : version.artifact === null
      ? t('store.noArtifact')
      : version.signed === 'invalid'
        ? t('store.badSignature')
        : version.compatible === false && !developer
          ? t('store.incompatible', { reason: version.incompatible_reason ?? '?' })
          : undefined;

  const install = async () => {
    if (!version) return;
    setError(undefined);
    try {
      await backend.call('utilities/install_plugin', version.artifact ?? '', plugin.name, version.name, version.hash, type);
    } catch (e) {
      setError(errorText(e));
    }
  };

  return (
    <Focusable style={{ display: 'flex', gap: 16, padding: '12px 0', alignItems: 'flex-start' }}>
      {plugin.image_url && (
        <img src={plugin.image_url} alt="" loading="lazy" style={{ width: 200, height: 125, objectFit: 'cover', borderRadius: 4, flexShrink: 0, background: 'rgba(255,255,255,0.05)' }} />
      )}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 18, fontWeight: 700 }}>{plugin.name}</div>
        <div style={{ fontSize: 12, opacity: 0.7 }}>
          {plugin.author && t('store.by', { author: plugin.author })} · {sourceName}
          {installed && ` · ${t('store.installed', { version: installed.version ?? '?' })}`}
        </div>
        {plugin.description && <div style={{ fontSize: 13, margin: '6px 0' }}>{plugin.description}</div>}
        {plugin.tags?.length ? <div style={{ fontSize: 11, opacity: 0.6 }}>{plugin.tags.join(' · ')}</div> : null}
        {root && (
          <div style={{ display: 'flex', gap: 6, alignItems: 'center', fontSize: 12, color: '#e5a50a', margin: '6px 0' }}>
            <WarningIcon size={14} /> {t('store.rootWarning')}
          </div>
        )}
        {version?.signed === true && <div style={{ fontSize: 12, color: '#59bf40' }}>{t('store.signed')}</div>}
        {blocked && <div style={{ fontSize: 12, color: '#e5a50a' }}>{blocked}</div>}
        <Focusable style={{ display: 'flex', gap: 8, marginTop: 8 }} flow-children="horizontal">
          <DialogButton style={{ flex: 1, minWidth: 0 }} disabled={Boolean(blocked)} onClick={() => void install()}>
            {t(LABEL[type] ?? 'store.install')}
          </DialogButton>
          {plugin.versions.length > 1 && (
            <div style={{ flex: '0 0 160px' }}>
              <Dropdown rgOptions={plugin.versions.map((v, i) => ({ data: i, label: v.name }))} selectedOption={vi} onChange={(o) => setVi(o.data)} menuLabel={t('store.version')} />
            </div>
          )}
        </Focusable>
        {error && <div style={{ color: '#ff6b6b', fontSize: 12, marginTop: 4 }}>{error}</div>}
      </div>
    </Focusable>
  );
}
