// Root approval per plugin (remora/permissions/*).
import { useEffect, useState } from 'react';
import { Focusable, ToggleField } from '../../hooks/components';
import type { PluginLoader } from '../../runtime/plugin-loader';
import { errorText } from '../../util/errors';
import { t } from '../i18n';

interface Permission {
  name: string;
  root: boolean;
  approved: boolean;
}

export function PermissionsPage({ loader }: { loader: PluginLoader }) {
  const [list, setList] = useState<Permission[]>();
  const [error, setError] = useState<string>();
  const load = () =>
    loader.backend.call<Permission[]>('remora/permissions/list').then(
      (l) => setList(l ?? []),
      (e) => setError(errorText(e)),
    );
  useEffect(() => void load(), []);

  const set = async (name: string, approved: boolean) => {
    setList((l) => l?.map((p) => (p.name === name ? { ...p, approved } : p)));
    try {
      await loader.backend.call('remora/permissions/set', name, approved);
    } catch (e) {
      setError(errorText(e));
    }
    void load();
    void loader.status.refresh();
  };

  return (
    <Focusable>
      <p style={{ opacity: 0.8 }}>{t('permissions.rootDesc')}</p>
      {error && <p style={{ color: '#ff6b6b' }}>{error}</p>}
      {list?.length === 0 && <p>{t('permissions.none')}</p>}
      {list
        ?.filter((p) => p.root)
        .map((p) => (
          <ToggleField key={p.name} label={p.name} description={t('permissions.root')} checked={p.approved} onChange={(v) => void set(p.name, v)} />
        ))}
    </Focusable>
  );
}
