# Decky Loader frontend runtime contract (compatibility spec)

Scope: what a clean-room, plugin-compatible replacement for Decky Loader must provide **inside Steam's
CEF JavaScript context** so that existing plugin bundles (both modern `@decky/api` ESM plugins and
legacy `decky-frontend-lib` IIFE plugins) keep working. Interfaces and behavior are described in our own
words; identifiers, message field names and signatures are quoted where they are part of the contract.

Reference snapshot (read-only, under `decky-ref/`):

| Repo | Version / commit |
|---|---|
| `decky-loader` | 67196ad (2026-10-17) |
| `loader-api` (`@decky/api`) | 1.1.3, d1b7f16 |
| `decky-frontend-lib` (`@decky/ui`) | 4.15.1, 5419bcb |
| `rollup/package` (`@decky/rollup`) | 1.0.2 (npm tarball) |
| `decky-plugin-template` | 90d0780 |
| `plugins/*` | 40 store plugins, used to check real-world usage |

All paths below are relative to `decky-ref/`. `DL` = `decky-loader`, `FE` = `decky-loader/frontend/src`,
`BE` = `decky-loader/backend/decky_loader`, `DFL` = `decky-frontend-lib/src`, `API` = `loader-api/src`.

Legend: **MUST** = plugins break without it; **SHOULD** = loader feature or edge case that some plugins use;
**NOTE** = quirk worth keeping bug-for-bug or fixing on purpose.

---

## 1. Getting into Steam (CEF remote debugging)

### 1.1 Enabling the debugger
- Steam opens a CEF DevTools HTTP/WebSocket server on `localhost:8080` when the flag file
  `<steam root>/.cef-enable-remote-debugging` exists (e.g. `~/.steam/steam/.cef-enable-remote-debugging`).
  The loader repo itself does not create it; the official installer does. Plugin-side evidence of the
  same convention: `plugins/SDH-CssLoader/css_utils.py:119-123`. The README says port 8080 (and 1337)
  must be free: `DL/README.md:49-51`.
- The DevTools base URL is hard-coded: `BE/injector.py:12` (`http://localhost:8080`).
- Optional LAN forwarding of the debugger is a systemd unit `steam-web-debug-portforward.service`
  (`BE/helpers.py:19`), started or stopped at startup based on the `cef_forward` setting
  (`BE/main.py:85-89`) and from the WS routes `utilities/allow_remote_debugging` /
  `disallow_remote_debugging` (`BE/utilities.py:292-298`). The error-boundary UI tells users the
  forwarded port is 8081 (`FE/components/DeckyErrorBoundary.tsx`, "CEF Debugger: http://<ip>:8081").

### 1.2 Finding the target
- Poll `GET http://localhost:8080/json` (3 s timeout). Connection refused: wait 5 s and retry;
  reset/timeout: wait 1 s and retry (`BE/injector.py:370-396`). Each entry gives `title`, `id`, `url`,
  `webSocketDebuggerUrl` (`BE/injector.py:16-29`).
- The target is the **GamepadUI shared JS context**: a tab whose URL contains
  `https://steamloopback.host/routes/` or `https://steamloopback.host/index.html` **and** whose title is one
  of `SharedJSContext`, `Steam Shared Context presented by Valve™`, `Steam`, `SP`
  (`BE/injector.py:413-425`). The loader's main loop retries every 5 s until it exists
  (`BE/main.py:185-206`).
- Legacy plugin helpers (`executeInTab`, `injectCssIntoTab`, `get_tab_id`) look tabs up by **exact
  title** instead (`BE/injector.py:399-404`, `427-430`). Common titles plugins pass: `SP`,
  `QuickAccess`, `MainMenu`, `SharedJSContext`.

### 1.3 What is evaluated and when
- After connecting to the target's WebSocket the loader sends `Page.enable` (so it receives
  `Page.domContentEventFired` and `Inspector.detached`), then injects (`BE/main.py:207-210`).
- Injection is one `Runtime.evaluate` (with `userGesture: true`, not awaited) of a small bootstrap
  (`BE/helpers.py:56-57`) whose behavior is:
  1. If `window.deckyHasLoaded` is already truthy: schedule `SteamClient.Browser.RestartJSContext()`
     after 100 ms (the context reloads clean and we inject again on the next DOMContentLoaded).
  2. Otherwise set `window.deckyHasLoaded = true` and dynamically `import()` the frontend entry
     `http://localhost:1337/frontend/<script>?v=<loader version>`; errors are only logged.
  `<script>` is `index.js`, or `fallback.js` once the webhelper crash counter reaches 3
  (`BE/main.py:252`).
- Re-injection triggers (`BE/main.py:211-235`):
  - `Page.domContentEventFired` and `window.deckyHasLoaded` is not set: inject again (Steam reloaded the context).
  - `Inspector.detached`: close the socket, go back to tab discovery.
  - Socket ends or errors: count a "webhelper crash" (`BE/main.py:103-118`): more than 4 crashes, each
    within 60 s of the previous, shut the loader down.
- First-connection guard (`BE/main.py:246-251`): on Linux, if the flag is **already** set when we
  first connect (a previous loader instance injected), the loader kills `steamwebhelper`
  (`killall -s SIGTERM steamwebhelper`, `BE/localplatform/localplatformlinux.py:232-236`) and waits
  for the fresh context, so two loader frontends never coexist.
- Crash-loop fallback: from 3 crashes on, `fallback.js` is injected instead of the loader, then the
  backend shuts down (`BE/main.py:252-256`). `fallback.js` is self-contained (no imports): it waits
  4 s, draws a "crash loop detected" overlay into the SP window, waits 10 s, and calls
  `SteamClient.User.StartShutdown(false)` (`FE/fallback.ts:1-128`). Guard global: `window.deckyFallbackLoaded`.
- A CEF socket workaround: on `SteamClient.User.RegisterForShutdownStart` the frontend calls WS route
  `utilities/close_cef_socket` (`FE/steamfixes/socket.ts:1-16`), which uses lsof+gdb to close Steam's
  stale 8080 listener (`BE/localplatform/localplatformlinux.py:304-340`, env `USE_CEF_CLOSE_WORKAROUND`).
  Optional for a replacement.
- Other CDP use (dev only): React DevTools injection uses `Debugger.setInstrumentationBreakpoint`
  `beforeScriptExecution` plus a reload, then evaluates the script many times (`BE/injector.py:136-201`,
  `BE/utilities.py:427-462`); guard global `window.deckyHasConnectedRDT`.

### 1.4 How the frontend bundle is served
- HTTP server on `127.0.0.1:1337` (env `SERVER_HOST`/`SERVER_PORT`, `BE/localplatform/localplatform.py:35-39`).
  The port and both host spellings (`127.0.0.1` and `localhost`) are baked into plugin bundles and
  loader code: **the replacement MUST listen on 127.0.0.1:1337 and answer on `localhost:1337` too**.
