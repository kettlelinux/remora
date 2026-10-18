# Decky Loader backend: compatibility spec for a clean-room Python rewrite

Reference: Decky Loader v3.2.10 backend (the commit Kettle packages, `packages/decky-loader/PKGBUILD:11`).
Paths below are relative to `decky-ref/decky-loader/backend/decky_loader/` unless
they start with another root. `L` = line. Everything here is a description of behaviour, not code.

Abbreviations: **loader** = the long-running root process; **plugin process** = the per-plugin child;
**frontend** = Decky's JS injected into Steam's SharedJSContext; **HOST_USER** = the unprivileged user
(uid 1000 on Kettle, `deck` on a Steam Deck); **EFFECTIVE_USER** = the user the loader runs as (root).

---

## 1. On-disk layout, environment, ownership

### 1.1 Two roots

| Concept | Env var | Fallback | Holds |
|---|---|---|---|
| Privileged path | `PRIVILEGED_PATH` | the unprivileged path | `plugins/`, `settings/loader.json` (localplatform/localplatformlinux.py L238-246, main.py L47, L75) |
| Unprivileged path, "homebrew", `DECKY_HOME` | `UNPRIVILEGED_PATH` | parent of `$PLUGIN_PATH`; else 2 dirs above the frozen binary (`.../homebrew/services/PluginLoader`) or 3 dirs above the source file; else `/home/deck/homebrew` (localplatformlinux.py L257-282) | `settings/<plugin_dir>/`, `data/<plugin_dir>/`, `logs/<plugin_dir>/`, `services/` (upstream binary + `.loader.version`, updater.py L205) |
| Unprivileged user | `UNPRIVILEGED_USER` | the passwd entry whose home is the longest prefix of the unprivileged path; else `deck` (L285-302) | |

Both paths are `os.makedirs`'d on every lookup (L244, L280).

Upstream systemd unit sets both paths to `${HOMEBREW_FOLDER}` (= `~/homebrew`), runs as `User=root`
(`dist/plugin_loader-release.service` L6-14). So upstream, `~/homebrew/plugins` and
`~/homebrew/settings/loader.json` live in the user's home.

**Kettle** (`packages/decky-loader/decky-loader-start` L15-142, `plugin_loader.service`):
- `PRIVILEGED_PATH=/home/.kettle/decky` (root:root 0755): `plugins/`, `plugins-aside/`, `settings/loader.json`.
- `UNPRIVILEGED_PATH=~/homebrew` (uid 1000), `UNPRIVILEGED_USER` = passwd name of uid 1000.
- `CHOWN_PLUGIN_PATH=0`, `KEEP_SYSTEMD_SERVICE=1`, `PYTHONPATH=/usr/lib/decky-loader`, `PYTHONDONTWRITEBYTECODE=1`, `LOG_LEVEL=INFO`; runs `python3 -P` with cwd = privileged path (L142-149).
- Image-shipped plugins live in `/usr/share/decky/plugins/<dir>` and are **symlinked** into
  `/home/.kettle/decky/plugins/<dir>` at each start (L128-140). A store copy with the same dir name is moved to `plugins-aside/`.
- One-time migration of `~/homebrew/plugins` and `~/homebrew/settings/loader.json` into the privileged path (L28-123).
- Creates `~/homebrew/{settings,data,logs,services}` as the user (L20). Touches `~/.local/share/Steam/.cef-enable-remote-debugging` (CEF devtools on :8080, needed for injection) (L125-126).
- Unit: `Restart=always`, `KillMode=process`, `TimeoutStopSec=15`, unit name `plugin_loader` (the loader restarts itself by that name).

### 1.2 Other loader env knobs (localplatform/localplatform.py)

| Var | Default | Meaning |
|---|---|---|
| `CHOWN_PLUGIN_PATH` | `1` | chown/chmod plugin dirs on load and install (L32-33) |
| `SERVER_HOST` / `SERVER_PORT` | `127.0.0.1` / `1337` | HTTP+WS listen address (L35-39) |
| `LIVE_RELOAD` | `1` | watchdog file watcher (L41-42) |
| `KEEP_SYSTEMD_SERVICE` | `0` | updater keeps the unit file (L44-45) |
| `USE_CEF_CLOSE_WORKAROUND` | `1` | gdb-close the stale :8080 socket (L47-48) |
| `LOG_LEVEL` | `INFO` | loader log level (L50-53) |
| `LINUX_SERVICE_MANAGER` | autodetect from `/sbin/init` | systemd/openrc/none (localplatformlinux.py L11-26) |
| `DECKY_BRANCH` | `dirty` | git branch for updater URLs (updater.py L46) |

### 1.3 Ownership rules

- On loader start with `CHOWN_PLUGIN_PATH=1`: `plugins/` created as user then chowned to root, chmod 755 (main.py L49-57).
- Per plugin at load (plugin/plugin.py L50-59, only if `CHOWN_PLUGIN_PATH=1`): if the plugin dir isn't root's, recursively chown it to root if `"root"` in flags else to HOST_USER, then the dir itself to root, chmod -R 755; `plugin.json` to root, 755. Install does the same (browser.py L120-137).
- Per plugin at load, **unconditionally** (plugin.py L72-79): create `DECKY_HOME/settings/<dir>`, `data`, `data/<dir>`, `logs`, `logs/<dir>` via `mkdir_as_user` = `realpath` + `makedirs` + `chown -R HOST_USER:group` (helpers.py L64-67, localplatformlinux.py L74-85). See weak point W3.
- Loader settings dir: owner forced to HOST_USER if `CHOWN_PLUGIN_PATH=1`, else root (settings.py L30-33).

