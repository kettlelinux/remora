// Remora's rendering of a plugin toast inside Steam's notification popup and tray.
import type { ToastData } from '../../hooks/toaster';
import { classes } from '../../hooks/classes';

export function RemoraToast({ data, location }: { data: ToastData; location?: unknown }) {
  const c = classes.toast;
  const tray = location !== undefined && location !== 1 && location !== 'popup';
  return (
    <div
      className={[c.ShortTemplate, data.className].filter(Boolean).join(' ')}
      style={{ display: 'flex', gap: 12, alignItems: 'center', padding: tray ? '8px 12px' : '12px 16px', cursor: data.onClick ? 'pointer' : undefined }}
      onClick={() => data.onClick?.()}
    >
      {data.logo && <div style={{ flexShrink: 0, width: 40, height: 40, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>{data.logo}</div>}
      <div className={data.contentClassName} style={{ minWidth: 0, flex: 1 }}>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center', fontWeight: 700 }}>
          {data.icon}
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{data.title}</span>
        </div>
        {data.body && <div style={{ fontSize: 13, opacity: 0.9 }}>{data.body}</div>}
        {data.subtext && <div style={{ fontSize: 11, opacity: 0.6 }}>{data.subtext}</div>}
      </div>
    </div>
  );
}
