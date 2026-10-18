# Remora frontend ↔ backend protocol

The transport is Decky's (see [compat/frontend.md §4](compat/frontend.md)): one WebSocket at
`ws://127.0.0.1:1337/ws?auth=<token>`, JSON text frames, message types `-1 ERROR, 0 CALL,
1 REPLY, 2 DISCARD, 3 RECEIVED_RESPONSE, 4 FULL_SYNC, 5 EVENT`, reply caching until acked, and
FULL_SYNC replay on reconnect. Errors carry both `error.message` and `error.error`.

Call ids: the backend knows a call by its id *and* its route and arguments. A CALL that reuses an
id with another route or other arguments (a reloaded JS context starting again at 1) is a new
call, not a replay; the old call's reply, if it is still running, is dropped. A FULL_SYNC drops
every cached reply and every running call it doesn't list. The frontend should still start each
JS context's ids at a random base. A message that can't be handled (an unhashable id, a bad
`messages`) is skipped; the connection stays.

DISCARD means "the plugin was stopped on purpose" (disable, reload, uninstall, shutdown). A call to
a plugin that crashed, failed to start or gave up restarting gets an ERROR named `PluginStopped`
with the plugin's error text instead. A call that arrives while a crashed plugin waits for its
restart waits for it once.

The token reaches the frontend only through the CDP bootstrap, as `window.deckyAuthToken` (also
`window.remoraAuthToken`). There is no `GET /auth/token`: an `Origin` header proves nothing, any
local process can send one. When the loader restarts, the injector reloads Steam's JS context and
the new bootstrap carries the new token. Every HTTP request must have `Host: 127.0.0.1:1337` or
`localhost:1337`.

## Decky-compatible routes

These work exactly as in [compat/backend.md §6.2](compat/backend.md), because plugins call them through
the init hook and the legacy ServerAPI.

| Route | Args | Result |
|---|---|---|
| `loader/get_plugins` | | `[{name, version, load_type, disabled}]` in `pluginOrder`, then load order |
| `loader/reload_plugin` | `name` | `null`; the frontend then gets `loader/import_plugin` |
| `loader/call_plugin_method` | `name, method, ...args` | the return value; rejects on a Python exception |
| `loader/call_legacy_plugin_method` | `name, method, kwargs` | `{success, result}`; never rejects (errors, a stopped plugin, bad kwargs: `success: false`) |
| `utilities/ping` | | `"pong"` |
| `utilities/settings/get` | `key, default` | value |
| `utilities/settings/set` | `key, value` | value; any key starting with `remora.` is refused (ERROR `PermissionError`): see `remora/settings/set` |
| `utilities/get_user_info` | | `{username, path}` |
| `utilities/execute_in_tab` | `tab, run_async, code` | `{success, result}` |
| `utilities/inject_css_into_tab` | `tab, css` | css id |
| `utilities/remove_css_from_tab` | `tab, css_id` | `null` |
| `utilities/get_tab_id` | `name` | CDP target id |
| `utilities/http_request` | `method, url, extra_opts, timeout?` | `{status, headers, body}` |
| `utilities/_call_legacy_utility` | `name, kwargs` | `{success, result}` |
| `utilities/filepicker_ls` | `path, include_files, include_folders, include_ext, include_hidden, order_by, filter_for, page, max` | `{realpath, files:[{isdir, name, realpath, size, modified, created}], total}` |
| `utilities/enable_plugin` / `disable_plugin` | `name` | `null` |
| `utilities/set_all_plugins_disabled` | | `null` |
| `utilities/restart_webhelper` | | `null` |
| `utilities/install_plugin` | `artifact, name, version, hash, install_type` | `null`; emits `loader/add_plugin_install_prompt` |
| `utilities/install_plugins` | `[{artifact, name, version, hash, install_type}]` | `null`; emits `loader/add_multiple_plugins_install_prompt` |
| `utilities/confirm_plugin_install` | `request_id` | `null` |
| `utilities/cancel_plugin_install` | `request_id` | `null` |
| `utilities/uninstall_plugin` | `name` | `null` |
| `updater/get_version_info` | | `{current, remote: null, all: [], updatable: false, supported: true}` |

`install_type`: `0` install, `1` reinstall, `2` update, `3` downgrade, `4` overwrite.

Events (EVENT `event`, `args`):

| Event | Args |
|---|---|
| `loader/import_plugin` | `name, version, load_type, use_queue=true, timeout_ms=15000` |
| `loader/unload_plugin` | `name` |
| `loader/disable_plugin` | `name` |
| `loader/plugin_event` | `{plugin, event, args}` |
| `loader/add_plugin_install_prompt` | `name, version, request_id, hash, install_type` |
| `loader/add_multiple_plugins_install_prompt` | `request_id, [{name, version, hash, install_type}]` |
| `loader/plugin_download_start` | `name` |
| `loader/plugin_download_info` | `percent, message_key` |
| `loader/plugin_download_finish` | `name` |

