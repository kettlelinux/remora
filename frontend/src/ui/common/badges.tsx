// Small status pills next to plugin names.
import type { PluginStatus } from '../../runtime/status-store';
import { t, type StringKey } from '../i18n';

const COLORS: Record<string, string> = {
  update: '#1a9fff',
  crashed: '#d94126',
  blocked: '#e5a50a',
  disabled: '#5c6370',
  starting: '#5c6370',
  error: '#d94126',
};

export function Badge({ kind }: { kind: keyof typeof COLORS }) {
  return (
    <span
      style={{
        background: COLORS[kind],
        color: '#fff',
        borderRadius: 4,
        padding: '1px 6px',
        fontSize: 11,
        fontWeight: 700,
        marginLeft: 6,
        whiteSpace: 'nowrap',
      }}
    >
      {t(`badge.${kind}` as StringKey)}
    </span>
  );
}

export function statusBadges(opts: { status?: PluginStatus; update?: boolean; loadError?: boolean }) {
  const out: (keyof typeof COLORS)[] = [];
  if (opts.loadError) out.push('error');
  const s = opts.status?.state;
  if (s === 'crashed' || s === 'blocked' || s === 'disabled' || s === 'starting') out.push(s);
  if (opts.update) out.push('update');
  return out.map((k) => <Badge key={k} kind={k} />);
}
