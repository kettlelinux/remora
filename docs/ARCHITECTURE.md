# Remora architecture

Remora is a plugin loader for Steam's Game Mode UI. It runs existing Decky Loader plugins
unchanged, and aims to be more reliable, more secure, easier to develop for, and not tied to a
single plugin store.

The compatibility target is defined in [compat/](compat/README.md). Section numbers like
"frontend §3" below refer to those specs.

## Ground rules

- **Clean room.** Remora's code is written from the behaviour specs in `compat/`, not from
  Decky's source. Nothing from `decky-loader` (GPL-2.0) is copied. The one exception is
  `@decky/ui`, which is shipped unmodified as a separate LGPL-2.1 module (see below).
- **Drop-in.** A Steam Deck with Decky installed switches to Remora without reinstalling plugins
  or losing their settings.
- **Plugins first.** When the better design and an existing plugin disagree, the plugin keeps
  working, and the better behaviour goes behind a flag or a new API.

## Drop-in contract

Kept exactly as Decky has it (backend §1, §7; store §5):

- `~/homebrew/{plugins,settings,data,logs,services}` and the per-plugin
  `settings|data|logs/<folder>` directories, keyed by folder name
- `loader.json` keys, with unknown keys kept as they are
- `plugin_loader.service` as the unit name. Plugins restart it by that name.
- HTTP and WebSocket on `127.0.0.1:1337`, with the same routes. Plugin bundles hardcode
  `http://127.0.0.1:1337/plugins/<name>/…`.
- CEF remote debugging on `127.0.0.1:8080`, enabled via `.cef-enable-remote-debugging`
- The `DECKY_*` environment variables, and the `decky` / `decky_plugin` Python modules
- The loader's own modules as plugins import them by bare name: `settings.SettingsManager`,
  `helpers`, `injector` (survey §6)
- `aiohttp`, `certifi`, `watchdog`, `async_timeout` importable in plugin processes (survey §7)

## Components

```
remora/
  backend/remora/        Python package (the loader)
    supervisor/          starts, watches and restarts plugin hosts; owns loader.json
    host/                the per-plugin process: imports main.py, runs the lifecycle, serves calls
    compat/              decky, decky_plugin, settings, helpers, injector shim modules
    server/              aiohttp HTTP + WebSocket on :1337, auth, /fetch proxy
    cdp/                 CEF DevTools client: finds SharedJSContext, injects, re-injects
    store/               sources, index fetch, install pipeline, updates
  frontend/              TypeScript runtime injected into Steam
    runtime/             boot, globals (SP_REACT, SP_JSX, SP_REACTDOM, DFL), plugin loading
    api/                 the @decky/api init hook (v1 and v2) and the legacy ServerAPI
    hooks/               QAM tab, router, toaster, error boundary: one module per Steam lookup
    ui/                  Remora's own QAM page, store, settings, plugin list
  sdk/                   typed decky.pyi stubs, a test harness that runs plugins without Steam
  packaging/             systemd unit, SteamOS installer, distro package notes
  docs/
```

### Backend

**Supervisor.** It is one root process and the only part that runs as root. It reads
`loader.json`, scans `plugins/`, starts one host per plugin, and talks to hosts over a private
socket per plugin in `/run/remora/` (mode 0700). It never uses the predictable path in `/tmp`
(backend W7). Hosts are started with `subprocess`, not `fork()`, so they don't inherit the auth
token, the :1337 listening socket or the supervisor's memory (backend W4).

**Host.** It runs a plugin's `main.py`. A non-root plugin's host runs as the Game Mode user, with
that user's own groups (not root's), `PR_SET_NO_NEW_PRIVS`, and a clean environment plus the
`DECKY_*` variables. A root plugin's host runs as root, but only once the user has approved it
(see Permissions). The host implements both calling conventions: `api_version` 0 passes the class
as `self` with kwargs, 1+ uses an instance with positional args (backend §3). Children the plugin
starts stay direct children of the host, so `PR_SET_PDEATHSIG` keeps working (Kettle's Device
Settings relies on it).

**Reliability over Decky:**
- A host that crashes is restarted with backoff, and the frontend gets a `plugin_crashed`
  event; Decky leaves it dead (backend §4).
- Every call has a timeout (default 30 s, per-plugin override). A non-JSON return value comes
  back as an error instead of hanging the call (backend §3 "Results").
- Errors carry both `message` and `error`, so frontends that read either get the text
  (frontend §6).
- `loader.json` is written atomically (temp file + rename).
- Unload sends SIGTERM, runs `_unload`, and sends SIGKILL after 5 s, as Decky does, but tracked
  per host so one stuck plugin doesn't block the others.

