// Lookup 2: React, ReactDOM and the JSX runtime from Steam's webpack. They must be Steam's own
// instances: hooks and context break with a second copy.
import { HookHandle, runStrategies } from '../hooks/status';
import { SteamAny } from '../steam/globals';
import { findModule } from './webpack';

const reactHook = new HookHandle('react', 'everything: plugins render with Steam\'s React');
const domHook = new HookHandle('react-dom', 'plugins that import react-dom (portals)');
const jsxHook = new HookHandle('jsx-runtime', 'plugins built with the automatic JSX runtime');

const win = window as SteamAny;

/** Returns false when React itself cannot be found (nothing can run then). */
export function ensureReactGlobals(): boolean {
  if (!win.SP_REACT) {
    const r = runStrategies(reactHook, [
      { name: 'Component+PureComponent+useLayoutEffect', find: () => findModule((m) => m.Component && m.PureComponent && m.useLayoutEffect) },
      { name: 'createElement+useState+Fragment', find: () => findModule((m) => m.createElement && m.useState && m.Fragment && m.Component) },
    ]);
    if (!r) return false;
    win.SP_REACT = r.value;
  } else {
    reactHook.ok('already defined');
  }

  if (!win.SP_REACTDOM) {
    const r = runStrategies(domHook, [
      { name: 'createPortal+createRoot', find: () => findModule((m) => m.createPortal && m.createRoot) },
      { name: 'createPortal+DOM internals', find: () => findModule((m) => m.createPortal && m.__DOM_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE) },
      { name: 'createPortal+flushSync', find: () => findModule((m) => m.createPortal && m.flushSync) },
    ]);
    // @decky/ui reads SP_REACTDOM.version; keep a stub so it does not throw.
    win.SP_REACTDOM = r?.value ?? { version: String(win.SP_REACT.version ?? ''), createPortal: () => null };
  } else {
    domHook.ok('already defined');
  }

  if (!win.SP_JSX) {
    const React = win.SP_REACT;
    const r = runStrategies(jsxHook, [
      { name: 'jsx+jsxs module', find: () => findModule((m) => typeof m.jsx === 'function' && typeof m.jsxs === 'function' && !m.createElement) },
      {
        name: 'lone jsx module',
        find: () => {
          const m = findModule((x) => typeof x.jsx === 'function' && !x.createElement && Object.keys(x).length <= 4);
          return m ? { jsx: m.jsx, jsxs: m.jsx, Fragment: React.Fragment } : undefined;
        },
      },
      {
        name: 'built from createElement',
        find: () => {
          const jsx = (type: unknown, props: SteamAny, key?: unknown) =>
            React.createElement(type, key === undefined ? props : { ...props, key });
          return { jsx, jsxs: jsx, Fragment: React.Fragment };
        },
      },
    ]);
    win.SP_JSX = r?.value;
  } else {
    jsxHook.ok('already defined');
  }
  return true;
}
