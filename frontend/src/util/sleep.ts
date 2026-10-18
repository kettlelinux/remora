export const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

/** Polls `test` every `interval` ms until it returns a truthy value, or until `timeout` ms pass. */
export async function waitFor<T>(test: () => T | undefined | null | false, interval: number, timeout = Infinity): Promise<T | undefined> {
  const end = Date.now() + timeout;
  for (;;) {
    try {
      const v = test();
      if (v) return v;
    } catch {
      // a lookup that throws counts as "not yet"
    }
    if (Date.now() >= end) return undefined;
    await sleep(interval);
  }
}

/** Rejects with `message` when `p` has not settled after `ms`. */
export function withTimeout<T>(p: Promise<T>, ms: number, message: string): Promise<T> {
  if (!Number.isFinite(ms) || ms <= 0) return p;
  return new Promise<T>((resolve, reject) => {
    const t = setTimeout(() => reject(new Error(message)), ms);
    p.then(
      (v) => {
        clearTimeout(t);
        resolve(v);
      },
      (e) => {
        clearTimeout(t);
        reject(e);
      },
    );
  });
}
