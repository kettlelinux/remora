import { useEffect, useState, useSyncExternalStore } from 'react';
import type { Store } from '../util/store';

export function useStore<T>(store: Store<T>): T {
  return useSyncExternalStore(store.subscribe, store.get);
}

/** Runs an async loader on mount and when `deps` change; returns [value, error, reload]. */
export function useAsync<T>(load: () => Promise<T>, deps: unknown[] = []): [T | undefined, unknown, () => void] {
  const [value, setValue] = useState<T>();
  const [error, setError] = useState<unknown>();
  const [n, setN] = useState(0);
  useEffect(() => {
    let live = true;
    setError(undefined);
    load().then(
      (v) => live && setValue(v),
      (e) => live && setError(e),
    );
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, n]);
  return [value, error, () => setN((x) => x + 1)];
}