**Filesystem safety.** The supervisor never runs `chown`, `chmod` or `realpath` on paths under
`~/homebrew`. Per-plugin directories are created by a helper that runs as the user and refuses
symlinks (`O_NOFOLLOW` walks). Settings files are never moved between the user's and root's
directories. The user's `site-packages` are never added to a root process's `sys.path`. Plugin
folders are never made world-writable. These close the holes in backend W3, W5, W6 and store §2.

### Frontend

**Injection.** It matches Decky: a CDP client attaches to the `SharedJSContext` tab, evaluates a
small bootstrap, and imports `http://127.0.0.1:1337/frontend/index.js`. It re-injects when the
context reloads, and switches to a minimal fallback UI after repeated crashes (frontend §1).

**Globals.** `SP_REACT`, `SP_REACTDOM` and `SP_JSX` come from Steam's webpack. `window.DFL` is
the unmodified `@decky/ui` npm package, pinned and loaded as its own module, which keeps the
LGPL boundary clean. Every old export name must still be there, because the legacy plugins
depend on them (survey §5). The `@decky/api` init global keeps its exact name and answers
`connect(2, name)` and `connect(1, name)` (frontend §2).

**Resilient Steam lookups.** Each of the 15 fragile lookups (frontend §6) is its own module with
an ordered list of strategies (current signature, older signatures, structural search) and a
health check. If a lookup fails, only the feature that needs it is turned off, and the plugin
list says why. A Steam update can't take the whole loader down.

**Private state some plugins read.** Remora keeps `DeckyPluginLoader.pluginReloadQueue`,
`deckyState.setActivePlugin` and `routerHook.routerState._routePatches`, because TabMaster and
Kettle's welcome plugin read them (survey §8).

### Store

- **Sources.** The store is a list of sources. The Decky store is one built-in source and speaks
  the existing API (store §1), but users can add others or remove it. A source can also be a
  static signed `index.json`, a superset of the Decky list format (store §6).
- **Installs.** Download, check the hash, check the signature if the source signs, then unpack
  into a staging folder. Reject zips with more than one top-level folder or path traversal, then
  swap the folder in atomically. A failed update leaves the old version installed.
- **Updates** don't run `_uninstall` and don't reset the plugin's hidden, frozen or order
  settings (store §2).
- **`arch` and `min_loader`** in the index stop ARM devices from installing x86_64-only
  plugins.
- **Telemetry** (install counters) is off unless the user turns it on.
- **The `X-Decky-Version` header** sent to the Decky source is configurable, and a 426 response
  shows a clear message instead of an empty store.

### Permissions

The `root` flag becomes a permission the user approves. Installing or first loading a root
plugin asks once and shows what root means. Existing root plugins migrated from Decky are
approved automatically, so migration doesn't break them, and the user can revoke them in
settings. Later API versions may add finer permissions (network proxy, CEF tab access, file
picker outside home), but v1 ships only `root` to stay compatible.

**Auth.** The token stays available to Steam's JS context, which needs it. `/auth/token` checks
`Host` and `Origin`, and the token rotates on every loader start (backend W1).

### Developer experience

- `remora dev <plugin-dir>` links a plugin in, watches it, and reloads the frontend and backend
  on change, without needing the `debug` flag.
- `sdk/` ships typed `decky.pyi` and `@decky/api` type references, plus a harness that runs a
  plugin's backend and calls its methods without a Deck or Steam.
- Logs: one file per plugin per run, plus a QAM log viewer.

## Milestones

1. **Skeleton.** The supervisor runs under systemd, CDP injection works, and a Remora QAM tab
   shows "hello" on a real device.
2. **Backend parity.** Hosts, the `decky` module and the compat shims, the WebSocket protocol,
   HTTP routes. Kettle's five plugins run their backends.
3. **Frontend parity.** Globals, `DFL`, `@decky/api` and the legacy ServerAPI, QAM/router/toaster.
   Kettle's five plugins work end to end.
4. **Survey coverage.** The 46-item minimum set (survey §4). 41 of the 45 surveyed plugins
   pass, then the remaining four.
5. **Store.** The Decky source client, the atomic install pipeline, updates, Decky
   `~/homebrew` migration.
6. **Hardening.** Permissions UI, token rotation, resilient lookups with health checks.
7. **Open store.** Signed static indexes, multiple sources, `arch`/`min_loader`.
8. **Dev tools.** `remora dev`, the SDK harness, docs.

## Open decisions

- Whether to support Windows/macOS for development the way Decky does, or Linux only
