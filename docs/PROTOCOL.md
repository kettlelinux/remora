# Remora frontend ↔ backend protocol

The transport is Decky's (see [compat/frontend.md §4](compat/frontend.md)): one WebSocket at
`ws://127.0.0.1:1337/ws?auth=<token>`, JSON text frames, message types `-1 ERROR, 0 CALL,
1 REPLY, 2 DISCARD, 3 RECEIVED_RESPONSE, 4 FULL_SYNC, 5 EVENT`, reply caching until acked, and
FULL_SYNC replay on reconnect. Errors carry both `error.message` and `error.error`.

The token reaches the frontend through the CDP bootstrap as `window.deckyAuthToken` (also
`window.remoraAuthToken`). `GET /auth/token` still answers, but only to requests whose `Origin` is
`https://steamloopback.host`. Every HTTP request must have `Host: 127.0.0.1:1337` or
`localhost:1337`.

## Decky-compatible routes

These work exactly as in [compat/backend.md §6.2](compat/backend.md), because plugins call them through
the init hook and the legacy ServerAPI.

| Route | Args | Result |
|---|---|---|
| `loader/get_plugins` | | `[{name, version, load_type, disabled}]` in `pluginOrder`, then load order |
| `loader/reload_plugin` | `name` | `null`; the frontend then gets `loader/import_plugin` |
| `loader/call_plugin_method` | `name, method, ...args` | the return value; rejects on a Python exception |
| `loader/call_legacy_plugin_method` | `name, method, kwargs` | `{success, result}` |
| `utilities/ping` | | `"pong"` |
| `utilities/settings/get` | `key, default` | value |
| `utilities/settings/set` | `key, value` | value |
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
| `remora/permissions/list` | | `[{name, root, approved}]` for plugins that ask for root |
| `remora/permissions/set` | `name, approved` | `null`; restarts the plugin |
| `remora/store/sources` | | `[StoreSource]` |
| `remora/store/set_sources` | `[StoreSource]` | `[StoreSource]` |
| `remora/store/list` | `sort_by?, sort_direction?, query?` | `{sources: [{id, ok, error?}], plugins: [StorePlugin]}` |
| `remora/store/updates` | | `{<plugin name>: {version, hash, artifact, source}}` |
| `remora/store/answer_root` | `request_id, allow` | `null` |

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
| `remora/root_prompt` | `request_id, name, version`: a plugin being installed asks for root. Answer with `remora/store/answer_root`. With no answer in 5 minutes, the install is cancelled. |
| `remora/install_failed` | `name, message` |
| `remora/install_request` | `request_id, [{name, version, hash, install_type, source, signed, compatible, root}]`: sent right after `loader/add_plugin_install_prompt` / `loader/add_multiple_plugins_install_prompt`, with what the store knows about each zip (matched by name + hash; `source: null` when no enabled source lists it, `root: null` when unknown). |

## Store details

- `StoreSource` may carry `keys: [{id, alg: "ed25519", pub}]` (base64 raw public keys) for an
  `index` source. Pinned keys replace the ones the index lists in `source.keys`. Built-ins
  (`decky`, `decky-testing`) always stay in the list; `set_sources` only switches them on or off.
  Custom sources without an `id` get one from their name.
- URLs: a `decky` source's API base is `url` if its path ends in `/plugins`, else `url + "/plugins"`.
  An `index` source fetches `url` if its path ends in `.json`, else `url + "/index.json"`; relative
  `artifact` URLs resolve against it, and `file://` is accepted only in developer mode.
- `StorePlugin.versions[]` gain `signed: true | false | "invalid"` and `compatible: bool` (plus
  `incompatible_reason` when false); index extension fields (`size`, `arch`, `min_loader`,
  `flags`, ...) pass through. `decky` sources fill a missing `artifact` with Decky's CDN URL;
  `index` versions without `artifact` have `artifact: null` and can't be installed.
- Signatures: `sig.value` is an ed25519 signature over the UTF-8 bytes
  `"decky-index/1\n" + name + "\n" + version + "\n" + lowercase sha256`. With keys present, a version
  with no signature, an unknown key or a bad signature is `"invalid"` and refused at install.
  `false` means unsigned, or signed but the `cryptography` module is missing.
- Installs need a hash; only developer mode (`developer.enabled`) installs without one (and then the
  plugin name comes from the zip). Incompatible versions (`arch`, `min_loader`) are refused unless
  developer mode is on.
- `loader/plugin_download_info` message keys: `Remora.install.downloading`, `.verifying`,
  `.unpacking`, `.binaries`, `.installing`. `loader/plugin_download_finish` is sent after failures
  too, following `remora/install_failed`.
- `remora/root_prompt` uses its own `request_id`, not the install request's.
- Settings keys: `remora.storeSources` (`[StoreSource]`), `remora.installSource` (`{name: source id}`),
  `remora.storeTelemetry` (bool, default false: Decky's install counter POST, to `decky` sources
  only). Uninstall also drops the plugin from `remora.rootApproved`.