## Remora routes

| Route | Args | Result |
|---|---|---|
| `remora/status` | | `{version, deckyVersion, plugins: [PluginStatus]}` |
| `remora/logs` | `name, max_lines=500` | the plugin's newest log file, as a string |
| `remora/permissions/list` | | `[{name, root, approved, source}]` for plugins that ask for root |
| `remora/permissions/set` | `name, approved` | `false`: revokes at once and stops the plugin, `{approved: false}`. `true`: asks for physical confirmation (blocks until confirmed or timed out), `{approved: bool, reason?}`, and starts the plugin when confirmed |
| `remora/permissions/confirm_mode` | `name?` | `{mode: "keys" \| "command" \| "terminal", hint}` |
| `remora/settings/set` | `key, value` | value; only `remora.storeTelemetry` and `remora.verbose` (booleans), anything else is ERROR `PermissionError` |
| `remora/store/sources` | | `[StoreSource]` |
| `remora/store/set_sources` | `[StoreSource]` | `[StoreSource]` |
| `remora/store/list` | `sort_by?, sort_direction?, query?` | `{sources: [{id, ok, error?}], plugins: [StorePlugin]}` |
| `remora/store/updates` | | `{<plugin name>: {version, hash, artifact, source}}` |
| `remora/store/answer_root` | `request_id, allow` | `null`; `true` only starts the device's own confirmation, it never approves by itself |
| `remora/store/rollback` | `name` | `{name, version}`: puts the kept previous version back (the current one becomes the kept one); RouteError `FileNotFoundError` when none is kept |
| `remora/store/previous` | `name` | `{version, source}` of the kept previous version, or `null` |

```
PluginStatus = {
  name, dir, version, loadType,            // loadType: 0 legacy IIFE, 1 ESM
  state,                                    // "running" | "starting" | "stopped" | "crashed"
                                            // | "disabled" | "passive" | "blocked" (root not approved)
  restarts,                                 // crash restarts since the loader started
  error,                                    // last error text or null
  root, approved                            // asks for root; the user allowed it
}
StoreSource = { id, name, kind: "decky" | "index", url, enabled, builtin }
StorePlugin = Decky's store entry ({id, name, author, description, tags, image_url,
              versions: [{name, hash, artifact, created}]}) plus source: <StoreSource id>
```

Remora events:

| Event | Args |
|---|---|
| `remora/plugin_status` | `PluginStatus` (whenever it changes) |
| `remora/root_prompt` | `request_id, name, version, mode, hint`: a plugin being installed (or rolled back) asks for root and has no approval from this source. `mode`/`hint` as `remora/permissions/confirm_mode`. Answer with `remora/store/answer_root`: `false` cancels; `true` runs the device confirmation (`remora/confirm_waiting` / `remora/confirm_done`, same `request_id`) and the install goes on only if it succeeds. With no answer in 5 minutes, the install is cancelled. Asked again whenever the source differs from the approval's, or is unknown. |
| `remora/confirm_waiting` | `request_id \| null, name, mode, hint, until`: the backend waits for physical confirmation until `until` (epoch seconds). Show the hint and a countdown; Cancel only closes the UI. |
| `remora/confirm_done` | `request_id \| null, name, ok` |
| `remora/install_failed` | `name, message` |
| `remora/install_request` | `request_id, [{name, version, hash, install_type, source, signed, compatible, root}]`: sent right after `loader/add_plugin_install_prompt` / `loader/add_multiple_plugins_install_prompt`, with what the store knows about each zip (matched by name + hash; `source: null` when no enabled source lists it, `root: null` when unknown). |

## Store details

- `StoreSource` may carry `keys: [{id, alg: "ed25519", pub}]` (base64 raw public keys) for an
  `index` source. Pinned keys replace the ones the index lists in `source.keys`. Built-ins
  (`decky`, `decky-testing`) always stay in the list; `set_sources` only switches them on or off.
  Custom sources without an `id` get one from their name.
