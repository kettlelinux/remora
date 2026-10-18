// Lookup 9: Valve's error reporting store. Errors from plugins or Remora are kept out of Valve's
// crash reports, and reporting pauses for 30 s after one (follow-on errors are usually ours too).
import { callOriginal, findModuleExport, Patch, replacePatch } from '@decky/ui';
import { SteamAny } from '../steam/globals';
import { attribute, collectText } from '../util/errors';
import { HookHandle, runStrategies } from './status';

const hook = new HookHandle('error-reporting', "keeping plugin errors out of Valve's error reports");
const PAUSE_MS = 30_000;

export class ErrorReportFilter {
  private patch?: Patch;
  private pausedUntil = 0;

  init(): void {
    const found = runStrategies<SteamAny>(hook, [
      { name: 'class with prototype.BIsBlacklisted', find: () => findModuleExport((e: SteamAny) => typeof e?.prototype?.BIsBlacklisted === 'function')?.prototype },
      {
        name: 'instance with BIsBlacklisted',
        find: () => {
          const inst = findModuleExport((e: SteamAny) => e && typeof e === 'object' && typeof e.BIsBlacklisted === 'function');
          return inst ? Object.getPrototypeOf(inst) : undefined;
        },
      },
    ], false);
    if (!found) {
      hook.fail('no object with BIsBlacklisted');
      return;
    }
    try {
      const self = this;
      this.patch = replacePatch(found.value, 'BIsBlacklisted', function (args: unknown[]) {
        const now = Date.now();
        if (now < self.pausedUntil) return true;
        const text = collectText(args).join('\n');
        if (attribute(text).kind !== 'unknown') {
          self.pausedUntil = now + PAUSE_MS;
          return true;
        }
        return callOriginal;
      });
      hook.ok(found.strategy);
    } catch (e) {
      hook.fail(`patch failed: ${String(e)}`);
    }
  }

  deinit(): void {
    try {
      this.patch?.unpatch();
    } catch {
      // already gone
    }
    this.patch = undefined;
  }
}
