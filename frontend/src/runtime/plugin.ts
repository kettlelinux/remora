// Plugin shapes: what a plugin factory returns, and what Remora keeps for each loaded plugin.
import type { ReactNode } from 'react';
import type { Compat } from '../ui/common/compat';

export const LOAD_LEGACY = 0;
export const LOAD_ESM = 1;

/** What `definePlugin(...)` factories return (modern and legacy). */
export interface PluginDefinition {
  name?: string;
  version?: string;
  icon?: ReactNode;
  content?: ReactNode;
  titleView?: ReactNode;
  /** Old decky-frontend-lib field; used when titleView is missing. */
  title?: ReactNode;
  alwaysRender?: boolean;
  onDismount?: () => void;
}

export interface LoadedPlugin extends PluginDefinition {
  name: string;
  version?: string;
  loadType: number;
  /** Set on the placeholder shown when the plugin failed to load. */
  loadError?: { message: string; stack: string };
}

/** An entry of loader/get_plugins. */
export interface InstalledPlugin {
  name: string;
  version?: string;
  load_type: number;
  disabled: boolean;
  /** Remora: whether it can work on this device (lists plus its binaries). */
  compat?: Compat;
}

export interface QueuedImport {
  name: string;
  version?: string;
  loadType: number;
  timeoutMs?: number;
}
