# Remora frontend

The code Remora injects into Steam's Game Mode UI (the SharedJSContext). It provides the globals
and APIs that Decky plugins expect (`SP_REACT`, `SP_JSX`, `SP_REACTDOM`, `DFL`, the `@decky/api`
init hook, the legacy ServerAPI, `DeckyPluginLoader`, `DeckyBackend`), loads the plugins, and draws
Remora's Quick Access tab, settings and store. The contract it implements is in
[../docs/compat/frontend.md](../docs/compat/frontend.md). The backend protocol is in
[../docs/PROTOCOL.md](../docs/PROTOCOL.md), and the Steam lookups with their fallbacks are in
[../docs/FRONTEND.md](../docs/FRONTEND.md).

## Build

```sh
cd frontend
npx --yes pnpm@10 install --frozen-lockfile
npx --yes pnpm@10 run build       # writes ../backend/remora/static/
npx --yes pnpm@10 run typecheck   # tsc --noEmit (strict)
npx --yes pnpm@10 test            # smoke test of the build against a fake Steam (Node + jsdom)
```

The build output (commit it with the backend):

| File | What |
|---|---|
| `index.js` | Entry point. The CDP bootstrap imports `http://127.0.0.1:1337/frontend/index.js` |
| `chunk-*.js` | The rest of Remora, loaded once Steam and @decky/ui are ready |
| `dfl.js` | `@decky/ui` from npm, unmodified, as its own ES module. It is also `window.DFL` |
| `LICENSE.decky-ui.txt` | The LGPL-2.1 text for `dfl.js` |
| `fallback.js` | The self-contained crash-loop screen (no imports) |
| `*.map` | Source maps |

`@decky/ui` stays a separate file, so it can be swapped for another build of the same major
version. Remora's own code imports it as `./dfl.js` (an external in `build.mjs`). In both
bundles, `react`, `react/jsx-runtime` and `react-dom` resolve to `src/shims/`, which read Steam's
instances from `window.SP_*` (the same mapping plugin builds use). Fixes for Steam lookups go in
Remora's code (`src/hooks/`, `src/boot/dfl-preflight.ts`), never into `dfl.js`.

## Boot

1. `index.ts`: wait for Steam's init flag, find React/ReactDOM/JSX in Steam's webpack, run the
   @decky/ui preflight, then `import('./dfl.js')` into `window.DFL`. None of this uses React or
   @decky/ui, which cannot be evaluated earlier.
2. `runtime/start.tsx`: replace a previous instance (`dismountAll`, `deinit`), check the
   lookups, connect the WebSocket, create `PluginLoader`, and register the pages.
3. `PluginLoader.init`: patch the error boundary, the error report filter, the toaster, the QAM
   tab and the router (each isolated), publish the init hook, load settings and status, wait for
   Game Mode and the SP window, then import every enabled plugin in parallel.

## Layout

```
src/
  index.ts              boot stage one
  fallback.ts           crash-loop screen (built on its own)
  globals.d.ts          window globals Remora reads and publishes
  shims/                react / jsx-runtime / react-dom -> window.SP_*
  boot/                 Steam init wait, boot-time webpack access, React globals, @decky/ui preflight
  hooks/                one module per fragile Steam lookup, plus status.ts (strategies + health)
                        and components.tsx (@decky/ui components with plain fallbacks)
  runtime/              backend WebSocket, token, plugin loader, settings, plugin status,
                        update checks, install prompts, QAM state, plugin ordering
  api/                  @decky/api init hook, legacy ServerAPI, fetchNoCors, file picker, tab CSS/JS
  ui/                   i18n/ (English strings), qam/ (Quick Access panel), settings/, store/,
                        modals/ (file picker), common/ (crash screens, toast, prompts, badges)
  steam/globals.ts      the only place Steam's own globals are typed loosely
test/smoke.mjs          boots the build in Node with fake Steam modules
```

## Translations

All UI text is in `src/ui/i18n/en.ts`. To add a language, copy it to `<lang>.ts` with the same
keys and register it in `src/ui/i18n/index.ts`. The language comes from `navigator.language`.

## Debugging

In the CEF console (`http://<device>:8080`, the SharedJSContext tab), `window.remora.hooks()`
lists every lookup's status. Turn on Settings → Developer → verbose logging for per-step logs.
Remora's log lines start with a blue `Remora` tag.
