// Lookup 5: the Quick Access menu's tab list. Remora finds the memo-wrapped QAM components,
// after-patches their render, and pushes its own tab (key 999, the key plugins open with
// QuickAccessTab.Decky) into the `tabs` array on each render. The render argument `visible`
// drives useQuickAccessVisible.
import { afterPatch, createReactTreePatcher, findInReactTree, findModuleByExport, Patch, QuickAccessTab } from '@decky/ui';
import type { ReactNode } from 'react';
import { reportQamVisible } from '../runtime/qam-state';
import { SteamAny } from '../steam/globals';
import { logger } from '../util/logger';
import { findFibers, rootFiber, typeSource } from './fiber';
import { HookHandle, runStrategies } from './status';

const hook = new HookHandle('qam-tab', "Remora's Quick Access tab (plugins' panels); routes and toasts keep working");
const log = logger('qam-tab');

const MARKERS = ['QuickAccessMenuBrowserView', 'QuickAccessMenuEmbedded'];
const hasTabs = (n: SteamAny) => Array.isArray(n?.props?.tabs);

function qamExports(module: SteamAny, test: (src: string) => boolean): SteamAny[] {
  if (!module || typeof module !== 'object') return [];
  const out: SteamAny[] = [];
  for (const k of Object.keys(module)) {
    try {
      const e = module[k];
      if (e && typeof e === 'object' && typeof e.type === 'function' && test(typeSource(e.type))) out.push(e);
    } catch {
      // getter threw
    }
  }
  return out;
}

export class TabsHook {
  private patches: Patch[] = [];
  private tab?: SteamAny;
  private renders = 0;
  private injected = false;

  constructor(
    private icon: () => ReactNode,
    private panel: () => ReactNode,
  ) {
    window.__TABS_HOOK_INSTANCE?.deinit?.();
    window.__TABS_HOOK_INSTANCE = this;
  }

  init(): void {
    const found = runStrategies<SteamAny[]>(hook, [
      {
        name: 'module with QuickAccessMenuBrowserView',
        find: () => {
          const m = findModuleByExport((e: SteamAny) => typeSource(e?.type).includes(MARKERS[0]!));
          const list = qamExports(m, (src) => MARKERS.some((x) => src.includes(x)));
          return list.length ? list : undefined;
        },
      },
      {
        name: 'module with QuickAccessMenu + visible',
        find: () => {
          const test = (src: string) => src.includes('QuickAccessMenu') && src.includes('visible');
          const m = findModuleByExport((e: SteamAny) => test(typeSource(e?.type)));
          const list = qamExports(m, test);
          return list.length ? list : undefined;
        },
      },
    ], false);
    if (!found) {
      hook.fail('QAM component not found');
      return;
    }

    const inner = createReactTreePatcher(
      [(tree: SteamAny) => findInReactTree(tree, (n: SteamAny) => n?.props?.onFocusNavDeactivated !== undefined)],
      (_args: unknown[], ret: SteamAny) => {
        const node = findInReactTree(ret, hasTabs);
        if (node) this.inject(node.props.tabs);
        return ret;
      },
      'RemoraQAM',
    );

    try {
      for (const exp of found.value) {
        const original = exp.type;
        const patch = afterPatch(exp, 'type', (args: SteamAny[], ret: SteamAny) => {
          this.renders++;
          try {
            reportQamVisible(args?.[0]?.visible);
            const direct = findInReactTree(ret, hasTabs);
            if (direct) {
              this.inject(direct.props.tabs);
              return ret;
            }
            const out = inner(args, ret);
            if (!this.injected && this.renders === 5) hook.degraded(found.strategy, 'QAM renders but its tab list was not found');
            return out;
          } catch (e) {
            log.error('QAM patch failed during render', e);
            return ret;
          }
        });
        this.patches.push(patch);
        this.swapMounted(original, exp.type);
      }
      hook.ok(found.strategy, `${found.value.length} component(s) patched`);
    } catch (e) {
      hook.fail(`patch failed: ${String(e)}`);
    }
  }

  private tabObject(): SteamAny {
    this.tab ??= {
      key: QuickAccessTab.Decky,
      title: null,
      tab: this.icon(),
      panel: this.panel(),
      decky: true,
      remora: true,
      initialVisibility: false,
    };
    return this.tab;
  }

  private inject(tabs: SteamAny[]): void {
    if (tabs.some((t) => t?.key === QuickAccessTab.Decky)) {
      if (!tabs.some((t) => t?.remora)) log.warn('another tab already uses key 999; not adding Remora\'s');
      return;
    }
    tabs.push(this.tabObject());
    if (!this.injected) {
      this.injected = true;
      log.info('tab injected');
    }
  }

  /** A QAM that is already mounted keeps the old function until its fiber's type is swapped. */
  private swapMounted(original: SteamAny, patched: SteamAny): void {
    try {
      const root = rootFiber();
      if (!root) return;
      for (const f of findFibers(root, (x) => x.type === original)) {
        f.type = patched;
        if (f.alternate) f.alternate.type = patched;
      }
    } catch (e) {
      log.debug('mounted QAM swap skipped', e);
    }
  }

  deinit(): void {
    for (const p of this.patches) {
      try {
        p.unpatch();
      } catch {
        // already unpatched
      }
    }
    this.patches = [];
  }
}