---

## 2. Plugin package layout

A plugin is a directory directly under `<privileged>/plugins/` that contains a regular `plugin.json`
(loader.py L201). The **directory name** ("plugin_directory") and the **plugin.json `name`** are both
load-bearing and different:
- `name` is the key everywhere at runtime: plugin map, WS calls, HTTP routes `/plugins/{name}/...`,
  `disabled_plugins`, `pluginOrder`, events (loader.py L137-238). The frontend's `@decky/api` identifies itself
  with the name from plugin.json baked in at build time (`loader-api/src/index.ts` L36). Names contain spaces
  ("Device Settings") and are URL-encoded in routes.
- the directory name is used for paths: `DECKY_HOME/{settings,data,logs}/<dir>` and `DECKY_PLUGIN_DIR`.
- Duplicate names: last import wins (map overwrite); `find_plugin_folder` returns the first dir whose plugin.json name matches (browser.py L109-118).

### 2.1 plugin.json (plugin/plugin.py L33-43)

| Field | Required | Backend use |
|---|---|---|
| `name` | yes (KeyError -> plugin not loaded) | identity |
| `author` | yes | `DECKY_PLUGIN_AUTHOR` |
| `flags` | yes (list) | `"root"`: run plugin process as root. `"debug"`: allow hot reload when a file changes (loader.py L177-180). Any other string ignored; templates use `"_root"` as a commented-out root (decky-plugin-template/plugin.json L4) |
| `api_version` | no, default 0 | 0 = legacy calling convention; >=1 = modern (see section 3) |
| `publish` {`tags`,`description`,`image`} | no | store metadata only, unused by backend |

### 2.2 package.json (plugin/plugin.py L34-38, browser.py L66-106)

Optional. If present:
- `version` **required** (KeyError otherwise) -> plugin version (`DECKY_PLUGIN_VERSION`, `get_plugins`, logs).
- `type == "module"` -> load type `ESMODULE_V1` (1); otherwise `LEGACY_EVAL_IIFE` (0) (enums.py L7-9).
- `remote_binary`: list of `{name, url, sha256hash}`; at install, each is downloaded into `<plugin>/bin/<name>` and sha256-checked; mismatch fails the install (helpers.py L111-133).
- `name` is not read by the backend.

### 2.3 Files

