// Lookup 8: Steam's ErrorBoundary class. Remora patches its render so a crash anywhere in Steam's
// UI shows Remora's crash screen (with the likely culprit) instead of Valve's, and adds a method
// the router patch uses to force a remount. Plugin surfaces are wrapped in Remora's own
// boundary below, which needs no Steam lookup at all.
import { callOriginal, ErrorBoundary as DflErrorBoundary, findModuleExport, Patch, replacePatch } from '@decky/ui';
import { Component, Fragment, type ErrorInfo, type ReactNode } from 'react';
import { SteamAny } from '../steam/globals';
import { attribute, errorStack } from '../util/errors';
import { logger } from '../util/logger';
import { CrashScreen, PluginCrashCard } from '../ui/common/CrashScreen';
import { HookHandle, runStrategies } from './status';

const hook = new HookHandle('error-boundary', "Remora's crash screen and plugin blame for Steam UI errors; forced re-render after the router patch");
const log = logger('error-boundary');

const isBoundary = (e: SteamAny) => typeof e?.prototype?.Reset === 'function' && typeof e?.prototype?.componentDidCatch === 'function';

export function findSteamErrorBoundary(): SteamAny {
  return runStrategies<SteamAny>(hook, [
    { name: '@decky/ui ErrorBoundary', find: () => DflErrorBoundary },
    {
      name: 'Reset+componentDidCatch+lastErrorKey',
      find: () => findModuleExport((e: SteamAny) => isBoundary(e) && String(e.prototype.render).includes('lastErrorKey')),
    },
    { name: 'Reset+componentDidCatch', find: () => findModuleExport((e: SteamAny) => isBoundary(e) && typeof e.prototype.render === 'function') },
  ], false)?.value;
}

/** True while a Steam ErrorBoundary instance is showing an error. */
function showingError(self: SteamAny): boolean {
  const st = self?.state;
  if (!st?.error) return false;
  if ('lastErrorKey' in st && self.props && 'errorKey' in self.props) return st.lastErrorKey === self.props.errorKey;
  return true;
}

export class ErrorBoundaryHook {
  private patch?: Patch;
  private proto?: SteamAny;

  constructor() {
    window.__ERRORBOUNDARY_HOOK_INSTANCE?.deinit?.();
    window.__ERRORBOUNDARY_HOOK_INSTANCE = this;
  }

  init(): void {
    const EB = findSteamErrorBoundary();
    if (!EB) {
      hook.fail('Steam ErrorBoundary class not found');
      return;
    }
    try {
      const proto = EB.prototype;
      this.proto = proto;
      this.patch = replacePatch(proto, 'render', function (this: SteamAny) {
        if (showingError(this)) {
          if (this.props?.fallback !== undefined) return callOriginal;
          const err = this.state.error;
          return <CrashScreen error={err} reset={() => this.Reset?.()} />;
        }
        if (this._remoraKey) {
          // Remount the children once with a new key, so a patched component type takes effect.
          return <Fragment key={`remora-${this._remoraKey}`}>{this.__remoraOriginalRender()}</Fragment>;
        }
        return callOriginal;
      });
      const patchRef = this.patch;
      proto.__remoraOriginalRender = function (this: SteamAny) {
        return patchRef.original.call(this);
      };
      proto._remoraForceRerender = function (this: SteamAny) {
        this._remoraKey = (this._remoraKey ?? 0) + 1;
        this.forceUpdate();
      };
      hook.ok('render replace-patch');
    } catch (e) {
      hook.fail(`patch failed: ${String(e)}`);
    }
  }

  deinit(): void {
    try {
      this.patch?.unpatch();
    } catch (e) {
      log.warn('unpatch failed', e);
    }
    if (this.proto) {
      delete this.proto._remoraForceRerender;
      delete this.proto.__remoraOriginalRender;
    }
    this.patch = undefined;
  }
}

interface BoundaryProps {
  children?: ReactNode;
  /** Plugin the surface belongs to (for the error card); omit for Remora's own UI. */
  plugin?: string;
  /** What to render on error instead of the default card; `null` renders nothing. */
  fallback?: ReactNode;
}

interface BoundaryState {
  failed: boolean;
  error?: unknown;
  key: number;
}

/** Remora's own error boundary for every plugin surface: QAM content, routes, toasts, globals. */
export class PluginBoundary extends Component<BoundaryProps, BoundaryState> {
  override state: BoundaryState = { failed: false, key: 0 };

  static getDerivedStateFromError(error: unknown): Partial<BoundaryState> {
    return { failed: true, error };
  }

  override componentDidCatch(error: unknown, info: ErrorInfo): void {
    const src = attribute(`${errorStack(error)}\n${info?.componentStack ?? ''}`);
    log.error(`caught in ${this.props.plugin ?? (src.kind === 'plugin' ? src.name : 'Remora')}`, error);
  }

  private retry = () => this.setState((s) => ({ failed: false, error: undefined, key: s.key + 1 }));

  override render(): ReactNode {
    if (this.state.failed) {
      if (this.props.fallback !== undefined) return this.props.fallback;
      const src = attribute(errorStack(this.state.error));
      const plugin = this.props.plugin ?? (src.kind === 'plugin' ? src.name : undefined);
      return <PluginCrashCard error={this.state.error} plugin={plugin} retry={this.retry} />;
    }
    return <Fragment key={this.state.key}>{this.props.children}</Fragment>;
  }
}
