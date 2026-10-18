// Build-time stand-in for `react`: every import resolves to Steam's own React instance
// (window.SP_REACT), the same way plugin builds map `react` to that global. Reads are late-bound
// so they always see the current object (@decky/ui swaps createElement briefly while it
// fake-renders components).
import type * as ReactNS from 'react';

type R = typeof ReactNS;
const r = (): R => window.SP_REACT as unknown as R;

const React = new Proxy({} as R, {
  get: (_t, key) => (r() as unknown as Record<PropertyKey, unknown>)[key],
});
export default React;

export const createElement: R['createElement'] = ((...a: Parameters<R['createElement']>) =>
  r().createElement(...a)) as R['createElement'];
export const cloneElement: R['cloneElement'] = ((...a: Parameters<R['cloneElement']>) =>
  r().cloneElement(...a)) as R['cloneElement'];
export const isValidElement: R['isValidElement'] = ((o: unknown) => r().isValidElement(o)) as R['isValidElement'];
export const createContext: R['createContext'] = ((v: unknown) => r().createContext(v)) as R['createContext'];
export const memo: R['memo'] = ((...a: Parameters<R['memo']>) => r().memo(...a)) as R['memo'];
export const forwardRef: R['forwardRef'] = ((f: Parameters<R['forwardRef']>[0]) => r().forwardRef(f)) as R['forwardRef'];
export const useState: R['useState'] = ((v?: unknown) => r().useState(v)) as R['useState'];
export const useEffect: R['useEffect'] = (f, d) => r().useEffect(f, d);
export const useLayoutEffect: R['useLayoutEffect'] = (f, d) => r().useLayoutEffect(f, d);
export const useMemo: R['useMemo'] = (f, d) => r().useMemo(f, d);
export const useCallback: R['useCallback'] = (f, d) => r().useCallback(f, d);
export const useRef: R['useRef'] = ((v?: unknown) => r().useRef(v)) as R['useRef'];
export const useContext: R['useContext'] = (c) => r().useContext(c);
export const useReducer: R['useReducer'] = ((...a: unknown[]) =>
  (r().useReducer as (...x: unknown[]) => unknown)(...a)) as R['useReducer'];
export const useSyncExternalStore: R['useSyncExternalStore'] = (s, g, gs) => r().useSyncExternalStore(s, g, gs);

// Classes and symbols cannot be late-bound wrappers; they are read when the module evaluates,
// which is always after the boot code has set SP_REACT.
export const Component: R['Component'] = r().Component;
export const PureComponent: R['PureComponent'] = r().PureComponent;
export const Fragment: R['Fragment'] = r().Fragment;
export const Children: R['Children'] = r().Children;
