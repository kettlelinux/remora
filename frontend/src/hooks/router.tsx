// Lookup 6: Steam's router. Three parts, each with its own status:
//  - router-route:  Steam's `Route` component, to render plugin pages with.
//  - router-root:   the component that renders Steam's route lists, after-patched so its output
//                   goes through RouterHost (routes, route patches, global components).
//  - router-layout: where the route lists sit in that output.
// Without router-route, plugin pages are off but route patches and global components work.
import { afterPatch, findModuleByExport, findModuleExport, Patch, wrapReactType, EUIMode } from '@decky/ui';
import { cloneElement, isValidElement, useMemo, useSyncExternalStore, type ComponentType, type ReactElement, type ReactNode } from 'react';
import { SteamAny } from '../steam/globals';
import { attribute, errorStack } from '../util/errors';
import { logger } from '../util/logger';
import { sleep } from '../util/sleep';
import { PluginBoundary } from './error-boundary';
import { findFiber, rootFiber, typeSource } from './fiber';
import { lockScreenActive, waitForUnlock } from './lock-screen';
import { HookHandle, runStrategies } from './status';
import { uiMode } from './ui-mode';

const routeHook = new HookHandle('router-route', 'plugin pages (addRoute) and Remora\'s settings and store pages');
const rootHook = new HookHandle('router-root', 'plugin pages, route patches (addPatch) and global components');
const layoutHook = new HookHandle('router-layout', 'plugin pages and route patches');
const log = logger('router');

export type RoutePatch = (props: SteamAny) => SteamAny;
interface RouteEntry {
  component: ComponentType;
  props: Record<string, unknown>;
}

/** Router state. `_routePatches` is read directly by TabMaster; keep its type. */
export class RouterState {
  routes = new Map<string, RouteEntry>();
  _routePatches = new Map<string, Set<RoutePatch>>();
  globalComponents = new Map<string, ComponentType>();
  private version = 0;
  private listeners = new Set<() => void>();

  changed(): void {
    this.version++;
    for (const l of [...this.listeners]) l();
  }
  subscribe = (l: () => void) => {
    this.listeners.add(l);
    return () => void this.listeners.delete(l);
  };
  getVersion = () => this.version;
}

