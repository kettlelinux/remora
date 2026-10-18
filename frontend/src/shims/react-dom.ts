// Build-time stand-in for `react-dom`, bound to window.SP_REACTDOM.
import type * as ReactDOMNS from 'react-dom';

type D = typeof ReactDOMNS;
const d = (): D => window.SP_REACTDOM as unknown as D;

const ReactDOM = new Proxy({} as D, {
  get: (_t, key) => (d() as unknown as Record<PropertyKey, unknown>)[key],
});
export default ReactDOM;
export const createPortal: D['createPortal'] = (...a) => d().createPortal(...a);
