# Remora frontend: Steam lookups

The frontend (`frontend/`, built into `backend/remora/static/`) has to find parts of Steam's UI at
runtime, because Valve ships no API for them. Each of these lookups lives in its own module under
`frontend/src/boot/` or `frontend/src/hooks/`. Each one tries an ordered list of strategies, logs the
one that matched, and reports a status (`ok`, `degraded` = a fallback strategy matched, `failed`,
`pending`). **Settings → Steam hooks** shows every status, and so does `window.remora.hooks()` in
the CEF console. When a lookup fails, only the feature listed for it turns off.

Spec references (`§6 #n`) point to the fragile-parts table in
[compat/frontend.md](compat/frontend.md).

| Status id | What | Strategies, in order | When it fails |
|---|---|---|---|
| `steam-init` (§6 #3) | Steam finished initialising | `App.BFinishedInitBeforeLogin()`, `App.BFinishedInitStageOne()`; after 60 s, start anyway if the webpack chunk array exists | Nothing turns off. Boot waits up to 60 s, then continues (`degraded`) |
| `webpack` (§6 #1) | Steam's webpack `require` | push a fake chunk into `webpackChunksteamui`; any other `window.webpackChunk*` array | Everything: React, @decky/ui and plugins need it |
| `react`, `react-dom`, `jsx-runtime` (§6 #2) | `SP_REACT`, `SP_REACTDOM`, `SP_JSX` | Reuse an existing global, else module shape: React `Component+PureComponent+useLayoutEffect`, then `createElement+useState+Fragment`. ReactDOM `createPortal+createRoot`, then the DOM-internals key, then `createPortal+flushSync`. JSX: a module with `jsx+jsxs`, then a lone `jsx` module (with `jsxs = jsx`), then a wrapper over `createElement` | No React: Remora stops (nothing can render). No ReactDOM: a stub with `version`, so plugin portals fail but the rest runs. JSX always resolves (the last strategy cannot fail) |
| `dfl-preflight` | What @decky/ui needs while it loads | Checks the common UI module, the `.PanelSection` module and the react-router module, which @decky/ui reads with `Object.values()` at load time. For any that is missing it registers a small stand-in webpack module, so @decky/ui still loads | With stand-ins, the @decky/ui components that came from those modules are plain (PanelSection) or undefined, but @decky/ui and plugins load. It fails only when React has neither 18 nor 19 internals, and then @decky/ui cannot load |
| `sp-window` (§6 #4) | The Game Mode (SP) window | @decky/ui `findSP()`; a nav tree whose id is `GamepadUI_Full_Root`, `root_1_` or `GamepadUI_VR_Full_Root` (or contains `GamepadUI`); `document.title == 'SP'` | Plugins load after 30 s without it, and the "SP vanished after a plugin loaded" crash check is off |
| `ui-mode` | Game Mode vs desktop | `SteamClient.UI.RegisterForUIModeChanged`; after 10 s, assume Game Mode | Plugins load and the router patch starts after 10 s |
| `navigation` (§6 #10) | Opening pages and the QAM from code | @decky/ui `Navigation` (needs its `Router` module); the focused window instance from `SteamUIStore` (`Navigate`, `NavigateBack`, `MenuStore.*`) | The QAM store and settings buttons do nothing. Plugins that call `Navigation` directly are affected the same way |
| `modals` (§6 #11) | Steam's modal dialogs | @decky/ui `showModal`, `ConfirmModal`, `ModalRoot` | Install and root prompts show inline (QAM panel and store page) instead of in a dialog. The file picker rejects with an error |
| `class-maps` (§6 #12) | CSS class maps | @decky/ui maps, checked by key (`Title`, `Field`, the `ShortTemplate` toast classes) | Remora's pages and toasts look plainer. Missing names become empty strings |
| `dfl-components` (§6 #13) | @decky/ui components Remora's pages use | The @decky/ui export; if it is undefined, a plain-element fallback component (`hooks/components.tsx`) | Remora's pages lose Steam's look but keep working. The plugin order list uses up/down buttons when `Field`/`Focusable` are missing |
| `error-boundary` (§6 #8) | Steam's `ErrorBoundary` class | @decky/ui `ErrorBoundary`; prototype `Reset`+`componentDidCatch`, render source has `lastErrorKey`; prototype `Reset`+`componentDidCatch` | Steam crashes show Valve's screen, not Remora's (which blames the right plugin), and the router patch cannot force its re-render (next row). Plugin surfaces are not affected: they use Remora's own boundary, which needs no lookup |
| `error-reporting` (§6 #9) | Valve's error report filter | An export whose prototype has `BIsBlacklisted`; an instance with `BIsBlacklisted` (its prototype is patched) | Plugin errors may reach Valve's crash reports |
| `qam-tab` (§6 #5) | The Quick Access tab list | Memo exports whose type source contains `QuickAccessMenuBrowserView`/`QuickAccessMenuEmbedded`; then source with `QuickAccessMenu` + `visible`. Inside the render: a node with `props.tabs` directly, else the `onFocusNavDeactivated` node's render (tree patcher). An already mounted QAM fiber is swapped to the patched type | No Remora tab, so plugin panels are not reachable. Routes, toasts, route patches and global components still work, and the store and settings stay reachable at `/decky/store` and `/decky/settings` (for example from a plugin page). `degraded` after 5 QAM renders with no tab list found |
| `router-route` (§6 #6) | Steam's `Route` component | The export of the `'router-backstack'` module whose source matches `routePath:<x>.match?.path.` or `routePath:null===(`; the same regex in any module; at render time, the `type` of an existing route element in the route list | Plugin pages (`addRoute`) and Remora's settings and store pages. Route patches and global components still work |
| `router-root` (§6 #6) | The component that renders the route lists | After Game Mode is reported and the lock screen is gone: a fiber whose type source contains `Settings.Root()` (and no `pendingProps.loggedIn`); the nearest memo-wrapped ancestor of the `/library` route fiber. Retried every 5 s (`failed` after 30 s, still retrying). Re-render: through the patched ErrorBoundary (remount with a new key); else by defeating the memo bail-out and calling `forceUpdate` on the nearest class parent; else it waits for Steam's next re-render | Plugin pages, route patches (`addPatch`) and global components |
| `router-layout` (§6 #6) | Where the route lists are in the root's output | `children[0].props.children` (main) and `children[1].props.children` (in-game, if it holds routes); a structural search for arrays with 2+ elements that have `props.path` (first = main) | Plugin pages and route patches. The output is passed through unchanged |
| `toast-renderer` (§6 #7) | Valve's notification renderer | A function, then a memo, whose source contains `controller:"notification",method:`, patched through @decky/ui's `injectFCTrampoline` | Toasts are off: `toaster.toast()` logs to the console and returns a handle whose `dismiss` does nothing |
| `toast-store` (§6 #7) | `NotificationStore.ProcessNotification` | The global (checked at boot and on each toast) | Same as above |
| (no status) lock screen (§6 #14) | `securitystore.IsLockScreenActive` | The global; missing means "unlocked" | The router patch may run under the lock screen and find nothing, then retry |

CDP target naming (§6 #15) is a backend lookup (`backend/remora/cdp.py`); its state is shown in
Settings → About (`remora/injector_status`).

## Notes on Steam internals that need device testing

These parts follow the compatibility spec but could not be checked against a live Steam client:

- **Toast posting.** The object passed to `NotificationStore.ProcessNotification(info, data, 0)`
  carries `nNotificationID` (from `m_nNextTestNotificationID++`), `eType`, `eSource: 1`, `data`,
  the `decky`/`remora` markers and an `fnTray`. Which fields Steam copies into the notification it
  renders is not known. The renderer patch therefore looks for Remora's notification by marker or
  by its `data` object, in `props.notification`, `props.toast`, `props.group.notifications[0]`,
  `props.notifications[0]` and `props` itself. The tray/popup layout guess uses `props.location`.
- **ErrorBoundary state.** An error counts as showing when `state.error` is set and, if both exist,
  `state.lastErrorKey === props.errorKey`.
- **Router remount.** Remounting under the patched ErrorBoundary rebuilds the whole Steam UI tree
  below it once at boot (Decky does the same). The memo-props fallback is untested.
- **QAM visibility.** It is read from the QAM render argument `visible`. Until Steam passes one,
  every mounted Remora panel reports its own document's visibility and the QAM counts as visible
  while any live panel's document is (a panel drops out when it unmounts).
- **Route patch remounts.** A patched route child with a memo/forwardRef type gets a new wrapped
  type on every root render, so React remounts that page whenever the root re-renders. Caching
  the wrapped type is not safe while plugin patches re-run (and re-patch the type) on every
  render; see the comment on `retype` in `hooks/router.tsx`. Needs a device check.
- **Re-injection in place.** The new instance connects its socket first and only then replaces
  the old one. The old instance's QAM, router and ErrorBoundary patches check a `stopped` flag
  and pass Steam's output through, and mounted QAM fibers are pointed back at the current type
  (matched through the `__deckyPatch` chain).

## Other reliability rules

- Every plugin surface (QAM panel, plugin content, `titleView`, list icons, routes, global
  components, toasts) renders inside Remora's own error boundary (`PluginBoundary`), which blames
  the plugin by the URL schemes in compat/frontend.md §5.5.
- Plugins load in parallel at boot; one failing import becomes an error entry and a toast, and
  the rest still load. Boot imports time out after 60 s (backend `loader/import_plugin` events:
  15 s). Unlike the contract (which re-throws and adds nothing), a timed-out import also gets the
  error entry with Retry and a toast; it still re-throws. If it finishes later anyway, it replaces
  the entry, or gets `onDismount()` if something newer replaced it. Update checks and the
  SP-crash check start without waiting for the imports.
- Imports of one name run one at a time, and each import request and each unload bumps a
  per-name generation. An import whose generation is stale when its turn comes is skipped; one
  that finishes stale is dismounted, never added. So an unload or disable during an import wins,
  and of two imports the newest wins.
- After every reconnect (not the first connect) the loader re-reads `remora/status`, the
  settings and `loader/get_plugins`, then imports missing plugins, reloads ones whose version
  changed (or that failed to load), and unloads ones that are gone or disabled: events sent
  during the gap are lost.
- WebSocket call ids start at a random base per JS context, so a reloaded context never gets
  replies the backend cached for the previous one.
- The SP-crash check looks three more times over about 2 s before it restarts the web helper.
- Hook status writes that happen during a render are published in a microtask, and writes that
  change nothing are skipped.
- Unloading a plugin empties its `@decky/api` listener map but keeps it registered, so a reload
  whose `@decky/api` module is cached (no new `connect()`) still gets its events.
- Root approval: after Allow (install prompt or Permissions page) the backend waits for a
  physical confirm or a terminal command. `remora/confirm_waiting` opens a modal (and an inline
  card in the QAM panel and store) with the hint and a countdown; `remora/confirm_done` closes it
  and toasts the result. Cancel only closes the UI. Install and root prompt modals are closed by
  code when the prompt is answered inline or expires.
- Remora's own `remora.*` settings are written with `remora/settings/set`; a refused write puts
  the old value back in the UI.
- Each loader setup step (error boundary, report filter, toaster, QAM tab, router, pages) runs in
  its own try/catch.
- `frontend/test/smoke.mjs` boots the build against a fake Steam that has none of Steam's UI
  modules, and checks that everything above degrades without breaking the plugin loader.
