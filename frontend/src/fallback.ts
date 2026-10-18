// Crash-loop screen. The backend injects this instead of index.js once Steam's web helper has
// crashed repeatedly, then stops. Self-contained on purpose: no imports, no React, no @decky/ui,
// nothing that could be what keeps crashing.
type AnyWin = Window & { deckyFallbackLoaded?: boolean; SteamClient?: any; FocusNavController?: any; GamepadNavTree?: any }; // eslint-disable-line @typescript-eslint/no-explicit-any

const w = window as AnyWin;

function findSP(): Window | undefined {
  try {
    if (document.title === 'SP') return window;
    const ctl = w.GamepadNavTree?.m_context?.m_controller ?? w.FocusNavController;
    const ctx = ctl?.m_ActiveContext ?? ctl?.m_LastActiveContext;
    const trees: any[] = ctx?.m_rgGamepadNavigationTrees ?? []; // eslint-disable-line @typescript-eslint/no-explicit-any
    const t = trees.find((x) => ['GamepadUI_Full_Root', 'root_1_', 'GamepadUI_VR_Full_Root'].includes(x?.m_ID));
    return t?.Root?.Element?.ownerDocument?.defaultView ?? undefined;
  } catch {
    return undefined;
  }
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function main(): Promise<void> {
  if (w.deckyFallbackLoaded) return;
  w.deckyFallbackLoaded = true;
  await sleep(4000);
  const sp = findSP() ?? window;
  const doc = sp.document;
  const box = doc.createElement('div');
  box.setAttribute('style', [
    'position:fixed', 'inset:0', 'z-index:2147483647', 'display:flex', 'flex-direction:column',
    'align-items:center', 'justify-content:center', 'gap:16px', 'padding:32px', 'text-align:center',
    'background:rgba(14,20,27,0.96)', 'color:#fff', 'font-family:"Motiva Sans",Arial,sans-serif',
  ].join(';'));
  const title = doc.createElement('div');
  title.setAttribute('style', 'font-size:28px;font-weight:700');
  title.textContent = 'Remora: crash loop detected';
  const body = doc.createElement('div');
  body.setAttribute('style', 'font-size:18px;max-width:720px;line-height:1.5');
  body.textContent =
    'The Steam interface crashed several times in a row while plugins were loaded. ' +
    'Remora has turned itself off for this session, and Steam will restart in 10 seconds. ' +
    'If this keeps happening, disable or remove the plugin that was installed or updated last.';
  const count = doc.createElement('div');
  count.setAttribute('style', 'font-size:16px;opacity:0.7');
  box.append(title, body, count);
  (doc.body ?? doc.documentElement).appendChild(box);
  for (let s = 10; s > 0; s--) {
    count.textContent = `Restarting Steam in ${s} s`;
    await sleep(1000);
  }
  try {
    w.SteamClient?.User?.StartShutdown?.(false);
  } catch (e) {
    console.error('[Remora fallback] StartShutdown failed', e);
  }
}

void main();
export {};
