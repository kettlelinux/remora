// Build-time stand-in for `react/jsx-runtime`, bound late to window.SP_JSX.
import type * as JSXRuntime from 'react/jsx-runtime';

type J = typeof JSXRuntime;
const j = (): J => window.SP_JSX as unknown as J;

export const jsx: J['jsx'] = (type, props, key) => j().jsx(type, props, key);
export const jsxs: J['jsxs'] = (type, props, key) => (j().jsxs ?? j().jsx)(type, props, key);
export const Fragment: J['Fragment'] = window.SP_REACT.Fragment;
