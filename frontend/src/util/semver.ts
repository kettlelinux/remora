// Just enough semver for update badges: "1.2.3", "v1.2.3", "1.2.3-pre.1". A version that does
// not parse is never treated as older or newer, matching the Decky store's rule.
interface Parsed {
  core: [number, number, number];
  pre: string[];
}

const RE = /^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$/;

export function parse(v: string | undefined | null): Parsed | null {
  const m = RE.exec((v ?? '').trim());
  if (!m) return null;
  return { core: [Number(m[1]), Number(m[2]), Number(m[3])], pre: m[4] ? m[4].split('.') : [] };
}

/** -1 if a < b, 0 if equal, 1 if a > b, null if either does not parse. */
export function compare(a: string, b: string): -1 | 0 | 1 | null {
  const pa = parse(a);
  const pb = parse(b);
  if (!pa || !pb) return null;
  for (let i = 0; i < 3; i++) {
    if (pa.core[i] !== pb.core[i]) return pa.core[i]! < pb.core[i]! ? -1 : 1;
  }
  if (!pa.pre.length || !pb.pre.length) {
    if (pa.pre.length === pb.pre.length) return 0;
    return pa.pre.length ? -1 : 1;
  }
  const n = Math.max(pa.pre.length, pb.pre.length);
  for (let i = 0; i < n; i++) {
    const x = pa.pre[i];
    const y = pb.pre[i];
    if (x === undefined) return -1;
    if (y === undefined) return 1;
    if (x === y) continue;
    const nx = /^\d+$/.test(x);
    const ny = /^\d+$/.test(y);
    if (nx && ny) return Number(x) < Number(y) ? -1 : 1;
    if (nx !== ny) return nx ? -1 : 1;
    return x < y ? -1 : 1;
  }
  return 0;
}

/** True when `remote` is a real update over `installed` (missing installed counts as 0.0.0). */
export function isNewer(remote: string, installed: string | undefined): boolean {
  if (remote === installed) return false;
  return compare(remote, installed || '0.0.0') === 1;
}
