// The backend auth token. The CDP bootstrap sets window.deckyAuthToken (and remoraAuthToken);
// GET /auth/token is the fallback, and is asked again when the token stops working.
import { logger } from '../util/logger';

const log = logger('token');
let fetched: string | undefined;

export function currentToken(): string | undefined {
  return window.deckyAuthToken || window.remoraAuthToken || fetched;
}

export async function refreshToken(): Promise<string | undefined> {
  try {
    const r = await fetch('http://127.0.0.1:1337/auth/token', { credentials: 'include' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const tok = (await r.text()).trim();
    if (tok) {
      fetched = tok;
      // Plugins (and old code) read the global directly.
      window.deckyAuthToken = tok;
    }
    return tok || undefined;
  } catch (e) {
    log.warn('GET /auth/token failed', e);
    return undefined;
  }
}

export async function ensureToken(): Promise<string | undefined> {
  return currentToken() ?? (await refreshToken());
}