// `routePath:x.match?.path` (current Steam passes it straight on), older `routePath:null===(...)`
const ROUTE_SRC = [/routePath:[\w$]+\.match\?\.path\b/, /routePath:null===\(/];

function findRouteComponent(): { value: SteamAny; strategy: string } | undefined {
  return runStrategies<SteamAny>(routeHook, [
    {
      name: "'router-backstack' module",
      find: () => {
        const m = findModuleByExport((e: SteamAny) => e === 'router-backstack', 20);
        if (!m) return undefined;
        return Object.values(m).find((e) => typeof e === 'function' && ROUTE_SRC.some((re) => re.test(typeSource(e))));
      },
    },
    { name: 'routePath source anywhere', find: () => findModuleExport((e: SteamAny) => typeof e === 'function' && ROUTE_SRC.some((re) => re.test(typeSource(e)))) },
  ], false);
}

type El = ReactElement<SteamAny>;
const isEl = (x: unknown): x is El => isValidElement(x);
const hasPath = (x: unknown) => isEl(x) && x.props?.path !== undefined;
type Loc = number[];

/** Where the route lists are: [main, ...others]. Each Loc is the path of child indices. */
function locateLists(tree: El): { lists: Loc[]; strategy: string } | undefined {
  try {
    const kids = tree.props?.children;
    const main = kids?.[0]?.props?.children;
    if (Array.isArray(main) && main.some(hasPath)) {
      const lists: Loc[] = [[0]];
      const game = kids?.[1]?.props?.children;
      if (Array.isArray(game) && game.some(hasPath)) lists.push([1]);
      return { lists, strategy: 'children[0] main, children[1] in-game' };
    }
  } catch {
    // fall through to the structural search
  }
  const lists: Loc[] = [];
  const walk = (el: unknown, loc: Loc, depth: number) => {
    if (!isEl(el) || depth < 0 || lists.length > 4) return;
    const c = el.props?.children;
    if (Array.isArray(c)) {
      if (c.filter(hasPath).length >= 2) {
        lists.push(loc);
        return;
      }
      c.forEach((k, i) => walk(k, [...loc, i], depth - 1));
    } else if (c !== undefined) walk(c, [...loc, 0], depth - 1);
  };
  walk(tree, [], 8);
  return lists.length ? { lists, strategy: 'structural search for Route lists' } : undefined;
}

function updateAt(el: El, loc: Loc, fn: (children: SteamAny) => SteamAny): El {
  const kids = el.props.children;
  if (!loc.length) return cloneElement(el, { children: fn(kids) });
  const [i, ...rest] = loc as [number, ...number[]];
  if (Array.isArray(kids)) {
    const next = kids.slice();
    next[i] = updateAt(kids[i], rest, fn);
    return cloneElement(el, { children: next });
  }
  return cloneElement(el, { children: updateAt(kids, rest, fn) });
}

function getAt(el: El, loc: Loc): SteamAny {
  let cur: SteamAny = el;
  for (const i of loc) {
    const k = cur?.props?.children;
    cur = Array.isArray(k) ? k[i] : k;
  }
  return cur?.props?.children;
}

/**
 * A per-render copy of a route's child, so plugin patches never touch Steam's own element.
 *
 * Known cost (finding 17, needs a device check before changing): for memo/forwardRef children,
 * wrapReactType makes a new type object on every root render, so React remounts the patched
 * page whenever the root re-renders. Caching the wrapped type per (original type, patch-list
 * version) is NOT safe as things stand: route patches run on every render and commonly
 * afterPatch() the wrapped type in place, so a cached type would collect one more patch per
 * render (handlers run N times). Fixing it needs patches to be applied once per cached type,
 * which changes when plugin patch code runs; left as is until it can be tested on hardware.
 */
function retype(child: SteamAny): SteamAny {
  if (!isEl(child)) return child;
  const copy: SteamAny = { ...child, props: { ...(child.props as object) } };
  if (copy.type && typeof copy.type === 'object') wrapReactType(copy);
  return copy;
}

function pathsOf(el: El): string[] {
  const p = el.props?.path;
  return Array.isArray(p) ? p : typeof p === 'string' ? [p] : [];
}

function applyPatches(list: SteamAny, patches: Map<string, Set<RoutePatch>>): SteamAny {
  if (!patches.size) return list;
  const one = (el: SteamAny): SteamAny => {
    if (Array.isArray(el)) return el.map(one);
    if (!isEl(el)) return el;
    const chain = pathsOf(el).flatMap((p) => [...(patches.get(p) ?? [])]);
    if (!chain.length) return el;
    let props: SteamAny = { ...(el.props as object), children: retype(el.props.children) };
    for (const patch of chain) {
      try {
        props = patch(props) ?? props;
      } catch (e) {
        const src = attribute(errorStack(e));
        log.error(`route patch for ${pathsOf(el).join(',')} failed${src.kind === 'plugin' ? ` (plugin ${src.name})` : ''}`, e);
      }
    }
    return cloneElement(el, { children: props.children });
  };
  return Array.isArray(list) ? list.map(one) : one(list);
}

export class RouterHook {
  readonly routerState = new RouterState();
  private RouteComponent?: SteamAny;
  private rootPatch?: Patch;
  private stopped = false;
  private lastLayout = '';

  constructor() {
    window.__ROUTER_HOOK_INSTANCE?.deinit?.();
    window.__ROUTER_HOOK_INSTANCE = this;
  }

  get routes(): Map<string, RouteEntry> {
    return this.routerState.routes;
  }

  addRoute = (path: string, component: ComponentType, props: Record<string, unknown> = {}): void => {
    this.routerState.routes.set(path, { component, props });
    this.routerState.changed();
  };

  removeRoute = (path: string): void => {
    if (this.routerState.routes.delete(path)) this.routerState.changed();
  };

  addPatch = (path: string, patch: RoutePatch): RoutePatch => {
    let set = this.routerState._routePatches.get(path);
    if (!set) this.routerState._routePatches.set(path, (set = new Set()));
    set.add(patch);
    this.routerState.changed();
    return patch;
  };

  removePatch = (path: string, patch: RoutePatch): void => {
    const set = this.routerState._routePatches.get(path);
    if (!set?.delete(patch)) return;
    if (!set.size) this.routerState._routePatches.delete(path);
    this.routerState.changed();
  };

  addGlobalComponent = (name: string, component: ComponentType): void => {
    this.routerState.globalComponents.set(name, component);
    this.routerState.changed();
  };

  removeGlobalComponent = (name: string): void => {
    if (this.routerState.globalComponents.delete(name)) this.routerState.changed();
  };

  waitForUnlock = (): Promise<void> => waitForUnlock();

  init(): void {
    this.RouteComponent = findRouteComponent()?.value;
    if (this.RouteComponent) routeHook.ok('found at boot');
    else routeHook.pending('not found by signature; will try the type of an existing route');
    void this.patchRootWhenReady();
  }

  private async patchRootWhenReady(): Promise<void> {
    if (uiMode.get() !== EUIMode.GamePad) rootHook.pending('waiting for Game Mode');
    while (!this.stopped && uiMode.get() !== EUIMode.GamePad) await sleep(500);
    let tries = 0;
    while (!this.stopped) {
      if (lockScreenActive()) {
        rootHook.pending('waiting for the lock screen');
        await waitForUnlock();
      }
      if (this.tryPatchRoot()) return;
      tries++;
      if (tries === 6) rootHook.fail('root component not found after 30 s; still retrying every 5 s');
      await sleep(5000);
    }
  }

  private tryPatchRoot(): boolean {
    const root = rootFiber();
    if (!root) return false;
    const found = runStrategies<SteamAny>(rootHook, [
      { name: 'Settings.Root() source', find: () => findFiber(root, (f) => typeSource(f.type).includes('Settings.Root()') && !f.pendingProps?.loggedIn) },
      {
        name: 'memo ancestor of the /library route',
        find: () => {
          let f = findFiber(root, (x) => pathsOf({ props: x.memoizedProps } as El).some((p) => p.startsWith('/library')));
          while (f && !(f.elementType && typeof f.elementType === 'object' && typeof f.elementType.type === 'function')) f = f.return;
          return f;
        },
      },
    ], false);
    if (!found) return false;
    const fiber = found.value;
    const et = fiber.elementType;
    if (!et || typeof et !== 'object' || typeof et.type !== 'function') {
      rootHook.fail(`root found via "${found.strategy}" but it is not memo-wrapped`);
      return true; // retrying cannot help
    }
    try {
      this.rootPatch = afterPatch(et, 'type', (_args: unknown[], ret: SteamAny) => {
        // after deinit (re-injection), a fiber may still call this function: pass Steam through
        if (this.stopped || !isEl(ret) || ret.type === RouterHost) return ret;
        return <RouterHost tree={ret} hook={this} />;
      });
      fiber.type = et.type;
      if (fiber.alternate) fiber.alternate.type = et.type;
      const how = this.forceRerender(fiber);
      rootHook.ok(found.strategy, how);
    } catch (e) {
      rootHook.fail(`patch failed: ${String(e)}`);
    }
    return true;
  }

  /** Makes the patched root render again; returns how (for the status detail). */
  private forceRerender(fiber: SteamAny): string {
    for (let f = fiber.return; f; f = f.return) {
      if (typeof f.stateNode?._remoraForceRerender === 'function') {
        f.stateNode._remoraForceRerender();
        return 'remounted through the patched ErrorBoundary';
      }
    }
    // No patched boundary above: defeat the memo bail-out and update the nearest class parent.
    for (const f of [fiber, fiber.alternate]) if (f?.memoizedProps) f.memoizedProps = { ...f.memoizedProps, __remora: Date.now() };
    for (let f = fiber.return; f; f = f.return) {
      if (typeof f.stateNode?.forceUpdate === 'function') {
        f.stateNode.forceUpdate();
        return 'forceUpdate on the nearest class parent';
      }
    }
    return "applies on Steam's next re-render";
  }

  /** Called by RouterHost on every render. */
  transform(tree: El): El {
    const found = locateLists(tree);
    if (!found) {
      if (this.lastLayout !== 'none') {
        this.lastLayout = 'none';
        layoutHook.fail('no route list in the root output');
      }
      return tree;
    }
    if (this.lastLayout !== found.strategy) {
      this.lastLayout = found.strategy;
      if (found.strategy.startsWith('structural')) layoutHook.degraded(found.strategy, `${found.lists.length} list(s)`);
      else layoutHook.ok(found.strategy);
    }
    const patches = this.routerState._routePatches;
    const [main, ...others] = found.lists as [Loc, ...Loc[]];
    let out = tree;
    for (const loc of others) out = updateAt(out, loc, (list) => applyPatches(list, patches));
    const routes = this.routeElements(getAt(out, main));
    out = updateAt(out, main, (list) => {
      const patched = applyPatches(list, patches);
      const arr = Array.isArray(patched) ? patched : [patched];
      return routes.length ? [...arr, routes] : arr;
    });
    return out;
  }

  private routeElements(mainList: SteamAny): ReactNode[] {
    const { routes } = this.routerState;
    if (!routes.size) return [];
    let Route = this.RouteComponent;
    if (!Route) {
      const sample = (Array.isArray(mainList) ? mainList : [mainList]).find(hasPath);
      if (sample && (typeof sample.type === 'function' || typeof sample.type === 'object')) {
        Route = this.RouteComponent = sample.type;
        routeHook.degraded('type of an existing route element', 'signature lookup failed');
      } else {
        routeHook.fail('no Route component');
        return [];
      }
    }
    return [...routes].map(([path, { component: C, props }]) => (
      <Route key={`remora-route:${path}`} path={path} {...props}>
        <PluginBoundary>
          <C />
        </PluginBoundary>
      </Route>
    ));
  }

  deinit(): void {
    this.stopped = true;
    try {
      this.rootPatch?.unpatch();
    } catch {
      // already gone
    }
    this.rootPatch = undefined;
  }
}

function GlobalComponents({ state }: { state: RouterState }) {
  useSyncExternalStore(state.subscribe, state.getVersion);
  return (
    <>
      {[...state.globalComponents].map(([name, C]) => (
        <PluginBoundary key={name} fallback={null}>
          <C />
        </PluginBoundary>
      ))}
    </>
  );
}

function RouterHost({ tree, hook }: { tree: El; hook: RouterHook }) {
  const version = useSyncExternalStore(hook.routerState.subscribe, hook.routerState.getVersion);
  const out = useMemo(() => {
    try {
      return hook.transform(tree);
    } catch (e) {
      log.error('route transform failed; rendering Steam unchanged', e);
      return tree;
    }
  }, [tree, version, hook]);
  return (
    <>
      {out}
      <GlobalComponents state={hook.routerState} />
    </>
  );
}
