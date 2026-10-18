// Health of every Steam lookup, so a broken feature after a Steam update explains itself.
import { Field, Focusable } from '../../hooks/components';
import { hookStatuses, type HookState } from '../../hooks/status';
import { t } from '../i18n';
import { useStore } from '../react-utils';

const COLOR: Record<HookState, string> = { ok: '#59bf40', degraded: '#e5a50a', failed: '#d94126', pending: '#8b929a' };

export function HooksPage() {
  const all = useStore(hookStatuses);
  const order: HookState[] = ['failed', 'degraded', 'pending', 'ok'];
  const sorted = [...all].sort((a, b) => order.indexOf(a.state) - order.indexOf(b.state) || a.id.localeCompare(b.id));
  return (
    <Focusable>
      <p style={{ opacity: 0.8 }}>{t('hooks.intro')}</p>
      {sorted.map((h) => (
        <Field
          key={h.id}
          focusable
          label={
            <span>
              <span style={{ color: COLOR[h.state], marginRight: 8 }}>●</span>
              {h.id}
            </span>
          }
          description={
            <div style={{ fontSize: 12 }}>
              {h.strategy && <div>{t('hooks.via', { strategy: h.strategy })}</div>}
              {h.detail && <div style={{ opacity: 0.8 }}>{h.detail}</div>}
              {h.state !== 'ok' && <div style={{ opacity: 0.8 }}>{t('hooks.affects', { feature: h.feature })}</div>}
            </div>
          }
        >
          {t(`hooks.state.${h.state}`)}
        </Field>
      ))}
    </Focusable>
  );
}
