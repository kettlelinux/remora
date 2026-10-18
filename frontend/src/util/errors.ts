// Who is to blame for an error, from the URLs in its stack. The schemes come from plugin builds
// (@decky/rollup sourcemaps, the legacy sourceURL Remora appends), so they must stay as they are.
export type ErrorSource = { kind: 'plugin'; name: string } | { kind: 'loader' } | { kind: 'unknown' };

const PLUGIN_PATTERNS = [
  /https?:\/\/(?:localhost|127\.0\.0\.1):1337\/plugins\/([^/]+)\//,
  /decky:\/\/decky\/plugin\/([^/]+)\//,
  /decky:\/\/decky\/legacy_plugin\/([^/]+)\/index\.js/,
];
const LOADER_PATTERN = /https?:\/\/(?:localhost|127\.0\.0\.1):1337\//;

function decode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

export function attribute(stack: string | undefined | null): ErrorSource {
  if (!stack) return { kind: 'unknown' };
  for (const re of PLUGIN_PATTERNS) {
    const m = re.exec(stack);
    if (m?.[1]) return { kind: 'plugin', name: decode(m[1]) };
  }
  return LOADER_PATTERN.test(stack) ? { kind: 'loader' } : { kind: 'unknown' };
}

export function errorText(e: unknown): string {
  if (e instanceof Error) return e.message || e.name;
  if (typeof e === 'string') return e;
  try {
    return JSON.stringify(e);
  } catch {
    return String(e);
  }
}

export function errorStack(e: unknown): string {
  if (e instanceof Error) return e.stack || `${e.name}: ${e.message}`;
  return errorText(e);
}

/** Collects the strings in a value (a few levels deep), for matching error reports by URL. */
export function collectText(v: unknown, depth = 3, out: string[] = []): string[] {
  if (out.length > 200) return out;
  if (typeof v === 'string') out.push(v);
  else if (v instanceof Error) out.push(v.stack ?? '', v.message);
  else if (v && typeof v === 'object' && depth > 0) {
    for (const k of Object.keys(v).slice(0, 50)) {
      try {
        collectText((v as Record<string, unknown>)[k], depth - 1, out);
      } catch {
        // getters on Steam objects can throw
      }
    }
  }
  return out;
}
