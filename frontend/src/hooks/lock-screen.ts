// Lookup 14: the lock screen. The router patch waits while it is up (the tree differs).
import { securityStore } from '../steam/globals';
import { sleep } from '../util/sleep';

export function lockScreenActive(): boolean {
  try {
    const s = securityStore();
    return typeof s?.IsLockScreenActive === 'function' ? Boolean(s.IsLockScreenActive()) : false;
  } catch {
    return false;
  }
}

export async function waitForUnlock(): Promise<void> {
  while (lockScreenActive()) await sleep(500);
}
