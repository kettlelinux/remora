// Walking React's fiber tree (Steam internals, hence the loose types).
import { getReactRoot } from '@decky/ui';
import { SteamAny } from '../steam/globals';

export type Fiber = SteamAny;

export function rootFiber(el: Element | null = document.getElementById('root')): Fiber | undefined {
  if (!el) return undefined;
  try {
    return getReactRoot(el as HTMLElement);
  } catch {
    return undefined;
  }
}

/** Depth-first search over child/sibling links, bounded so a huge tree cannot hang the UI. */
export function findFiber(start: Fiber, test: (f: Fiber) => boolean, limit = 20_000): Fiber | undefined {
  const stack: Fiber[] = start ? [start] : [];
  let n = 0;
  while (stack.length && n++ < limit) {
    const f = stack.pop();
    try {
      if (test(f)) return f;
    } catch {
      // ignore odd nodes
    }
    if (f.sibling) stack.push(f.sibling);
    if (f.child) stack.push(f.child);
  }
  return undefined;
}

export function findFibers(start: Fiber, test: (f: Fiber) => boolean, limit = 20_000): Fiber[] {
  const out: Fiber[] = [];
  findFiber(
    start,
    (f) => {
      if (test(f)) out.push(f);
      return false;
    },
    limit,
  );
  return out;
}

/** Source text of a component type (function, memo, forwardRef), or '' if it has none. */
export function typeSource(t: SteamAny): string {
  try {
    if (typeof t === 'function') return Function.prototype.toString.call(t);
    if (t && typeof t === 'object') return typeSource(t.type ?? t.render);
  } catch {
    // native or proxied functions
  }
  return '';
}
