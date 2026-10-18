// The legacy ServerAPI passed to type-0 (decky-frontend-lib) plugin factories. Its quirks are
// kept on purpose: callPluginMethod and fetchNoCors never reject, fetchNoCors defaults to POST
// and renames `body` to `data`.
import type { RouterHook } from '../hooks/router';
import type { Toaster } from '../hooks/toaster';
import type { Backend } from '../runtime/backend';
import { errorText } from '../util/errors';
import { logger } from '../util/logger';
import { filePickerApi } from './file-picker';
import { tabApi } from './tabs';

const log = logger('server-api');

export interface LegacyResult<T = unknown> {
  success: boolean;
  result: T;
}

async function settle<T>(p: Promise<T>): Promise<LegacyResult> {
  try {
    const r = await p;
    if (r && typeof r === 'object' && 'success' in r && 'result' in r) return r as unknown as LegacyResult;
    return { success: true, result: r };
  } catch (e) {
    return { success: false, result: errorText(e) };
  }
}

export function createServerAPI(name: string, deps: { backend: Backend; routerHook: RouterHook; toaster: Toaster }) {
  const { backend } = deps;
  const { openFilePicker, openFilePickerLegacy } = filePickerApi(backend);
  return {
    routerHook: deps.routerHook,
    toaster: deps.toaster,
    callPluginMethod: (method: string, kwargs: Record<string, unknown> = {}) => settle(backend.call('loader/call_legacy_plugin_method', name, method, kwargs)),
    callServerMethod: (method: string, args: Record<string, unknown> = {}) => {
      log.warn(`${name} uses the deprecated callServerMethod("${method}")`);
      return settle(backend.call('utilities/_call_legacy_utility', method, args));
    },
    fetchNoCors: async (url: string, request: Record<string, unknown> = {}): Promise<LegacyResult> => {
      const { method, body, ...rest } = request;
      const opts: Record<string, unknown> = { ...rest };
      if (body !== undefined) opts.data = body;
      try {
        const result = await backend.call('utilities/http_request', (method as string | undefined) ?? 'POST', url, opts);
        return { success: true, result };
      } catch (e) {
        return { success: false, result: errorText(e) };
      }
    },
    ...tabApi(backend),
    openFilePicker: openFilePickerLegacy,
    openFilePickerV2: openFilePicker,
  };
}
