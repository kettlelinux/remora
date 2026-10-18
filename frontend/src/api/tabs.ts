// CDP helpers that run in other Steam tabs (executed by the backend).
import type { Backend } from '../runtime/backend';
import { errorText } from '../util/errors';

export function tabApi(backend: Backend) {
  return {
    executeInTab: async (tab: string, runAsync: boolean, code: string): Promise<{ success: boolean; result: unknown }> => {
      try {
        return await backend.call<{ success: boolean; result: unknown }>('utilities/execute_in_tab', tab, runAsync, code);
      } catch (e) {
        return { success: false, result: errorText(e) };
      }
    },
    injectCssIntoTab: (tab: string, css: string): Promise<string> => backend.call<string>('utilities/inject_css_into_tab', tab, css),
    removeCssFromTab: (tab: string, cssId: string): Promise<void> => backend.call<void>('utilities/remove_css_from_tab', tab, cssId),
  };
}