| Path | Role |
|---|---|
| `dist/index.js` | frontend bundle. ESM plugins: frontend imports `/plugins/{name}/dist/index.js?t=...` and calls its default export. Legacy: frontend fetches `/plugins/{name}/frontend_bundle` (same file) and evals it with a `serverAPI` object (frontend/src/plugin-loader.tsx L470-530) |
| `dist/assets/*` | served at `/plugins/{name}/assets/*`; @decky/rollup hardcodes `http://127.0.0.1:1337/plugins/<name>/assets/<file>` into bundles (seen in Kettle's welcome/dist/index.js) |
| `main.py` | backend. Absent -> **passive** plugin: no process, method calls raise (plugin.py L46, L118-119, L139-141) |
| `py_modules/` | appended to the plugin process's `sys.path` (sandboxed_plugin.py L91) |
| `defaults/` | template convention only; the template's build copies it into the zip root. Backend ignores it |
| `bin/` | target of `remote_binary`; also where `backend/out` binaries end up in the release zip (CLI convention). Backend only creates it |
| `backend/` (`Dockerfile`, `Makefile`, `src/`) | build-time only (decky CLI builds it, output `backend/out` -> zip `bin/`). Backend ignores it |

Install zip layout: one top-level dir containing `plugin.json` (browser.py L232-257, L58-64; extracted straight into `plugins/`).

---

## 3. Python plugin lifecycle and method dispatch

### 3.1 Load (plugin/sandboxed_plugin.py `initialize`, L55-144)

In the plugin process:
1. Set process/thread title to `"<name> (<main.py path>)"` / `<name>` (setproctitle).
2. New asyncio event loop; SIGINT/SIGTERM handlers schedule `shutdown()` (L66-67).
3. Passive -> return (never reached in practice; passive plugins get no process).
4. Drop privileges (L72-73), set env (L75-88, see section 5), append `py_modules` to `sys.path` (L91).
5. Alias every loaded `decky_loader.X` module as top-level `X` in `sys.modules` (L94-96). This is what makes
   legacy `import helpers`, `from settings import SettingsManager`, `import localplatform` work in old plugins. **Compat requirement** for legacy plugins: provide `helpers` (`get_homebrew_path`, `get_home_path(username)`, `get_user`, `get_user_id`, `get_user_group`, `get_user_group_id`, `get_effective_user*`, `get_user_owner`, `get_loader_version`, `get_ssl_context`, `get_csrf_token`, `mkdir_as_user`, systemd unit helpers, `set_user`/`set_user_group` no-ops; helpers.py L135-195) and `settings.SettingsManager(name, settings_directory=None)` with `getSetting`/`setSetting`/`read`/`commit` (settings.py).
6. Import `decky` (plugin/imports/decky.py), replace its `emit` with a real one, register it as `sys.modules["decky"]` and `sys.modules["decky_plugin"]` (L98-110).
7. Load `main.py` as a module named `"_"` via `spec_from_file_location` (L112-116). Module top-level code runs here.
8. `api_version > 0`: `self.Plugin = module.Plugin()` (instance, no ctor args). Else `self.Plugin = module.Plugin` (the **class itself**) (L118-121).
9. If `_migration` exists: run it **to completion, blocking**, before anything else (L123-127).
10. If `_main` exists: schedule it as a task (not awaited; may run forever) (L128-132).
11. Schedule the IPC server (L133), then `run_forever`.
12. Any exception in steps 4-11: log "Failed to start", `sys.exit(0)` (L134-136). Loader is not told.

Legacy self quirk (api_version 0): every lifecycle hook and method is called as `Plugin.method(Plugin, ...)`, i.e. `self`
is the class; state set on `self` becomes class attributes (L127, L131, L150-153, L165-168, L205).

### 3.2 Hooks

| Hook | When | Notes |
|---|---|---|
| `_migration()` | before `_main`, blocking | template uses it with `decky.migrate_*` |
| `_main()` | task at start | long-running loops normal (all Kettle plugins loop here) |
| `_unload()` | on SIGTERM/SIGINT, in `shutdown()` (L146-159, L176-190) | exceptions logged and swallowed |
| `_uninstall()` | after `_unload`, only if the loader sent the uninstall message first (L161-174, L182-184) | |

All hooks must be **coroutine functions**: a sync `_main` makes `create_task` raise -> plugin exits at start;
sync `_migration` likewise; sync `_unload` raises -> logged. After hooks, the loop is stopped and `SystemExit(0)` raised.

### 3.3 Method calls

Two conventions, chosen by `api_version` and enforced in the plugin process (L192-214):

| | Modern (`api_version >= 1`) | Legacy (`api_version` 0) |
|---|---|---|
| Frontend API | `@decky/api` `call(name, ...args)` / `callable(name)` | `serverAPI.callPluginMethod(name, kwargsObj)` |
| WS route | `loader/call_plugin_method` args `[plugin_name, method, ...args]` | `loader/call_legacy_plugin_method` args `[plugin_name, method, kwargs]` |
| Invocation | `await getattr(instance, method)(*args)` | `await getattr(Class, method)(Class, **kwargs)` |
| Wrong convention | error "api_version 1 or newer is required..." | error "Legacy methods may not be used on api_version > 0" |
| Success to frontend | WS REPLY `result` = return value | WS REPLY `result` = `{"success": true, "result": value}` |
| Failure to frontend | WS ERROR `{name:"Exception", message:str(e), traceback:<loader-side trace>}`, i.e. the JS promise rejects | REPLY `{"success": false, "result": str(e)}` (never rejects) |

- Methods whose name starts with `_` are refused by the loader (loader.py L215-216, L227-228).
- Methods must be `async` (or return an awaitable): result is always `await`ed; a sync method yields a TypeError error response.
- Each call runs as its own task in the plugin loop (localsocket.py L116-126), so calls are **concurrent**; a blocking sync body inside an async method blocks the plugin's whole loop (Kettle plugins use `asyncio.to_thread` heavily for this).
- Return values are serialized with `json.dumps(..., ensure_ascii=False)` in the plugin process (L214). Only JSON types survive (tuples -> arrays, dict keys -> strings). **Non-serializable return -> the dumps raises outside the try, no response is ever written, the frontend call hangs forever.** Exceptions are reduced to `str(e)`; the exception type is lost.
- **No timeout** anywhere on the backend for method calls. The frontend's 15 s timeout applies only to loading the JS bundle (`loader/import_plugin` arg, loader.py L196).
- Unknown plugin name -> KeyError -> WS ERROR. Passive plugin -> RuntimeError "This plugin is passive".
- If the plugin stops while a call is pending, the request is cancelled with `PluginStopped` -> WS `DISCARD` (plugin.py L101-110, messages.py L26-41, wsrouter.py L72-73).

### 3.4 Events (plugin -> frontend)

`await decky.emit(event, *args)` in the plugin writes an EVENT line (sandboxed_plugin.py L99-104); the loader
re-emits it on the WS as event `loader/plugin_event` with payload `{"plugin": <name>, "event": event, "args": [..]}`
(loader.py L168-170). The frontend dispatches to listeners registered by that plugin's `addEventListener(event, fn)`, each called with `...args` (plugin-loader.tsx L765-786).
- Events before the loader has connected to the plugin's socket are **silently dropped** (localsocket.py L111-114).
- Events with no WS client connected are dropped with a warning (wsrouter.py L49-60).
- `emit` must be awaited (it is a coroutine).

### 3.5 Frontend-visible plugin list

WS `loader/get_plugins` -> `[{name, version, load_type, disabled}]` (loader.py L137-139). After a non-batch
(re)load, the loader emits `loader/import_plugin(name, version, load_type, true, 15000)` (L195-196).
Disabled plugins are kept in the map with `disabled=True` and no process (L173-176).

---

## 4. Process model, IPC, reload

### 4.1 Spawn

- One `multiprocessing.Process` per non-passive plugin, target `SandboxedPlugin.initialize`, arg the `LocalSocket` (plugin.py L139-145). Relies on the **fork** start method (Linux default up to Python 3.13; 3.14 defaults to forkserver, which would need pickling). The child therefore inherits the loader's whole memory: CSRF token, loaded modules, the open listening socket on :1337, sys.path (including system/user python paths added by `get_system_pythonpaths`, helpers.py L94-108, main.py L276).
- stdout/stderr are inherited (journal on Kettle).
- uid/gid drop (localplatformlinux.py L137-157): `setgid` then `setuid` to HOST_USER's primary gid/uid unless `"root"` in flags; no-op if already equal. **Supplementary groups are not touched** (no `setgroups`/`initgroups`), so a "non-root" plugin keeps root's supplementary groups (W4). No capability bounding, no namespaces, no seccomp, no rlimits.

### 4.2 IPC protocol (localplatform/localsocket.py, plugin/messages.py)

- Transport: Unix stream socket at `/tmp/plugin_socket_<time.time()>` (localsocket.py L16), path chosen in the loader before fork. The **plugin process is the server** (binds after dropping privileges, so the socket is owned by the plugin's uid); the loader is the client. TCP 127.0.0.1:40000-60000 on Windows.
- Loader connects lazily, retrying 10x every 2 s per attempt cycle (L33-45); one connection carries everything both ways.
- Framing: UTF-8 JSON, one object per line, `\n`-terminated; stream buffer limit 1 MiB but over-long lines are read in pieces (L7, L86-102).
- Messages (type enum: CALL=0, RESPONSE=1, EVENT=2; messages.py L5-8):
  - loader -> plugin CALL: `{"type":0, "method":str, "args":list|dict, "id":uuid4-str, "legacy":true?}` (plugin.py L123, L134).
  - loader -> plugin uninstall notice: `{"uninstall": true}` (no type) (plugin.py L158; handled sandboxed_plugin.py L195-197).
  - plugin -> loader RESPONSE: `{"type":1, "id":..., "success":bool, "res":any}`.
  - plugin -> loader EVENT: `{"type":2, "event":str, "args":list}`.
- Responses matched by `id` (plugin.py L91-100).

### 4.3 Stop / restart / crash

- `stop(uninstall)` (plugin.py L147-174): if uninstall, write the uninstall notice (1 s budget), then SIGTERM immediately, cancel the listener, poll every 100 ms; after 5 s SIGKILL (L176-183). The plugin's `_unload` (+`_uninstall`) therefore has at most ~5 s. Uninstall notice and SIGTERM race (W8).
- Loader shutdown stops all plugins in parallel (loader.py L105-106; main.py L120-167); aiohttp shutdown_timeout 40 s (main.py L262).
- **No crash detection or restart.** If a plugin process dies, it stays dead until a reload/enable/loader restart; pending calls are not failed (W7).
- Re-import of an already-loaded name stops the old instance first (loader.py L177-183).

### 4.4 Reload

- watchdog `Observer` on the plugins dir, recursive, regexes `dist/index.js` and `main.py`, ignoring `__pycache__` and directories (loader.py L27-68, L84-89). Enabled 10 s after start (L108-117); disabled during install/uninstall (browser.py L140-171, L191-192, L305-306) and shutdown.
- A change queues `(main.py, dir, refresh=True)`; `import_plugin` skips it unless `"debug"` in flags (L177-180). So only debug plugins hot-reload; others need `loader/reload_plugin`.
- WS `loader/reload_plugin(name)` queues an unconditional re-import (L235-238). Reloads are serialized through one queue (L206-209).
- Startup: `import_plugins` with `batch=True` (no per-plugin `import_plugin` event; the frontend pulls `get_plugins`) (L198-204). Sets `pluginOrder` to the loaded names if unset (main.py L177-183).

---

## 5. The `decky` module (plugin/imports/decky.py, decky.pyi)

Available as `import decky` and legacy `import decky_plugin` (same object). Constants are read from the
process environment **at import time** (the loader sets the env first, sandboxed_plugin.py L75-88). Plugins
also commonly read the env vars directly via `os.environ`.

| Name | Env var | Value |
|---|---|---|
| `HOME` | `HOME` | root plugin: root's home; else HOST_USER's home (L75) |
| `USER` | `USER` | `"root"` or HOST_USER name (L76) |
| `DECKY_VERSION` | same | `get_loader_version()`: `vX.Y.Z[-preN][-devN]`, `dev` for 0.0.0, `unknown` on error (helpers.py L70-89). From `importlib.metadata.version("decky_loader")`; Kettle fakes a dist-info for this (PKGBUILD L49-53) |
| `DECKY_USER` | same | HOST_USER name, even for root plugins |
| `DECKY_USER_HOME` | same | HOST_USER home, even for root plugins |
| `DECKY_HOME` | same | unprivileged path (`~/homebrew`) |
| `DECKY_PLUGIN_SETTINGS_DIR` | same | `DECKY_HOME/settings/<dir>` |
| `DECKY_PLUGIN_RUNTIME_DIR` | same | `DECKY_HOME/data/<dir>` |
| `DECKY_PLUGIN_LOG_DIR` | same | `DECKY_HOME/logs/<dir>` |
| `DECKY_PLUGIN_DIR` | same | `<privileged>/plugins/<dir>` (on Kettle may be a symlink into /usr/share/decky/plugins) |
| `DECKY_PLUGIN_NAME` | same | plugin.json name |
| `DECKY_PLUGIN_VERSION` | same | package.json version; env var unset (constant `""`) if no package.json |
| `DECKY_PLUGIN_AUTHOR` | same | plugin.json author |
| `DECKY_PLUGIN_LOG` | not set as env (pyi claims it is) | `DECKY_PLUGIN_LOG_DIR/<YYYY-MM-DD HH.MM.SS>.log`, time of import |
| `__version__` | | `'1.0.0'` |

Functions:
- `migrate_any(target_dir, *paths) -> dict[old, new]`: for each path: missing -> maps to `""`; directory -> its contents copied recursively into target and the source removed, maps to target; file -> copied into target and removed, maps to `target/basename`. Implemented via a `sh -c` tar pipe; mkdirs the target (decky.py L136-159).
- `migrate_settings(*p)`, `migrate_runtime(*p)`, `migrate_logs(*p)`: `migrate_any` into the respective dir (L162-192).
- `async emit(event, *args)`: see 3.4.
- `logger`: the **root** logger, reconfigured with `basicConfig(filename=DECKY_PLUGIN_LOG, format='[%(asctime)s][%(levelname)s]: %(message)s', force=True)`, plus a `StreamHandler` (stderr), level INFO (L205-213). On import, all but the 4 newest `*.log` files in the log dir are deleted (L199-203).

Because `logger` is the root logger, any library logging in the plugin process lands in the plugin log too.

---

## 6. Loader services plugins depend on indirectly

### 6.1 HTTP server (aiohttp on 127.0.0.1:1337; main.py L59-101, L262)

The port is effectively ABI: bundles hardcode `http://127.0.0.1:1337/...` (asset URLs, the frontend itself).

| Route | Auth | Behaviour |
|---|---|---|
| `GET /auth/token` | none | returns the CSRF token (uuid4 per loader start, helpers.py L22) as text (main.py L97, L174-175) |
| `GET /ws?auth=<token>` | query param | the single WS (6.2) (wsrouter.py L80-83) |
| `GET/POST/PUT/DELETE/PATCH/HEAD /fetch?auth=<token>&fetch_url=<urlenc>` | query param | streaming proxy (`fetchNoCors`, `getExternalResourceURL`): forwards method/body; drops `Host, Origin, Sec-Fetch-Site/Mode/Dest` and anything listed in `X-Decky-Fetch-Excluded-Headers` (comma-space separated); `X-Decky-Header-<Name>` becomes `<Name>`; all other `X-Decky-*` removed; sets `User-Agent: Decky/<ver> (https://decky.xyz)`; no auto-decompress; strips upstream `Access-Control-Allow-Origin/-Credentials/-Expose-Headers` so the CORS layer adds its own; chunked passthrough (utilities.py L91-92, L147-216) |
| `GET /plugins/{name}/dist/{path}` | exempt | file from `<plugin>/dist/` (loader.py L141-145) |
| `GET /plugins/{name}/assets/{path}` | exempt | file from `<plugin>/dist/assets/` (L147-151) |
| `GET /plugins/{name}/data/{path}` | exempt | file from `DECKY_HOME/data/<dir>/` (L153-158) |
| `GET /plugins/{name}/frontend_bundle` | `X-Decky-Auth` | `dist/index.js` as `application/javascript` (legacy plugins) (L160-164) |
| `GET /frontend/{path}` | exempt | loader's own built frontend, `static/` (L124-126) |
| `GET /locales/{lang}` | header | loader translations, `{}` if missing (L128-135) |
| `GET /static/*` | exempt | `static/` dir (main.py L101) |
| `POST /methods/{name}` | header | legacy utility call, JSON body = kwargs, returns `{"result","success"}` (utilities.py L87-109) |

All file routes send `Cache-Control: no-cache`. CSRF middleware (helpers.py L37-54): passes `OPTIONS`, a matching
`X-Decky-Auth` header, or URL prefixes `/auth/token`, `/plugins/load_main/`, `/static/`, `/steam_resource/`,
`/frontend/`, paths `/fetch` and `/ws`, and regexes `^/plugins/.*/(assets|data|dist)/.*`; everything else 403.
CORS (aiohttp_cors) allows origin `https://steamloopback.host` with credentials, all headers (main.py L66-72, L99-100; routes added after that loop, i.e. `/static`, are not CORS-registered).

### 6.2 WebSocket RPC (wsrouter.py)

- One client at a time; a new connection closes the previous one (L89-96). Text frame `close` ends the session.
- Frames are JSON. Types (L14-25): `ERROR=-1, CALL=0, REPLY=1, DISCARD=2, RECEIVED_RESPONSE=3, FULL_SYNC=4, EVENT=5`.
  - client CALL `{type:0, id:int, route:str, args:list}` -> route called with `*args`; reply `{type:1, id, result}` or `{type:-1, id, error:{name, message, traceback}}`; unknown route -> ERROR name `RouteNotFoundError` (L127-147, L68-78).
  - Replies are cached per id until the client sends `{type:3, id}`; a duplicate CALL with a cached id gets the cached reply (L49-55, L127-137, L149-151).
  - After reconnect the client sends `{type:4, messages:[CallMessage...]}`; the server re-handles each and drops cached replies whose ids are absent (L153-162).
  - server EVENT `{type:5, event, args:list}` (L164-166).
- Routes (positional args):
  - `loader/`: `get_plugins()`, `reload_plugin(name)`, `call_plugin_method(name, method, ...args)`, `call_legacy_plugin_method(name, method, kwargs)` (loader.py L100-103).
  - `utilities/` (utilities.py L59-85): `ping` -> `"pong"`; `settings/get(key, default)`; `settings/set(key, value)`; `install_plugin(artifact, name, version, hash, install_type)`; `install_plugins(requests)`; `confirm_plugin_install(request_id)`; `cancel_plugin_install(request_id)`; `uninstall_plugin(name)`; `execute_in_tab(tab, run_async, code)` -> `{success, result}` (CDP `Runtime.evaluate` on the tab whose title equals `tab`); `inject_css_into_tab(tab, css)` -> id; `remove_css_from_tab(tab, id)`; `allow_remote_debugging`/`disallow_remote_debugging` (start/stop `steam-web-debug-portforward.service`); `start_ssh`/`stop_ssh` (bug: both mapped to `allow_remote_debugging`, L72-73); `filepicker_ls(path=None->user home, include_files, include_folders, include_ext, include_hidden, order_by="name_desc", filter_for=regex-or-substring, page=1, max=1000)` -> `{realpath, files:[{isdir,name,realpath,size,modified,created}], total}` (L315-398); `enable_rdt`/`disable_rdt`; `get_tab_id(name)`; `get_user_info()` -> `{username, path}`; `http_request(method, url, extra_opts, timeout)` -> `{status, headers, body}` (legacy fetch; `extra_opts` splatted into aiohttp `request`, L218-226); `restart_webhelper`; `close_cef_socket`; `_call_legacy_utility(method, kwargs)`; `enable_plugin(name)`; `disable_plugin(name)`; `set_all_plugins_disabled()`.
  - legacy utility names for `/methods/` and `_call_legacy_utility`: `ping, http_request, execute_in_tab, inject_css_into_tab, remove_css_from_tab, set_setting, get_setting, filepicker_ls, get_tab_id, get_user_info` (L39-50).
  - `updater/`: `get_version_info, check_for_updates, do_restart, do_shutdown, do_update, get_testing_versions, download_testing_version` (updater.py L55-61; Kettle patch 0001 makes it inert).
- Server events: `loader/import_plugin`, `loader/plugin_event`, `loader/unload_plugin`, `loader/disable_plugin`, `loader/add_plugin_install_prompt(name, version, request_id, hash, install_type)`, `loader/add_multiple_plugins_install_prompt(request_id, requests)`, `loader/plugin_download_start|info|finish`, `loader/notify_updates`, `updater/update_download_percentage`, `updater/finish_download`.

### 6.3 Frontend API surface backed by the above (plugin-loader.tsx L705-763, L788-815)

`@decky/api` connect (api versions 1-2): `call`, `callable`, `addEventListener`, `removeEventListener`,
`openFilePicker` (UI over `filepicker_ls`), `executeInTab`, `fetchNoCors` (-> `/fetch`), `getExternalResourceURL`,
`injectCssIntoTab`, `removeCssFromTab`, `routerHook`, `toaster`, v2 `useQuickAccessVisible`. Legacy `serverAPI`:
`callPluginMethod`, `callServerMethod` (-> `_call_legacy_utility`), `fetchNoCors` (-> `utilities/http_request`), `openFilePicker(V2)`, `executeInTab`, CSS, `routerHook`, `toaster`.

### 6.4 Injection into Steam

The loader polls CEF devtools at `http://localhost:8080/json` for the GamepadUI/SharedJSContext tab, connects,
and evaluates a stub that imports `http://localhost:1337/frontend/index.js?v=<ver>` (or `fallback.js` after 3
webhelper crashes within a minute each), guarded by `window.deckyHasLoaded`; if already set it kills
`steamwebhelper` to get a clean context; >4 crashes -> loader shuts down (main.py L103-118, L185-259, helpers.py L56-57, injector.py).

### 6.5 Install / uninstall (browser.py)

Install is two-step: `install_plugin` stores a request and emits a prompt; `confirm_plugin_install(id)`
downloads (`file://` or HTTP), posts an install-count increment to the store, uninstalls an existing copy, unzips into
`plugins/` (sha256 checked only if a hash was given), fetches remote binaries, fixes permissions, appends to
`pluginOrder` if new, imports (L173-326). Uninstall: emit `loader/unload_plugin`, stop with uninstall=True,
remove name from `frozenPlugins`/`hiddenPlugins`/`pluginOrder`/`disabled_plugins`, `rmtree` the dir (L139-171, L331-358).
The per-plugin settings/data/logs dirs are not removed. Disable: add to `disabled_plugins`, stop, emit `loader/disable_plugin`; enable: remove and re-import (utilities.py L484-520).

---

## 7. Settings storage (settings.py)

- `SettingsManager("loader", <privileged>/settings)` -> `<privileged>/settings/loader.json` (main.py L75). Kettle: `/home/.kettle/decky/settings/loader.json`.
- Plain JSON object, written with indent 4, `ensure_ascii=False`, rewritten in full on **every** `setSetting` (truncate+write, not atomic) (L51-55). Created empty (0-byte) at first start; unreadable JSON -> empty dict (L37-49).
- Constructor side effect: every `*.json` in the **unprivileged root** (`~/homebrew/*.json`) is renamed into the settings dir (L13-17, L22-27) (W5).
- Keys (backend + frontend; frontend greps of `frontend/src`):

| Key | Type | Used by |
|---|---|---|
| `pluginOrder` | list of names | QAM order; default = load order (main.py L181-183); install appends, uninstall removes |
| `hiddenPlugins` | list of names | frontend hide; cleaned on uninstall |
| `frozenPlugins` | list of names | frontend: exclude from updates; cleaned on uninstall |
| `disabled_plugins` | list of names | **backend**: not started (loader.py L173-176) |
| `branch` | int 0 Stable, 1 Prerelease, 2 Testing; -1/absent -> derived from version (updater.py L64-77) |
| `store` | int 0 default, 1 testing, 2 custom (browser.py L218-222) |
| `store-url` | str | custom store URL |
| `cef_forward` | bool | start/stop `steam-web-debug-portforward.service` at startup (main.py L86-89) |
| `developer.enabled`, `developer.valve_internal`, `developer.warn.third_party`, `developer.rdt.enabled`, `developer.rdt.ip` | bool/str | developer menu; `rdt.ip` -> React DevTools script fetched from `http://<ip>:8097` and run in Steam (utilities.py L427-462) |
| `user_info.user_name`, `user_info.user_home` | str | written by frontend from `get_user_info` (plugin-loader.tsx L240-241) |
| `notificationSettings`, `sortPlugins`, `store_select.warn.third_party` | any | frontend only |

The frontend may read/write **any** key through `utilities/settings/get|set`; a rewrite must persist unknown keys verbatim.

Plugin-side settings are the plugin's own business (files in `DECKY_PLUGIN_SETTINGS_DIR`; legacy plugins use `settings.SettingsManager`).

---

## 8. What Kettle's plugins actually use (packages/kettle-decky-plugins)

Five plugins, all `api_version: 1`, `flags: []` (non-root), with `main.py`, `py_modules/`, `package.json`
(ESM), `dist/index.js`, some `dist/assets/`. Dirs: `kettle-welcome` (Welcome), `kettle-device-settings`
(Device Settings), `kettle-crash` (Crash Reports), `kettle-game-settings` (Game Settings), `kettle-stores` (Game Stores) (PKGBUILD `_install_plugin` calls).

Backend contract used:
- `import decky`; `decky.logger` (all), `decky.emit` (crash, device-settings, stores; events `crash`, `steam_values`, `confirm`, `confirm_done`, `login`, `job`), `DECKY_PLUGIN_SETTINGS_DIR`, `DECKY_PLUGIN_DIR`, `DECKY_USER_HOME`. No `migrate_*`, no `decky_plugin`, no legacy `helpers`/`settings` imports.
- `class Plugin` instance methods, all `async`; `_main` (infinite loops in stores/device-settings/crash), `_unload`; no `_migration`/`_uninstall` on the class (`stores/main.py` L944 `_uninstall` is a module function).
- `py_modules` on `sys.path` (`import inotify`, `steamlib`, `refresh`, `powerd`, ...).
- **Cross-plugin file access**: Game Stores reads/writes sibling settings dirs `dirname(DECKY_PLUGIN_SETTINGS_DIR)/{kettle-game-settings,kettle-framegen,kettle-upscaling}/...` (stores/main.py L87, L1004-1011). Requires: settings dir = `DECKY_HOME/settings/<dirname>` and all non-root plugins sharing one uid.
- Spawns subprocesses with its own env (`XDG_RUNTIME_DIR=/run/user/1000`, session bus, popping `PYTHONPATH`) for `systemctl --user`, `xprop`, flatpak etc. (stores/main.py L150-158, shared/refresh.py L125-142); uses `PR_SET_PDEATHSIG` so children die with the plugin process (device-settings/main.py L50-53). Requires the plugin process to be the direct parent and to really exit on stop.
- Uses polkit-gated system tools as uid 1000 (Game Mode polkit rules trust the session), not root plugins.
- Frontend: `@decky/api` `callable` (~150), `call`, `addEventListener`/`removeEventListener`, `toaster`/`toast`, `routerHook`, `openFilePicker`, `definePlugin`; assets via hardcoded `http://127.0.0.1:1337/plugins/Welcome/assets/...`; welcome pokes `window.DeckyPluginLoader.deckyState.setActivePlugin` (frontend internal, welcome/src/tour.tsx L38).

---

## 9. Weak points worth fixing in a rewrite

Security (ordered by impact on Kettle):

- **W1. Token is free to any local process.** `GET /auth/token` is unauthenticated (main.py L97, helpers.py L41). Anything on the box (any uid, a Proton game, a Flatpak with network) gets the token, then can: install and confirm a plugin with `"root"` flag (root code exec, browser.py L309-326), run JS in Steam (`execute_in_tab`), proxy requests (`/fetch`, `http_request`), toggle SSH/devtools services, edit settings. Also no `Host` header check, so DNS rebinding from a web page can reach it. Fix: hand the token only to the injected frontend (inject it in the CDP stub instead of a fetchable route), check `Host`/`Origin`, consider a Unix socket + CDP-only path.
- **W2. Root plugins and root loader.** A `"root"` flag is all it takes; store plugins run as full root with no confinement. Kettle mitigates by keeping plugin dirs root-owned in `/home/.kettle/decky` and CHOWN_PLUGIN_PATH=0. Rewrite: run the loader itself unprivileged; spawn root plugins only via an allowlist (e.g. image-shipped dirs), with systemd-run/namespaces.
- **W3. `chown -R` on user-controlled paths as root.** Every plugin load calls `mkdir_as_user` on `~/homebrew/{settings,data,logs}/<dir>`, which `realpath`s the path and then `chown -R uid:gid` it (plugin.py L72-79, helpers.py L64-67). The user (or anything running as uid 1000) can replace `~/homebrew/data/kettle-crash` with a symlink to `/etc` or `/usr`, and the next loader start chowns that tree to uid 1000. That is local root, and **it applies on Kettle today** (not gated by CHOWN_PLUGIN_PATH). Fix: create these dirs as the user (setpriv/fork + setuid), `O_NOFOLLOW`, never recursive chown.
- **W4. Incomplete privilege drop.** No `setgroups([])`/`initgroups`, so "unprivileged" plugins keep root's supplementary groups (localplatformlinux.py L137-157). Forked children also inherit the CSRF token, the :1337 listening fd and every loader module, so a non-root plugin can trivially become the loader. Fix: spawn a fresh interpreter (`subprocess`/`posix_spawn` + `close_fds`), `initgroups` for the user, pass only what's needed.
- **W5. Settings migration moves user files into root's dir.** `SettingsManager.__init__` renames every `~/homebrew/*.json` into the privileged settings dir, including a user-planted `loader.json` (which then stays user-owned and editable): store URL, `developer.rdt.ip` (remote JS into Steam), `cef_forward`, `disabled_plugins` (settings.py L13-27). Drop this migration; write settings atomically with fixed ownership.
- **W6. User-writable paths on the root loader's sys.path.** `get_system_pythonpaths` runs `python3` as the user and appends its `sys.path` (including user site-packages) to the root loader and therefore every root plugin (helpers.py L94-108, main.py L276). Fix: don't; give each plugin a fixed path list.
- **W7. Unix socket in /tmp with predictable name.** `/tmp/plugin_socket_<timestamp>` (localsocket.py L16), bound by the plugin after the uid drop: any uid-1000 process can connect and call methods of non-root plugins, pre-create the path (DoS), or win the bind race and impersonate the plugin to the loader (root loader trusts its responses). Fix: socketpair/inherited fd, or a 0700 per-plugin dir under `/run`.
- **W8. Open proxy and arbitrary HTTP.** `/fetch` and `utilities/http_request` reach any URL with any method, including localhost services (SSRF); `extra_opts` is splatted into aiohttp. Token sits in URLs (logs, Referer). Keep for compat but require the header form, refuse loopback/link-local unless whitelisted, log less.
- **W9. Unauthenticated static routes.** `/plugins/*/data/*` and `dist/*` bypass CSRF; paths are joined without containment checks (loader.py L141-158). Normalize, refuse `..`/absolute/symlink escapes, maybe require a per-plugin capability for `data/`.
- **W10. CSRF matching on raw `rel_url` strings** with prefix/regex lists (helpers.py L37-54). Match the normalized path against the router, not strings.
- **W11. Install hash is optional** (browser.py L58-64); `file://` artifacts accepted; zip extracted with `extractall` straight into `plugins/` (zip-slip is mitigated by zipfile but no layout check, so a zip can write any top-level dir name, including another plugin's).

Reliability:

- **R1. No method-call timeout and silent hangs**: non-JSON return values never answer; dead plugin processes leave calls pending (plugin.py L91-112, sandboxed_plugin.py L214). Wrap serialization in the try, add per-call timeouts and fail pending calls on EOF.
- **R2. Dead plugin -> probable busy loop.** On EOF the response listener reads `""`, `json.loads` fails into a bare `except: pass`, and loops; `readuntil` at EOF raises without yielding, so this likely spins the loader's event loop (plugin.py L91-112, localsocket.py L86-102). Verify; in any case detect exit (`proc.sentinel`/pidfd) and optionally restart with backoff.
- **R3. Uninstall race**: uninstall notice and SIGTERM are sent back to back; `_uninstall` may not run (plugin.py L156-161). Send the flag in the stop request itself and wait for an ack.
- **R4. Fixed 5 s kill budget** for `_unload`; children of the plugin are not killed (no process group/cgroup). Use a cgroup or process group per plugin and kill it.
- **R5. Events before connect are dropped**; `_migration` blocks the IPC server start (sandboxed_plugin.py L123-133), so a slow migration delays everything. Start the server first, queue early events.
- **R6. Non-atomic settings writes** (settings.py L51-52): crash = empty/garbled loader.json = lost order/disabled list.
- **R7. Name vs directory confusion**: duplicate names silently shadow; renaming `name` in an update orphans settings keyed by name. Detect and refuse duplicates.
- **R8. `multiprocessing` fork from a threaded process** (watchdog observer, aiohttp) is unsafe and breaks on Python 3.14's default forkserver.
- **R9. Single WS client** kicks the previous one; any local client can knock out the real frontend (wsrouter.py L89-96).
- **R10. Bugs to not replicate**: `start_ssh`/`stop_ssh` routes both call `allow_remote_debugging` (utilities.py L72-73); `filepicker_ls` name sort "asc" reverses (L363-369); legacy `execute_in_tab` returns an Exception object as `result` on failure (L245-249, unserializable).

## 10. Minimum compatibility checklist for the rewrite

1. Same paths and env vars (sections 1, 5), with the dir-name/plugin-name split.
2. Fork-equivalent semantics a plugin can observe: own process, uid 1000 (or root), `HOME`/`USER`, `py_modules` on `sys.path`, `decky` + `decky_plugin` modules, legacy top-level `helpers`/`settings`/`localplatform` aliases for api_version 0 plugins.
3. Lifecycle: `_migration` (awaited first), `_main` (task), `_unload`, `_uninstall`; api_version 0 class-as-self convention.
4. Calls: positional (v1) vs kwargs (v0), private-name refusal, error shapes per convention, JSON results.
5. Events: `loader/plugin_event {plugin, event, args}`.
6. HTTP on 127.0.0.1:1337: `/plugins/{name}/{dist,assets,data}/...`, `/plugins/{name}/frontend_bundle`, `/fetch`, `/ws`, `/frontend/`, `/locales/`, `/static/`, `/methods/`, plus a token mechanism the (unchanged) Decky frontend can still use (`/auth/token` today).
7. WS protocol with reply caching, FULL_SYNC, DISCARD, and every route/event in 6.2 the stock frontend calls.
8. `loader.json` format and keys (section 7), preserving unknown keys.