- Transport: `https` only. Plain `http` is accepted for an `index` source with pinned `keys`, or in
  developer mode; `file://` only for an `index` source in developer mode. Both are checked again at
  every fetch, so they stop working when developer mode goes off. `file://` must be a regular file.
  TLS uses the system trust store (certifi's bundle only when that is empty).
- URLs: a `decky` source's API base is `url` if its path ends in `/plugins`, else `url + "/plugins"`.
  An `index` source fetches `url` if its path ends in `.json`, else `url + "/index.json"`; relative
  `artifact` URLs resolve against it, and `file://` is accepted only in developer mode.
- `StorePlugin.versions[]` gain `signed: true | "self" | false | "invalid"` and `compatible: bool` (plus
  `incompatible_reason` when false); index extension fields (`size`, `arch`, `min_loader`,
  `flags`, ...) pass through. `decky` sources fill a missing `artifact` with Decky's CDN URL;
  `index` versions without `artifact` have `artifact: null` and can't be installed.
- Signatures: `sig.value` is an ed25519 signature over the UTF-8 bytes
  `"decky-index/1\n" + name + "\n" + version + "\n" + lowercase sha256`. With keys present, a version
  with no signature, an unknown key or a bad signature is `"invalid"` and refused at install.
  `true`: signed by a key the user pinned (`StoreSource.keys`). `"self"`: signed by a key the index
  lists itself, which proves nothing (whoever changes the index changes its keys); shown as
  unverified. `false`: unsigned, or self-listed keys and no `cryptography` module. Pinned keys
  without `cryptography` make every version `"invalid"`. The signature covers name, version and
  hash only, not `flags`/`arch`/`min_loader` (the root flag that counts is the zip's plugin.json,
  which the hash covers). Malformed plugins or versions are skipped one by one; `downloads` is
  always an int and `tags` a list of strings.
- Lists are cached 5 minutes, failures 1 minute. An install whose hash no cached list has refetches
  lists older than 30 s, at most once per request. Updates offer the highest compatible, non-invalid
  version by semver.
- Downloads: 30 s to connect, 15 minutes overall, and aborted when slower than 10 KB/s over 30 s.
  `utilities/cancel_plugin_install(request_id)` also stops a confirmed install that is still running
  (`remora/install_failed(name, "cancelled")`); the old version stays or comes back. Once the swap
  has started it finishes (or rolls back) before the cancel takes effect.
- Kept files in plugins/: an update keeps the old version as `.prev-<dir>` (with `.prev-<dir>.json`:
  `{name, version, source}`) until the next update or the uninstall. At startup, `.staging-*`
  folders are removed and an `.old-<tag>-<dir>` left by a crash goes back to `<dir>` when that is
  missing (else it becomes `.prev-<dir>`).
- Installs need a hash; only developer mode (`developer.enabled`) installs without one (and then the
  plugin name comes from the zip). Incompatible versions (`arch`, `min_loader`) are refused unless
  developer mode is on.
- `loader/plugin_download_info` message keys: `Remora.install.downloading`, `.verifying`,
  `.unpacking`, `.binaries`, `.installing`. `loader/plugin_download_finish` is sent after failures
  too, following `remora/install_failed`.
- `remora/root_prompt` uses its own `request_id`, not the install request's.
- Settings keys: `remora.storeSources` (`[StoreSource]`), `remora.installSource` (`{name: source id}`),
  `remora.storeTelemetry` (bool, default false: Decky's install counter POST, to `decky` sources
  only), `remora.verbose` (bool, the UI's extra logging), `remora.rootApproved`
  (`[{name, source}]`; the old list of names reads as approvals of each plugin's recorded source).
  Uninstall also drops the plugin's root approval and its kept previous version.

## Root approval

Anything running as the Game Mode user can drive Steam's UI through CEF devtools, so a click in
the UI can't approve a root plugin. Approval needs a channel that process can't drive
(`REMORA_CONFIRM`):

- `command`: `$REMORA_CONFIRM_COMMAND "<what>"` run as root, exit 0 confirms (Kettle:
  `/usr/lib/kettle/confirm-keys`).
- `keys`: Volume Up + Volume Down held together for a second within 30 s, on platform inputs only
  (not uinput, USB or Bluetooth), pressed after the request. `REMORA_CONFIRM_KEYS` (space
  separated) limits the input names.
- `terminal`: `sudo remora approve <name>` within 5 minutes.
- `auto` (default): `command` if the command is set and executable, else `keys` if the device has
  platform volume keys, else `terminal`.

Root on a terminal can always answer, whatever the mode: `remora approve [name|id]` (no argument
lists what waits), `remora deny <name|id>` (denies a waiting request, or revokes an approval),
`remora permissions` (lists approvals). They talk to the root-only control socket
`/run/remora/control.sock` (`REMORA_CONTROL_SOCKET`; folder 0700, socket 0600, peer uid
checked), one JSON line per request: `{"cmd": "pending" | "list"}`,
`{"cmd": "approve" | "deny", "target": <id or name>}`. `remora approve <name>` with nothing
pending approves an installed root plugin and starts it.

An approval is for a source: `{name, source}` (`source: null` for local and migrated plugins). A
root plugin installed or updated from another source than its approval's, or from no known
source, asks again. One confirmation runs at a time; the next waits its turn.
