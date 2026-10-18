// @decky/ui runs its Steam lookups when it is evaluated, and a few of them throw (instead of
// leaving one component undefined) when Steam has moved something: Object.values() over the
// common UI module, the PanelSection module and the react-router module. One such throw would
// stop @decky/ui, and with it every plugin. Before loading it, Remora checks each of these and,
// for any that is missing, registers a small stand-in webpack module so @decky/ui evaluates.
// The stand-ins render plain elements. @decky/ui itself is not changed.
//
// This file runs before Remora's React shims may be evaluated, so it uses window.SP_REACT directly.
import { HookHandle } from '../hooks/status';
import { SteamAny, webpackChunks } from '../steam/globals';
import { findModule } from './webpack';

const hook = new HookHandle('dfl-preflight', '@decky/ui itself; stand-ins keep it loading with plain components');

type Props = { children?: unknown; title?: unknown };
const h = (type: string, props: object | null, ...children: unknown[]) => (window as SteamAny).SP_REACT.createElement(type, props, ...children);

function withSource<T extends object>(fn: T, src: string): T {
  Object.defineProperty(fn, 'toString', { value: () => src });
  return fn;
}

function commonUIStub(): object {
  // @decky/ui recognises the common UI module by size (> 60 exports) and one export carrying a
  // React context; every component it then searches for in here stays undefined.
  const m: Record<string, unknown> = { remoraContext: { contextType: { _currentValue: {} } } };
  for (let i = 0; i < 61; i++) m[`remoraStub${i}`] = null;
  return m;
}

function panelStub(): object {
  const PanelSection = withSource(
    ({ title, children }: Props) =>
      h('div', { style: { padding: '0 16px', marginBottom: 12 } }, title ? h('div', { style: { fontWeight: 700, fontSize: 12, textTransform: 'uppercase', opacity: 0.7, margin: '8px 0' } }, title) : null, children),
    'function RemoraPanelSection(){/* .PanelSection stand-in */}',
  );
  const PanelSectionRow = ({ children }: Props) => h('div', { style: { padding: '4px 0' } }, children);
  return { a: PanelSection, b: PanelSectionRow };
}

function routerStub(): object {
  return {
    remoraRouter: { computeRootMatch: (pathname: string) => ({ path: '/', url: '/', params: {}, isExact: pathname === '/' }) },
    // Matches @decky/ui's useParams search; returns no params.
    useParams: withSource(() => ({}), 'function(){var e=null;return e?e.params:{}}'),
  };
}

const checks: { id: string; present: () => boolean; stub: () => object }[] = [
  {
    id: 'common UI module',
    present: () =>
      Boolean(findModule((m) => Object.keys(m).length > 60 && Object.keys(m).some((k) => m[k]?.contextType?._currentValue))),
    stub: commonUIStub,
  },
  {
    id: 'PanelSection module',
    present: () => Boolean(findModule((m) => Object.values(m).some((e: SteamAny) => typeof e?.toString === 'function' && String(e.toString()).includes('.PanelSection')))),
    stub: panelStub,
  },
  { id: 'react-router module', present: () => Boolean(findModule((m) => Object.values(m).some((e: SteamAny) => e?.computeRootMatch))), stub: routerStub },
];

/** Returns false when @decky/ui cannot load at all (React internals it needs are missing). */
export function preflightDFL(): boolean {
  const R = (window as SteamAny).SP_REACT;
  if (!R?.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED?.ReactCurrentDispatcher && !R?.__CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE) {
    hook.fail("Steam's React has neither React 18 nor React 19 internals; @decky/ui cannot load");
    return false;
  }
  const stubs: Record<string, (module: { exports: unknown }) => void> = {};
  const missing: string[] = [];
  for (const c of checks) {
    let ok = false;
    try {
      ok = c.present();
    } catch {
      ok = false;
    }
    if (ok) continue;
    missing.push(c.id);
    const exports = c.stub();
    stubs[`remora-stub:${c.id.replace(/\W+/g, '-')}`] = (module) => {
      module.exports = exports;
    };
  }
  if (!missing.length) {
    hook.ok('all eval-time lookups present');
    return true;
  }
  try {
    webpackChunks().push([[Symbol('remora-stubs')], stubs]);
    hook.degraded('stand-in modules', `missing in Steam: ${missing.join(', ')}; the @decky/ui components that need them are plain or absent`);
  } catch (e) {
    hook.fail(`could not register stand-ins for ${missing.join(', ')}: ${String(e)}`);
  }
  return true;
}
