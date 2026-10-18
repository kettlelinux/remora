// Root approval per plugin (remora/permissions/*). Revoking is immediate; allowing makes the
// backend wait for the physical (or terminal) confirm, shown by the install flow's confirm UI
// (remora/confirm_waiting), and answers {approved, reason?} when it is done.
import { useEffect, useState } from 'react';
import { Focusable, ToggleField } from '../../hooks/components';
import { confirmHint } from '../../runtime/install-flow';
import type { PluginLoader } from '../../runtime/plugin-loader';
import { errorText } from '../../util/errors';
import { t } from '../i18n';

interface Permission {
  name: string;
  root: boolean;
  approved: boolean;
}

interface ConfirmMode {
  mode?: string;
  hint?: string;
}

export function PermissionsPage({ loader }: { loader: PluginLoader }) {
  const [list, setList] = useState<Permission[]>();
  const [error, setError] = useState<string>();
  const [mode, setMode] = useState<ConfirmMode>();
  const [waiting, setWaiting] = useState<string>();
  const load = () =>
    loader.backend.call<Permission[]>('remora/permissions/list').then(
      (l) => setList(l ?? []),
      (e) => setError(errorText(e)),
    );
  useEffect(() => {
    void load();
    loader.backend.call<ConfirmMode>('remora/permissions/confirm_mode').then(
      (m) => setMode(m ?? undefined),
      () => {}, // older backend: no hint
    );
  }, []);

  const set = async (name: string, approved: boolean) => {
    setError(undefined);
    if (!approved) setList((l) => l?.map((p) => (p.name === name ? { ...p, approved } : p)));
    else setWaiting(name);
    try {
      const r = await loader.backend.call<{ approved?: boolean; reason?: string } | null>('remora/permissions/set', name, approved);
      if (approved && r && r.approved === false) {
        setError(r.reason ? t('permissions.notApproved', { name, reason: r.reason }) : t('permissions.notApprovedNoReason', { name }));
      }
    } catch (e) {
      setError(errorText(e));
    }
    setWaiting((w) => (w === name ? undefined : w));
    void load();
    void loader.status.refresh();
  };

  return (
    <Focusable>
      <p style={{ opacity: 0.8 }}>{t('permissions.rootDesc')}</p>
      <p style={{ opacity: 0.8 }}>{t('permissions.confirmHint', { hint: confirmHint(mode?.mode, mode?.hint, '<name>') })}</p>
      {error && <p style={{ color: '#ff6b6b' }}>{error}</p>}
      {list?.length === 0 && <p>{t('permissions.none')}</p>}
      {list
        ?.filter((p) => p.root)
        .map((p) => (
          <ToggleField
            key={p.name}
            label={p.name}
            description={waiting === p.name ? t('permissions.waiting') : t('permissions.root')}
            checked={p.approved}
            disabled={waiting !== undefined}
            onChange={(v) => void set(p.name, v)}
          />
        ))}
    </Focusable>
  );
}
