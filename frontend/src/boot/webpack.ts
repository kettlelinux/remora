// Lookup 1 (boot copy): Steam's webpack require. @decky/ui has its own module cache; this one is
// only used before @decky/ui loads, to find React.
import { HookHandle, runStrategies } from '../hooks/status';
import { SteamAny, webpackChunks } from '../steam/globals';

type Require = SteamAny;
const hook = new HookHandle('webpack', 'everything: React, @decky/ui and all plugins need it');

function viaChunkArray(arr: SteamAny): Require | undefined {
  if (!arr || typeof arr.push !== 'function') return undefined;
  let req: Require;
  arr.push([[Symbol('remora')], {}, (r: Require) => (req = r)]);
  return req?.m ? req : undefined;
}

let cached: Require | undefined;

export function getRequire(): Require | undefined {
  if (cached) return cached;
  cached = runStrategies<Require>(hook, [
    { name: 'webpackChunksteamui.push', find: () => viaChunkArray(webpackChunks()) },
    {
      name: 'any window.webpackChunk* array',
      find: () => {
        for (const k of Object.keys(window)) {
          if (k.startsWith('webpackChunk') && k !== 'webpackChunksteamui') {
            const r = viaChunkArray((window as SteamAny)[k]);
            if (r) return r;
          }
        }
        return undefined;
      },
    },
  ])?.value;
  return cached;
}

/** Yields loaded module exports first (cheap), then requires every module once. */
export function* allModules(): Generator<SteamAny> {
  const req = getRequire();
  if (!req) return;
  const seen = new Set<string>();
  const cache: Record<string, SteamAny> = req.c ?? {};
  for (const id of Object.keys(cache)) {
    seen.add(id);
    const exp = cache[id]?.exports;
    if (exp) yield exp;
  }
  for (const id of Object.keys(req.m ?? {})) {
    if (seen.has(id)) continue;
    let exp: SteamAny;
    try {
      exp = req(id);
    } catch {
      continue; // module side effects failing is normal
    }
    if (exp) yield exp;
  }
}

export function findModule(test: (m: SteamAny) => boolean): SteamAny {
  for (const m of allModules()) {
    for (const candidate of [m, m?.default]) {
      try {
        if (candidate && typeof candidate === 'object' && test(candidate)) return candidate;
      } catch {
        // getters on odd modules can throw
      }
    }
  }
  return undefined;
}