- `GET /frontend/{path}` serves the built loader frontend from `BE/static/` with
  `Cache-Control: no-cache` (`BE/loader.py:92`, `124-126`). The loader frontend is an ESM build
  (entry `index.js` plus `chunk-<hash>.js` lazy chunks, sourcemaps rewritten to
  `decky://decky/loader/...`), produced by `DL/frontend/rollup.config.js`.
- CORS is enabled for origin `https://steamloopback.host` with credentials and all headers
  (`BE/main.py:66-72`, `99-100`). The page origin is `https://steamloopback.host`; CEF lets it load
  `http://127.0.0.1` modules (loopback counts as a secure context).

### 1.5 Frontend boot sequence (`FE/index.ts:13-54`, `FE/start.tsx`)
1. Busy-wait (via `setTimeout(0)`) until `window.App.BFinishedInitBeforeLogin()` or, on older clients,
   `BFinishedInitStageOne()` returns true (`FE/index.ts:8-20`).
2. If `window.SP_REACT` is not already defined, create the React globals from Steam's webpack (section 2.1).
3. `window.DFL = await import('@decky/ui')` (the loader's own bundled copy).
4. `window.deckyAuthToken = fetch('http://127.0.0.1:1337/auth/token').text()`.
5. `window.DeckyBackend = new WSRouter(); await DeckyBackend.connect()`.
6. `import('./start')`: set up i18next (locales from `/locales/{lng}.json`), call
   `DeckyPluginLoader.dismountAll()` and `.deinit()` on any previous instance, then create and `init()`
   a new `PluginLoader` (`FE/start.tsx:16-48`).
7. The `PluginLoader` constructor (`FE/plugin-loader.tsx:90-161`) subscribes backend events, installs
   the QAM tab, registers `/decky/store` and `/decky/settings` routes, the Steam fixes and file-picker
   patches, publishes the plugin API init hook, then: user info + version, then `loadPlugins()`, then
   a support-status toast, then a plugin update check after 30 s.
8. `loadPlugins()` (`FE/plugin-loader.tsx:189-223`) waits for the first `SteamClient.UI.RegisterForUIModeChanged`
   callback; in GamePad mode (`EUIMode.GamePad`) it also waits until `findSP()` returns a window; then
   it fetches `loader/get_plugins` and imports every non-disabled plugin in parallel (no timeout and no
   queue at boot).

---

## 2. Globals the loader exposes

### 2.1 Required by plugin bundles (MUST)

| Global | Contents | Set at | Consumers |
|---|---|---|---|
| `window.SP_REACT` | Steam's own React module object (the instance Steam renders with). Found via webpack: the module having `Component`, `PureComponent` and `useLayoutEffect`. | `FE/index.ts:28` | every plugin: `react` import is rewritten to it; legacy bundles call `window.SP_REACT.createElement` directly (e.g. `plugins/NonSteamLaunchers/dist/index.js`, `plugins/decky-cloud-save/src/helpers/toast.ts:15`). |
| `window.SP_REACTDOM` | Steam's react-dom: module with `createPortal` and `createRoot`, or `createPortal` and `__DOM_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE`. | `FE/index.ts:29-31` | `react-dom` imports |
| `window.SP_JSX` | Object with `jsx`, `jsxs` and `Fragment`. Uses Steam's jsx-runtime module if it has both `jsx` and `jsxs`; if the module only has `jsx`, the loader builds an object with `jsxs` aliased to `jsx` and `Fragment` taken from `SP_REACT.Fragment`. | `FE/index.ts:34-43` | `react/jsx-runtime` imports (modern plugins built with the automatic JSX runtime) |
| `window.DFL` | The **module namespace of the loader's bundled `@decky/ui`** (4.15.1 here). Plugins do not bundle `@decky/ui`; every `import ... from '@decky/ui'` (or legacy `'decky-frontend-lib'`) becomes a property read on `DFL`. | `FE/index.ts:46` | every plugin |
| `window.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit` | `{ connect(version: number, pluginName: string) => BackendAPI }`, see 3.3. | `FE/plugin-loader.tsx:705-763` | the `@decky/api` copy bundled into each modern plugin, evaluated when the plugin module loads (`API/index.ts:25-40`). |

Rules:
- All of these must exist **before** any plugin module is evaluated. `@decky/api` throws at import
  time if the init hook is missing (`API/index.ts:27-31`).
- `SP_REACT` must be the same React instance that renders Steam's tree; hooks and context break with a copy.
- `DFL` must be backward compatible with the old `decky-frontend-lib` export names, because legacy IIFE
  bundles get the same `DFL` object (`plugins/SDH-CssLoader/rollup.config.js`: `"decky-frontend-lib": "DFL"`).
- The loader skips step 2 when `window.SP_REACT` is already defined (`FE/index.ts:24`), so a context
  where Steam (or an earlier loader) defined it is reused.

### 2.2 Loader-internal globals (SHOULD keep names; some plugins reach into them)

| Global | Meaning | Ref |
|---|---|---|
| `window.deckyHasLoaded` | injection guard (section 1.3) | `BE/helpers.py:57`, `FE/start.tsx:7-9` |
| `window.deckyAuthToken` | backend CSRF token string | `FE/index.ts:48` |
| `window.DeckyBackend` | the WSRouter: `call`, `callable`, `addEventListener`, `removeEventListener` | `FE/wsrouter.ts:5-7`, `FE/index.ts:50` |
| `window.DeckyPluginLoader` | the PluginLoader instance | `FE/start.tsx:7`, `47` |
| `window.__ROUTER_HOOK_INSTANCE`, `__TABS_HOOK_INSTANCE`, `__TOASTER_INSTANCE`, `__ERRORBOUNDARY_HOOK_INSTANCE` | singletons; a new instance calls `deinit()` on the old one first (safe re-injection) | `FE/router-hook.tsx:56-57`, `FE/tabs-hook.tsx:24-25`, `FE/toaster.tsx:28-29`, `FE/errorboundary-hook.tsx:23-24` |
| `window.deckyFallbackLoaded`, `window.deckyHasConnectedRDT` | guards (fallback, React DevTools) | `FE/fallback.ts:10-11`, `FE/developer.tsx:60` |

Real-world reach-ins: TabMaster reads `DeckyPluginLoader.pluginReloadQueue` (array of `{name}`)
(`plugins/TabMaster/src/lib/controllers/MicroSDeckInterop.ts:55`) and
`DeckyPluginLoader.routerHook.routerState._routePatches` (a `Map<path, Set<patch>>`)
(`plugins/TabMaster/src/utils/Utils.tsx:280`). To stay compatible, keep `DeckyPluginLoader` with
`routerHook`, `toaster`, `pluginReloadQueue`, and `routerHook.routerState._routePatches`.

### 2.3 Steam globals the runtime depends on (provided by Steam, not the loader)
`window.webpackChunksteamui` (webpack chunk array), `window.SteamClient` (`UI.RegisterForUIModeChanged`,
`Browser.RestartJSContext`, `User.StartRestart/StartShutdown/RegisterForShutdownStart`,
`Apps.PromptToChangeShortcut`), `window.App`, `window.NotificationStore`, `window.securitystore`
(`IsLockScreenActive`), `window.SteamUIStore` (`GetFocusedWindowInstance`), `window.settingsStore`,
`window.SystemNetworkStore`, `window.FocusNavController` / `window.GamepadNavTree`.

### 2.4 What `@decky/rollup` does to plugin code (`rollup/package/src/index.js:19-62`)
- Input `./src/index.tsx`; output **ESM** (`format: 'esm'`) to `dist/` with `exports: 'default'`;
  so `dist/index.js` is an ES module whose **default export is the `definePlugin(...)` result** (a
  zero-argument factory function).
- `external: ['react', 'react-dom', '@decky/ui']`, and `rollup-plugin-external-globals` rewrites
  imports to global reads:
  `react` → `SP_REACT`, `react/jsx-runtime` → `SP_JSX`, `react-dom` → `SP_REACTDOM`,
  `@decky/ui` → `DFL`, and `@decky/manifest` → an inline JSON literal of the plugin's `plugin.json`
  (`index.js:30-36`, manifest read at `index.js:74`). Named imports become property reads on the global
  (for example `useState` becomes `SP_REACT.useState`). Only the exact specifier `@decky/ui` is
  external: deep imports such as `@decky/ui/dist/globals/steam-client/App` (used by about 20 plugins,
  almost all type-only) get bundled.
- **`@decky/api` is NOT external**: each plugin bundles its own copy, which calls the init hook
  with `manifest.name` (the `plugin.json` name) when the module loads.
- `context: 'window'`; `process.env.NODE_ENV` replaced (default `production`).
- `rollup-plugin-import-assets` with `publicPath` `http://127.0.0.1:1337/plugins/<plugin.json name>/`:
  imported images and similar become absolute URLs into the backend (they land under `dist/assets/`
  and are served from `/plugins/<name>/assets/...`, see 4.4).
- Sourcemap sources rewritten to `decky://decky/plugin/<encodeURIComponent(name)>/...`; the error
  attribution uses this scheme (section 5.6).
- Third-party build setups follow the same contract, e.g. TabMaster builds with Vite/rolldown as ES
  with the same externals map (`plugins/TabMaster/vite.config.ts:58-90`).

Legacy builds (no `"type": "module"` in `package.json`, `decky-frontend-lib`), e.g.
`plugins/SDH-CssLoader/rollup.config.js`: output **IIFE** to `dist/index.js`, `exports: 'default'`,
with globals `react` → `SP_REACT`, `react-dom` → `SP_REACTDOM`, `decky-frontend-lib` → `DFL`. The file
is a single expression statement of the form `(function (deps...) { ...; return <default export>; })(DFL, SP_REACT)`,
so **evaluating it returns the default export** (`plugins/NonSteamLaunchers/dist/index.js`, last lines).
Legacy JSX is usually classic `createElement`.

### 2.5 How the loader imports a plugin (`FE/plugin-loader.tsx:421-589`)
- The backend picks the load type (`BE/plugin/plugin.py:28-38`, `BE/enums.py:7-9`):
  `ESMODULE_V1 = 1` if the plugin's `package.json` has `"type": "module"`, otherwise `LEGACY_EVAL_IIFE = 0`.
  `plugin.json` `api_version` is read but only passed to the Python side.
- **ESM (type 1)**: `import('http://127.0.0.1:1337/plugins/<name>/dist/index.js?t=<Date.now()>')`
  (`FE/plugin-loader.tsx:475`). The `t` query param defeats the module cache, so a reload re-evaluates
  the module (and its bundled `@decky/api` reconnects). `<name>` is the `plugin.json` name inserted
  without encoding (the browser percent-encodes spaces); the server must decode it before lookup.
  Then call `module.default()` with **no arguments** (`:486`).
- **Legacy (type 0)**: `fetch('http://127.0.0.1:1337/plugins/<name>/frontend_bundle')` with header
  `X-Decky-Auth` and credentials `include` (`:504-510`). The text gets
  `//# sourceURL=decky://decky/legacy_plugin/<encodeURIComponent(name)>/index.js` appended, goes through
  (direct) `eval`, and the resulting function is called with a **legacy ServerAPI** object (section 3.2) (`:518-521`).
- The returned object is spread and then **`name`, `version` and `loadType` are overwritten** with the
  backend's `plugin.json` name, `package.json` version and load type (`:488-493`, `:522-527`). The
  plugin's own `name` field is effectively ignored.
- Timeout: when called from the backend event, `timeoutMS` is 15000 (`BE/loader.py:195-196`). ESM
  races the import against a timer, legacy aborts the fetch. On timeout the error is re-thrown and no
  error entry is added (`:535`).
- Any other load failure: an error placeholder plugin (stack trace panel, error icon) is put in the list
  and a "failed to load" toast is shown (`:537-579`).
- Before importing, any loaded plugin of that name is unloaded (its `onDismount()` runs) (`:438`, `:414-419`).
- Reload queue: while one queued import runs (`reloadLock`), others go into `pluginReloadQueue` and run in order (`:428-457`).
- SP-crash check: if the SP window existed before a plugin loaded and is gone after, call
  `utilities/restart_webhelper` (`:584-588`); the same check runs 5 s after boot (`:172-180`).

---

## 3. Plugin module shape and plugin-facing APIs

### 3.1 Plugin object (returned by the factory)
Type: `API/types.ts:61-71`, `FE/plugin.ts:7-15`.

| Field | Type | Semantics |
|---|---|---|
| `name` | string | Ignored; replaced by the backend name (2.5). The QAM list and title bar show the backend name. |
| `icon` | JSX element | Icon in the QAM plugin list (`FE/components/PluginView.tsx`, list rows). |
| `content` | JSX element, optional | Panel shown when the plugin is opened. Plugins **without** `content` are left out of the QAM list (`PluginView.tsx:35`) but stay loaded (background-only plugins). |
| `titleView` | JSX element, optional | Replaces the default title (the plugin name) in the header next to the back button (`FE/components/TitleView.tsx:60`). |
| `alwaysRender` | boolean, optional | If true, `content` stays mounted while the QAM is hidden; otherwise it renders only while the QAM is visible (`PluginView.tsx:46`). |
| `onDismount()` | function, optional | Called on unload, reload, disable, uninstall, and on loader re-init (`dismountAll`) (`FE/plugin-loader.tsx:361-366`, `401-419`). |
| `title` (legacy) | JSX element | Old `decky-frontend-lib` field (e.g. `plugins/NonSteamLaunchers/dist/index.js` returns `title:`). **Ignored by the current loader** (nothing reads it). A replacement could fall back to it when `titleView` is missing (an improvement, not required). |

`definePlugin(fn)` (`API/index.ts:118-123`; deprecated copy in `DFL/index.ts:15-20`) just returns a
wrapper that forwards its arguments to `fn`. Modern plugins get no arguments; legacy plugins get the ServerAPI.

### 3.2 Legacy ServerAPI (type-0 plugins) (`FE/plugin-loader.tsx:788-815`)
Object passed as the only argument of a legacy plugin's factory:

| Member | Signature | Behavior |
|---|---|---|
| `routerHook` | RouterHook (3.4) | the shared loader instance |
| `toaster` | Toaster (3.5) | the shared loader instance |
| `callPluginMethod(method, kwargs = {})` | → `Promise<{success: boolean, result: any}>` | WS `loader/call_legacy_plugin_method` with `[pluginName, method, kwargs]`. The backend calls the Python method with **kwargs**. Never rejects for plugin errors: `success:false`, `result` = the exception string. Method names starting with `_` are refused (`BE/loader.py:211-222`). Used by about 100 call sites across the sample plugins. |
| `callServerMethod(name, args = {})` | → `Promise<{success, result}>` | WS `utilities/_call_legacy_utility` with `[name, args]`; dispatches to a fixed set: `ping`, `http_request`, `execute_in_tab`, `inject_css_into_tab`, `remove_css_from_tab`, `set_setting`, `get_setting`, `filepicker_ls`, `get_tab_id`, `get_user_info`, called with kwargs (`BE/utilities.py:39-50`, `111-121`). Logs a deprecation warning. |
| `fetchNoCors(url, request = {})` | → `Promise<{success, result: {status, headers, body: string}}>` | WS `utilities/http_request` with `[method, url, extra_opts]`. **NOTE**: the method defaults to **POST** when `request.method` is missing; `request.body` is renamed to `data`; other keys (e.g. `headers`) pass through as aiohttp request options (`FE/plugin-loader.tsx:684-703`, `BE/utilities.py:218-227`). On exception: `success:false`, `result` = error string. |
| `executeInTab(tab, runAsync, code)` | → `Promise<{success, result}>` | WS `utilities/execute_in_tab`; evaluates `code` in the CDP target titled `tab` (awaits a promise if `runAsync`); `result` is the CDP `result.value`, or the CDP result on exception (`BE/utilities.py:229-247`). |
| `injectCssIntoTab(tab, css)` | → `Promise<string>` | WS `utilities/inject_css_into_tab`; adds a `<style>` with a fresh UUID id to the head of that tab and resolves to the id. **NOTE**: the modern backend returns the bare id and rejects on failure; older loaders returned `{success, result}`. Plugins written for the old shape will read `.result` as undefined. |
| `removeCssFromTab(tab, cssId)` | → `Promise<void>` | WS `utilities/remove_css_from_tab` |
| `openFilePicker(startPath, selectFiles?, regex?)` | → `Promise<{path, realpath}>` | Legacy form: `selectFiles` true means FILE mode (files and folders shown); false means FOLDER mode (folders only) (`FE/plugin-loader.tsx:602-613`). |
| `openFilePickerV2(...)` | same as modern `openFilePicker` (3.3) | |

Legacy HTTP equivalent: `POST /methods/{method_name}` with a JSON kwargs body (needs the
`X-Decky-Auth` header) runs the same utility set, replying `{result, success}` (`BE/utilities.py:87-109`).

### 3.3 Modern backend API (init hook + `@decky/api` exports)

**Init hook contract** (`FE/plugin-loader.tsx:705-763`, `API/index.ts:10-44`):
- `connect(version, pluginName)` is called once each time a plugin module is evaluated, with `pluginName`
  = the bundled `plugin.json` name (it must equal the backend plugin name, since it routes calls and events).
- `@decky/api` 1.1.x asks for `version = 2`; if `connect` **throws**, it retries with 1. The current loader
  never throws: it warns for anything outside 1..2 and returns an object.
- Every `connect` call creates a **fresh event-listener map for that plugin**, replacing the previous one,
  so a reloaded plugin gets a clean listener set.
- Returned object fields (v1): `call`, `callable`, `addEventListener`, `removeEventListener`,
  `openFilePicker`, `executeInTab`, `fetchNoCors`, `getExternalResourceURL`, `injectCssIntoTab`,
  `removeCssFromTab`, `routerHook`, `toaster`, `_version: 1`. If version ≥ 2, also
  `useQuickAccessVisible`, and `_version: 2`. `@decky/api` warns when `_version` differs from what it asked for.

**`@decky/api` exports** (`API/index.ts:48-123`, types `API/types.ts`). Each is just re-exported from the object above:

| Export | Signature | Semantics |
|---|---|---|
| `call` | `(method: string, ...args) => Promise<T>` | WS `loader/call_plugin_method` with `[pluginName, method, ...args]`; the backend calls the plugin's Python method with **positional args** (`BE/loader.py:224-233`). Resolves to the raw return value; **rejects** with a PyError on a Python exception. Names starting with `_` are refused (rejects). |
| `callable` | `(method) => (...args) => Promise<T>` | curried `call` |
| `addEventListener` | `(event, listener) => listener` | Registers for backend `decky.emit(event, *args)` from **this plugin only**; the listener is called with the spread args. Returns the listener (handle for removal). Async listener errors are caught and logged. |
| `removeEventListener` | `(event, listener) => void` | |
| `routerHook` | RouterHook | 3.4 |
| `toaster` | Toaster | 3.5 |
| `openFilePicker` | `(select: FileSelectionType, startPath, includeFiles?, includeFolders?, filter?: RegExp or (file)=>boolean, extensions?: string[], showHiddenFiles?, allowAllFiles?, max?) => Promise<{path, realpath}>` | Opens the loader's file-picker modal; rejects with the string `'User canceled'` on cancel (`FE/plugin-loader.tsx:615-654`). `FileSelectionType`: `FILE = 0`, `FOLDER = 1` (a const enum, inlined into plugins as numbers). Listing comes from WS `utilities/filepicker_ls`. |
| `executeInTab` | `(tab, runAsync, code) => Promise<{success, result}>` | as legacy |
| `injectCssIntoTab` | typed `(tab, style) => string`, **actually returns `Promise<string>`** | as legacy |
| `removeCssFromTab` | `(tab, cssId) => Promise<void>` (typed void) | as legacy |
| `fetchNoCors` | `(url, init?: RequestInit & {excludedHeaders?: string[]}) => Promise<Response>` | A real `fetch` through the backend proxy: the URL is replaced by `getExternalResourceURL(url)`, every init header `K: V` is sent as `X-Decky-Header-K: V`, `excludedHeaders` goes as `X-Decky-Fetch-Excluded-Headers: a, b`, credentials `include`; method and body pass through (`FE/plugin-loader.tsx:661-682`). Returns a native `Response`, streamed and **not** decompressed by the proxy. |
| `getExternalResourceURL` | `(url) => string` | `http://127.0.0.1:1337/fetch?auth=<token>&fetch_url=<encodeURIComponent(url)>`; usable directly as `<img>/<audio>/<video>` src (`:657-659`). |
| `useQuickAccessVisible` | `() => boolean` (v2 only) | React hook: true while the QAM is visible. Backed by a context provided around each QAM tab panel (section 5.1). **It only works inside content rendered in the QAM tab**; elsewhere it returns the context default `false` (`FE/components/QuickAccessVisibleState.tsx:3-17`). |
| `definePlugin` | `(fn) => fn'` | 3.1 |
| types | `ToastData`, `ToastNotification`, `Toaster`, `RouterHook`, `RoutePatch`, `FilePickerRes`, `FileSelectionType`, `DeckyRequestInit`, `Plugin`, `DefinePluginFn` | `API/types.ts` |

Not part of `@decky/api` but exported by `@decky/ui`: a deprecated `useQuickAccessVisible` that watches
the QAM window's `document.visibilitychange` through the gamepad nav tree `QuickAccess-NA`
(`DFL/custom-hooks/useQuickAccessVisible.ts:5-65`). It ships inside `DFL`, so no loader support is needed.

### 3.4 RouterHook (shared by legacy and modern) (`FE/router-hook.tsx:328-350`, `FE/components/DeckyRouterState.tsx`)

| Method | Semantics |
|---|---|
| `addRoute(path, component, props = {})` | Adds a full-screen GamepadUI route. `component` is a component **type** (rendered with no props), wrapped in Steam's `ErrorBoundary`, inside Steam's own `Route` element with `path` plus `props` (e.g. `exact: true`). Same path replaces the old entry. Pages are opened with `DFL.Navigation.Navigate(path)` (usually followed by `Navigation.CloseSideMenus()`). |
| `removeRoute(path)` | |
| `addPatch(path, patch) => patch` | `patch(routeProps) => routeProps` is called on each render of the existing Steam route with that path (e.g. `/library/app/:appid`), receiving the Route's props with `children` replaced by a cloned, re-typed element; the returned props' `children` becomes the rendered child. Several patches per path chain in insertion order. Returns the patch as the removal handle. Applies to the main route list and to the in-game list (`/appoverlay`, `/apprunning`). |
| `removePatch(path, patch)` | Restores the original children on the next render. |
| `addGlobalComponent(name, Component)` | Renders `<Component/>` once, as a sibling of the router, always mounted (overlays, listeners). Keyed by name. |
| `removeGlobalComponent(name)` | |

Plus loader-internal `waitForUnlock()` (polls `securitystore.IsLockScreenActive()` every 500 ms) and `routes`, `routerState`.
State changes re-render through a React context provider (event bus), so add/remove works at any time.

### 3.5 Toaster (`FE/toaster.tsx:48-120`, types `API/types.ts:15-45`)
`toast(data: ToastData) => ToastNotification`.

ToastData fields: `title`, `body`, `subtext?`, `logo?`, `icon?` (ReactNodes); `timestamp?` (Date, default
now); `onClick?`; `className?`, `contentClassName?`; `duration?` (ms on screen, default 5000);
`expiration?` (ms until removed from the tray); `critical?`; `eType?` (default 31); `sound?` (default 6);
`showNewIndicator?` (default true); `playSound?` (default true); `showToast?` (default true). Defaults are
**written back into the caller's object**.

Return value: `{ data, dismiss() }`; `dismiss` removes the toast's group from Steam's tray.
Behavior: posts a notification through `NotificationStore.ProcessNotification(info, toastData, 0)`
with a synthetic id from `NotificationStore.m_nNextTestNotificationID++`, `eSource 1`, payload `data` =
the ToastData, marker `decky: true`, and a `fnTray` that puts a group `{eType, notifications:[toast]}` at
the top of the tray. Valve's toast renderer is patched so groups marked `decky` render Decky's own toast
component (popup and tray layouts) inside an `ErrorBoundary` (`FE/toaster.tsx:31-43`, `FE/components/Toast.tsx`).

---

## 4. Frontend ↔ backend transport

### 4.1 Auth
- `GET /auth/token` returns the token (a random UUID v4 created when the backend process starts) as
  `text/plain` (`BE/main.py:97`, `174-175`, `BE/helpers.py:22`).
- HTTP middleware (`BE/helpers.py:37-54`): requests need `X-Decky-Auth: <token>` unless they are
  `OPTIONS`, `/auth/token`, or match `/plugins/load_main/*`, `/static/*`, `/steam_resource/*`,
  `/frontend/*`, `/fetch`, `/ws`, `/plugins/*/assets/*`, `/plugins/*/data/*` or `/plugins/*/dist/*`.
  Rejected requests get `403 Forbidden`. `/ws` and `/fetch` take the token as the `auth` query
  parameter instead (`BE/wsrouter.py:81-83`, `BE/utilities.py:144-146`).
- The token changes on every backend restart; the frontend fetches it once at boot. After a backend
  restart the WS reconnect fails until Steam reloads the context. **NOTE**: a replacement could
  re-fetch the token on reconnect.

### 4.2 WebSocket `ws://127.0.0.1:1337/ws?auth=<token>` (`FE/wsrouter.ts`, `BE/wsrouter.py`)
Plain JSON text frames. Message `type` values (`FE/wsrouter.ts:9-21`, `BE/wsrouter.py:14-25`):

| type | Name | Direction | Fields |
|---|---|---|---|
| -1 | ERROR | BE→FE | `id`, `error: {name, message, traceback}` (`BE/wsrouter.py:75-76`); unknown route: `error: {error, name: "RouteNotFoundError", traceback: null}` (`:146`) |
| 0 | CALL | FE→BE | `id` (int, counting up from 1 per page load), `route` (string), `args` (array) |
| 1 | REPLY | BE→FE | `id`, `result` |
| 2 | DISCARD | BE→FE | `id` (the plugin was stopped mid-call; the frontend **never settles** that promise) |
| 3 | RECEIVED_RESPONSE | FE→BE | `id`; ack after any REPLY, ERROR or DISCARD, so the backend can drop its cached reply |
| 4 | FULL_SYNC | FE→BE | `messages: CALL[]`; sent on every (re)connect with all unresolved calls |
| 5 | EVENT | BE→FE | `event` (string), `args` (array, spread into listeners) |

Semantics:
- The backend runs each CALL as a task and caches the reply per `id` until acked. A CALL whose `id`
  is already cached (resent during FULL_SYNC) gets the cached reply resent instead of running again.
  During FULL_SYNC, cached ids that the frontend did not list are dropped (`BE/wsrouter.py:127-162`).
- The backend keeps only one socket: a new connection closes the old one (`BE/wsrouter.py:89-96`).
  The text frame `close` ends the session.
- The frontend sends only while the socket is OPEN (otherwise drops; FULL_SYNC repairs it). On close
  it waits 5 s and reconnects (`FE/wsrouter.ts:230-244`).
- On ERROR the frontend rejects with `PyError`: `name = "Python " + error.name`, Python traceback
  appended to `stack`, `pythonTraceback` property. **NOTE**: the frontend reads the message from
  `error.error` while the backend sends `error.message`, so the JS message is empty for plugin
  exceptions (`FE/wsrouter.ts:51-73`, `164-167` vs `BE/wsrouter.py:75`). A replacement SHOULD fill both fields.
- `DeckyBackend.addEventListener/removeEventListener` work per event name, with a listener set per event (`FE/wsrouter.ts:131-148`).
- Plugin events: Python `decky.emit(event, *args)` reaches the frontend as EVENT `loader/plugin_event`
  with `args = [{plugin, event, args: [...]}]` (`BE/loader.py:168-170`, `BE/plugin/sandboxed_plugin.py:99-104`).
  The frontend sends it to that plugin's listener map from `connect()` and spreads `args`
  (`FE/plugin-loader.tsx:765-786`).

**WS routes the frontend calls** (backend registrations: `BE/loader.py:100-103`, `BE/utilities.py:59-85`, `BE/updater.py:55-61`):
- Plugin-facing: `loader/call_plugin_method [name, method, ...args]`,
  `loader/call_legacy_plugin_method [name, method, kwargs]`, `utilities/execute_in_tab [tab, runAsync, code]`,
  `utilities/inject_css_into_tab [tab, css]`, `utilities/remove_css_from_tab [tab, id]`,
  `utilities/http_request [method, url, opts]`, `utilities/_call_legacy_utility [name, kwargs]`,
  `utilities/filepicker_ls [path, include_files, include_folders, include_ext, include_hidden, order_by, filter_for, page, max]`
  (returns `{realpath, files:[{isdir, name, realpath, size, modified, created}], total}`).
- Loader UI: `loader/get_plugins` (returns `[{name, version, load_type, disabled}]`, in backend dict
  order), `loader/reload_plugin [name]`, `utilities/settings/get [key, default]`,
  `utilities/settings/set [key, value]`, `utilities/get_user_info` (returns `{username, path}`),
  `utilities/install_plugin(s)`, `confirm_plugin_install`, `cancel_plugin_install`, `uninstall_plugin`,
  `enable_plugin`, `disable_plugin`, `set_all_plugins_disabled`, `restart_webhelper`, `close_cef_socket`,
  `enable_rdt`/`disable_rdt`, `allow_remote_debugging`, `start_ssh`, `ping`, `get_tab_id`, `updater/*`.

**Events the frontend handles** (`FE/plugin-loader.tsx:93-105`):
`loader/import_plugin (name, version, load_type, useQueue=true, timeoutMs=15000)` → `importPlugin`;
`loader/unload_plugin (name)`; `loader/disable_plugin (name)`; `loader/plugin_event ({plugin,event,args})`;
`loader/notify_updates`; `loader/add_plugin_install_prompt (artifact, version, request_id, hash, install_type)`;
`loader/add_multiple_plugins_install_prompt (request_id, requests)`; `updater/update_download_percentage`,
`updater/finish_download`; plus store download progress events `loader/plugin_download_*` (`BE/browser.py:149-319`).

### 4.3 HTTP endpoints used by the frontend and plugins

| Endpoint | Auth | Purpose | Ref |
|---|---|---|---|
| `GET /auth/token` | none | token | `BE/main.py:97` |
| `GET /frontend/{path}` | none | loader bundle, no-cache | `BE/loader.py:92`, `124-126` |
| `GET /locales/{lang}.json` | header (i18next sends it) | loader i18n; unknown language returns `{}` | `BE/loader.py:93`, `128-135`, `FE/start.tsx:31-39` |
| `GET /plugins/{name}/dist/{path}` | none | ESM entry and chunks (`dist/index.js`, lazy chunks, sourcemaps), no-cache | `BE/loader.py:95`, `141-145` |
| `GET /plugins/{name}/frontend_bundle` | header | legacy bundle text, `application/javascript` | `BE/loader.py:94`, `160-164` |
| `GET /plugins/{name}/assets/{path}` | none | files from `<plugin>/dist/assets/` (import-assets output) | `BE/loader.py:96`, `147-151` |
| `GET /plugins/{name}/data/{path}` | none | files from `<homebrew>/data/<plugin dir>/` | `BE/loader.py:97`, `153-158` |
| `GET/POST/PUT/DELETE/PATCH/HEAD /fetch?auth=&fetch_url=` | query param | streaming CORS proxy: drops `Host`, `Origin`, `Sec-Fetch-Site/Mode/Dest`, drops anything named in `X-Decky-Fetch-Excluded-Headers`, turns `X-Decky-Header-*` into the real headers, removes other `X-Decky-*`, sets `User-Agent: Decky/<ver> (https://decky.xyz)`, forwards the body, no auto-decompress, keeps upstream status and headers (minus CORS headers, which the server's CORS layer re-adds) | `BE/utilities.py:91-92`, `142-216`, `31-34` |
| `POST /methods/{name}` | header | legacy utilities | `BE/utilities.py:87-109` |
| `GET /static/*` | none | backend static dir | `BE/main.py:101` |

`{name}` in `/plugins/{name}/...` is the **plugin.json `name`** (backend dict key), not the directory
(`BE/loader.py:142`). Plugins build URLs like `http://127.0.0.1:1337/plugins/<name>/...` by hand (21
occurrences in the sample plugins).

---

## 5. UI integration points

### 5.1 QAM tab injection (`FE/tabs-hook.tsx`)
- Find the webpack module exporting memo-wrapped components whose `type` source contains
  `QuickAccessMenuBrowserView` (and optionally `QuickAccessMenuEmbedded`) (`:44-50`). After-patch their
  `type` with a React tree patcher that goes to the node with `props.onFocusNavDeactivated`, then to the
  element with a `props.tabs` array, and **pushes Decky's tab objects into that array** on every render
  (`:52-63`, `:99-128`).
- Tab object fields Steam accepts: `key` (= `QuickAccessTab.Decky` = 999, `DFL/modules/Router.ts:11-21`),
  `title` (null), `tab` (icon element), `panel` (content element), plus Decky markers `decky: true`,
  `initialVisibility`. The panel is `<ErrorBoundary><QuickAccessVisibleStateProvider>…`.
- The QAM `visible` render argument (`args[0].visible`) drives `useQuickAccessVisible`: on later renders
  the existing tabs' `qAMVisibilitySetter(visible)` is called (`:100-111`).
- An already mounted QAM fiber has its `type` (and `alternate.type`) swapped so the patch applies at once (`:65-81`).
- Decky's panel content (`FE/components/PluginView.tsx`): header (`TitleView`) with store and settings
  buttons, then one `ButtonItem` per plugin (icon, name, update badge). Opening a plugin shows `TitleView`
  (back button + `titleView` or name) and its `content` in an `ErrorBoundary`; the B button
  (`onCancelButton`) goes back. Other code can open the QAM on this tab with
  `Navigation.OpenQuickAccessMenu(QuickAccessTab.Decky)`.

### 5.2 Router patch (`FE/router-hook.tsx`)
- Find Steam's `Route` component: in the webpack module exporting the string `'router-backstack'`
  (min 20 exports), take the function whose source matches `routePath:<x>.match?.path.` or the older
  `routePath:null===(…match)` form (`:59-73`).
- On the first `SteamClient.UI.RegisterForUIModeChanged` callback with GamePad mode (`:75-94`): from
  the React root of `document.getElementById('root')`, find the fiber whose `type` source contains
  `Settings.Root()` and has no `pendingProps.loggedIn` (`:97-104`; retried every 5 s, waiting while the lock
  screen is up). After-patch its `elementType.type`, swap the live fiber's `type` and `alternate.type`, then
  force a re-render through the nearest patched ErrorBoundary's `_deckyForceRerender` (`:113-126`).
- The patched render wraps the output in the route wrapper plus a global-components host, marked
  `_decky` so it is not wrapped twice (`:207-225`). The wrapper expects `children.props.children[0].props.children`
  (main route list) and `[1].props.children` (in-game list: `/appoverlay`, `/apprunning`) (`:236-252`).
  It **appends one array of Decky `Route` elements** at the end of the main list (recreated when the
  route count changes) and applies route patches to both lists (`:277-326`).
- Desktop-mode routing is not done (`:85-88`, `:131-174` unused): plugins work in GamePad UI only.

### 5.3 Toaster
See 3.5. Fragile lookup: Valve's toast renderer is the export whose source contains
`controller:"notification",method:` (`FE/toaster.tsx:31`), wrapped with `injectFCTrampoline`
(`DFL/utils/react/fc.ts:30`) and replace-patched.

### 5.4 Plugin list, ordering, hidden, frozen, disabled
All stored as backend settings (`utilities/settings/get|set`, `FE/utils/settings.ts`):
- `pluginOrder: string[]`: QAM list order by index of the name; created by the backend on first run
  in load order (`BE/main.py:181-183`). If `sortPlugins: boolean` is true, sort by name instead
  (case-insensitive) (`FE/components/PluginView.tsx:28-37`). The settings page reorders and saves
  `pluginOrder` (`FE/components/settings/pages/plugin_list/index.tsx:178-225`).
- `hiddenPlugins: string[]`: loaded but left out of the QAM list; a "N hidden" note is shown
  (`FE/hidden-plugins-service.tsx`, `PluginView.tsx:36`).
- `frozenPlugins: string[]`: left out of store update checks and update badges (`FE/frozen-plugins-service.tsx`,
  `FE/plugin-loader.tsx:277-285`).
- `disabled_plugins` (backend-owned): Python not started; reported as `disabled: true` by
  `loader/get_plugins`, not imported on the frontend, "N disabled" note (`BE/loader.py:173-176`,
  `FE/plugin-loader.tsx:209-217`). Live disable sends `loader/disable_plugin` (`BE/utilities.py:491`).
- Per-plugin settings rows: reload (`loader/reload_plugin`, Python restart, then the frontend re-import
  through `loader/import_plugin`), hide/show, freeze, uninstall, disable.

### 5.5 Error handling
- **Global ErrorBoundary patch** (`FE/errorboundary-hook.tsx:76-106`): Steam's `ErrorBoundary` class
  (found via `DFL/components/ErrorBoundary.ts:4-6`: prototype has `Reset` and `componentDidCatch`, and
  the `render` source contains `lastErrorKey`) gets its `render` replace-patched. On error it renders the
  boundary's `fallback` prop if given, else Decky's crash screen (`FE/components/DeckyErrorBoundary.tsx`):
  likely source, Retry (`this.Reset()`), restart Steam or Decky, disable or uninstall the suspected plugin,
  disable all plugins, update, disable Decky until reboot, enable CEF forwarding and SSH. It also adds
  `_deckyForceRerender()` to the prototype, which the router patch uses.
- **Valve error reporting filter** (`FE/errorboundary-hook.tsx:27-69`): find the error-reporting-store
  factory by a regex over the export's source (`:27`), and replace-patch its prototype's `BIsBlacklisted`
  so errors from plugins or Decky are not reported to Valve (and reporting pauses for 30 s after one).
- **Source attribution** (`FE/utils/errors.ts:3-51`): a plugin is to blame if its stack contains
  `http://(localhost|127.0.0.1):1337/plugins/<name>/`, `decky://decky/plugin/<name>/` or
  `decky://decky/legacy_plugin/<name>/index.js`; any other `:1337/` URL means "the Decky frontend".
  **A replacement MUST keep these URL and sourcemap schemes** if it wants the same attribution (they come
  from the plugin build, see 2.4).
- Every Decky-rendered plugin surface is wrapped in Steam's `ErrorBoundary`: QAM tab panel, plugin
  content, each `addRoute` component, each toast. Global components are **not** wrapped (`FE/router-hook.tsx:227-234`).
- Event listener exceptions are caught and logged (WS and plugin events). Plugin load exceptions become
  an error panel plus a toast (2.5).

### 5.6 `@decky/ui` at runtime vs bundled
- **The loader provides `DFL`** (its own bundled `@decky/ui`, 4.15.1 here) as `window.DFL`. Plugins do
  not bundle it (external in `@decky/rollup`; `globals` in legacy builds). So the loader's `@decky/ui`
  version decides component and finder behavior for **all** plugins, and the loader must ship the
  newest DFL with all old export names (legacy `decky-frontend-lib` names included).
- `DFL` keeps one webpack module cache, built when it is first imported: it pushes a fake chunk into
  `window.webpackChunksteamui` to get webpack's `require`, then requires every module id
  (`DFL/webpack.ts:21-57`). Everything else (components, class maps, Navigation, `findSP`,
  patch helpers) is computed from that cache **when the module is evaluated**, so DFL must be imported
  after Steam has initialized (the boot wait in 1.5).
- Loaded and used by the loader itself: `findModule*`, `afterPatch`/`replacePatch`/`callOriginal`,
  `createReactTreePatcher`, `findInReactTree`/`findInTree`, `getReactRoot`, `injectFCTrampoline`,
  `ErrorBoundary`, `QuickAccessTab`, `EUIMode`, `findSP`, `showModal`/`ModalRoot`, `Navigation`,
  `sleep`, class maps (`quickAccessMenuClasses`, `staticClasses`, `findClassModule`), plus UI components
  for its own pages.
- Things plugins use only through `DFL` (must be present): UI components (`PanelSection`,
  `PanelSectionRow`, `ButtonItem`, `ToggleField`, `SliderField`, `Dropdown`, `Focusable`, `DialogButton`,
  `Menu`, `ConfirmModal`, `showModal`, `showContextMenu`, `Tabs`, `SidebarNavigation`...), `staticClasses`
  and the other class maps, `Navigation`/`Router`, `findModule*`, patch helpers, React tree helpers,
  `findSP`, `useParams`, `ReorderableList`, `ColorPickerModal`, `SuspensefulImage`, and the deprecated
  `definePlugin` and `useQuickAccessVisible`.
- Deep-imported `@decky/ui/dist/...` files are bundled into the plugin (2.4). If one has runtime code,
  it builds its **own** second webpack cache. Harmless, but slower.

### 5.7 Other loader patches (not part of the plugin contract)
- File-picker patch: replaces `SteamClient.Apps.PromptToChangeShortcut` with Decky's picker and
  re-applies it on history changes (`FE/components/modals/filepicker/patches/library.ts:10-55`).
- Developer mode: Valve-internal menu via the `settingsStore` mobx observer, and React DevTools (`FE/developer.tsx`).
- Built-in routes `/decky/store`, `/decky/settings` (`FE/plugin-loader.tsx:130-145`). Plugins open them
  with `Navigation.Navigate('/decky/settings/plugins')` and similar.

---

## 6. Fragile parts (break when Steam updates)

| # | What | How it is found | Where |
|---|---|---|---|
| 1 | Webpack bootstrap | `window.webpackChunksteamui.push([[Symbol], {}, r => …])` to get `require`, then require **every** module | `DFL/webpack.ts:21-57` |
| 2 | React, ReactDOM, JSX runtime | module shapes (`Component`+`PureComponent`+`useLayoutEffect`; `createPortal`+`createRoot` or the DOM-internals key; `jsx`+`jsxs` or a lone `jsx`) | `FE/index.ts:28-43` |
| 3 | Steam init readiness | `App.BFinishedInitBeforeLogin` / `BFinishedInitStageOne` | `FE/index.ts:8-11` |
| 4 | SP window | nav tree ids `GamepadUI_Full_Root`, `root_1_`, `GamepadUI_VR_Full_Root`, or `document.title == 'SP'`; through `GamepadNavTree.m_context.m_controller` or `FocusNavController` | `DFL/utils/index.ts:23-48` |
| 5 | QAM tab list | module whose export `type` source contains `QuickAccessMenuBrowserView` / `QuickAccessMenuEmbedded`; tree steps `props.onFocusNavDeactivated` then `props.tabs`; render arg `visible` | `FE/tabs-hook.tsx:44-63`, `99-128` |
| 6 | Router | `Route` from the `'router-backstack'` module by a source regex; root fiber whose type source contains `Settings.Root()`; children index layout `[0]` main, `[1]` in-game | `FE/router-hook.tsx:59-73`, `97-126`, `236-252` |
| 7 | Toasts | export whose source contains `controller:"notification",method:`; `NotificationStore.ProcessNotification`, `m_nNextTestNotificationID`, `RemoveGroupFromTray`; default `eType` 31 and sound 6 | `FE/toaster.tsx:31-43`, `48-120` |
| 8 | ErrorBoundary | prototype `Reset` + `componentDidCatch` + `render` source contains `lastErrorKey`; state fields `error`, `lastErrorKey`, prop `errorKey` | `DFL/components/ErrorBoundary.ts:4-6`, `FE/errorboundary-hook.tsx:76-101` |
| 9 | Error reporting store | regex over the factory's source; prototype method `BIsBlacklisted` | `FE/errorboundary-hook.tsx:27-69` |
| 10 | Navigation | `Router` = export with `Navigate` and `NavigationManager`; `SteamUIStore.GetFocusedWindowInstance()`, `WindowStore.GamepadUIMainWindowInstance`, `MenuStore` methods | `DFL/modules/Router.ts:75`, `95-145` |
| 11 | Modals | `showModalRaw` = function whose source contains `props.bDisableBackgroundDismiss` and has no `prototype.Cancel` | `DFL/components/Modal.ts:40-60` |
| 12 | CSS class maps | plain-object modules whose values are all strings; picked by key presence (`Title`+`QuickAccessMenu` for `quickAccessMenuClasses`/`staticClasses`, `ShortTemplate` for toasts, and so on) | `DFL/class-mapper.ts:7-32`, `DFL/utils/static-classes.ts:1067-1095`, `FE/components/Toast.tsx:26` |
| 13 | Other DFL components | about 17 component files located by export-source heuristics (Focusable, Field, Menu, Tabs, Carousel, ProgressBar...) | `DFL/components/*.ts` |
| 14 | Lock screen and history | `window.securitystore.IsLockScreenActive`; `m_history` export for re-patching | `FE/router-hook.tsx:176-184`, `FE/components/modals/filepicker/patches/library.ts:42` |
| 15 | CDP target naming | tab titles `SharedJSContext` and the other names; steamloopback URLs | `BE/injector.py:413-425` |

Design advice for the replacement: keep these lookups inside `DFL` and our loader core, each in its own
"finder" with a logged failure and degraded behavior (e.g. no QAM tab, but routes and toasts still work),
the way the reference already degrades (`FE/errorboundary-hook.tsx:32-35`, `71-74`, `FE/router-hook.tsx:68-73`).

---

## 7. Minimum compatibility checklist
1. Inject into the GamepadUI SharedJSContext through CDP on :8080 and re-inject on context reload
   (`deckyHasLoaded` guard, `RestartJSContext` on double-inject).
2. Serve on 127.0.0.1:1337 (and `localhost`): `/auth/token`, `/ws`, `/fetch`, `/plugins/{name}/dist|assets|data|frontend_bundle`, `/frontend/*`.
3. Before loading plugins: `SP_REACT`, `SP_REACTDOM`, `SP_JSX`, a complete `DFL`, and the init-hook
   global with a `connect(1|2, name)` that does not throw.
4. Load type by `package.json` `"type":"module"`: ESM `import(...?t=now)` then `default()`; legacy fetch,
   eval with the `decky://decky/legacy_plugin/` sourceURL, then call it with the ServerAPI object.
5. WS protocol types -1..5 with ids, FULL_SYNC replay, and EVENT `loader/plugin_event` fan-out per plugin.
6. Plugin object handling: backend name wins; `icon`/`content`/`titleView`/`alwaysRender`/`onDismount`.
7. QAM tab (key 999) with a visibility context, router `addRoute`/`addPatch`/`addGlobalComponent`, toaster.
8. ErrorBoundary wrapping and plugin attribution by the URL schemes.
9. Keep `window.DeckyPluginLoader` with `routerHook`, `toaster`, `pluginReloadQueue`, and
   `routerHook.routerState._routePatches` for plugins that reach in (TabMaster).
