// fetchNoCors and getExternalResourceURL: requests through the backend's /fetch proxy, which
// turns X-Decky-Header-* into real headers and adds CORS headers to the reply.
import { currentToken } from '../runtime/token';

export interface DeckyRequestInit extends RequestInit {
  excludedHeaders?: string[];
}

export function getExternalResourceURL(url: string): string {
  return `http://127.0.0.1:1337/fetch?auth=${encodeURIComponent(currentToken() ?? '')}&fetch_url=${encodeURIComponent(url)}`;
}

export function fetchNoCors(url: string, init: DeckyRequestInit = {}): Promise<Response> {
  const { headers, excludedHeaders, ...rest } = init;
  const out = new Headers();
  new Headers(headers ?? {}).forEach((value, key) => out.set(`X-Decky-Header-${key}`, value));
  if (excludedHeaders?.length) out.set('X-Decky-Fetch-Excluded-Headers', excludedHeaders.join(', '));
  return fetch(getExternalResourceURL(url), { ...rest, headers: out, credentials: 'include' });
}
