# Developing a plugin with Remora

Remora runs Decky Loader plugins unchanged, so a plugin you write for Remora is a Decky plugin:
the same `plugin.json`, `package.json`, `main.py`, `dist/index.js` and `@decky/api`. Start from
[decky-plugin-template](https://github.com/SteamDeckHomebrew/decky-plugin-template) or any
existing plugin. This folder adds what Remora has on top: typed stubs, a harness that calls your
backend without Steam, and a dev loader that reloads your plugin as you edit it.

You need a checkout of Remora with its backend installed (see
[docs/DEVELOPING.md](../docs/DEVELOPING.md)), or Remora installed on the device. All commands
below run as your own user. None of them needs root, a Steam Deck or Steam.

## Plugin layout

```
my-plugin/
  plugin.json        {"name": "My Plugin", "author": "...", "flags": [], "api_version": 1}
  package.json       {"name": "my-plugin", "version": "1.0.0", "type": "module", ...}
  main.py            the backend (optional: without it the plugin is frontend-only)
  py_modules/        on the backend's sys.path
  dist/index.js      the built frontend
  dist/assets/       served at http://127.0.0.1:1337/plugins/My%20Plugin/assets/...
```

- `name` in plugin.json is the plugin's identity: routes, events, the frontend's `call()`.
  The folder name decides where its files go: `~/homebrew/{settings,data,logs}/<folder>`.
- `api_version: 1` (or newer) is the modern convention: `class Plugin` is instantiated once and
  methods get positional arguments from `call("method", a, b)`. Without it (version 0), methods
  are called on the class itself with keyword arguments from `callPluginMethod("method", {a, b})`.
- `flags`: `"root"` runs the backend as root, which the user has to approve in Remora;
  `"debug"` makes the regular loader reload the plugin when its files change.

## The backend

```python
import asyncio
import decky


class Plugin:
    async def _main(self):          # runs as a task once the plugin is loaded; may loop forever
        decky.logger.info("started")

    async def add(self, left: int, right: int) -> int:   # call("add", 1, 2) from the frontend
        await decky.emit("added", left + right)          # addEventListener("added", ...)
        return left + right

    async def _unload(self):        # when the plugin stops (5 s budget, then it is killed)
        pass

    async def _uninstall(self):     # after _unload, only when the user uninstalls it
        pass

    async def _migration(self):     # before anything else, e.g. decky.migrate_settings(...)
        pass
```

Methods starting with `_` can't be called from the frontend. Return values must be JSON: a value
that isn't comes back to the frontend as an error. Each call runs as its own task, so blocking
code (subprocess.run, big file reads) belongs in `await asyncio.to_thread(...)`, or it holds up
every other call. Remora also accepts plain `def` methods and runs them in a thread; Decky
refuses them, so use `async def` if the plugin should run on both.

What differs from Decky, in ways a plugin can notice:

- Non-root plugins run as the Game Mode user with that user's groups, in a fresh process. They
  don't inherit the loader's memory, and `helpers.get_csrf_token()` returns `""`.
- A backend that crashes is restarted (after 1, 2, 5, 10 and 30 s, then it stays down until
  reloaded), and calls in flight fail instead of hanging.
- `_migration` doesn't block calls from being queued; they wait until it is done.
- Children your backend starts are killed with it (its process group), once `_unload` returns.

### Types

`sdk/decky.pyi` describes the `decky` module. Point your editor at this folder:

```jsonc
// pyrightconfig.json, or "python.analysis.extraPaths" in VS Code
{ "extraPaths": ["/path/to/remora/sdk"] }
```

```ini
# mypy.ini
[mypy]
mypy_path = /path/to/remora/sdk
```

For the frontend, use `@decky/api` and `@decky/ui` from npm, as with Decky.

## Call your backend without Steam: `remora call`

```sh
remora call ./my-plugin add 1 2            # prints 3
remora call ./my-plugin get_config         # any JSON comes back pretty-printed
remora call ./my-plugin set_name '"Bob"'   # each argument is JSON; a bare word is a string
remora call ./my-plugin save '{"a": [1, 2]}' --compact
remora call ./old-plugin greet who=world   # api_version 0: keyword arguments
remora call ./my-plugin greet '{"who": "world"}' --legacy
```

It starts only your plugin's backend, the way the loader would (same environment, `decky`
module, `py_modules`, lifecycle), runs `_migration`, starts `_main`, makes the one call, prints
the result as JSON on stdout and stops the backend, which runs `_unload`. Logs, plugin output
and `decky.emit` events go to stderr, so the output can be piped into `jq`. The exit status is 0
on success, 1 if the method raised (its traceback is printed) or the backend died, 2 for a bad
plugin folder.

Options: `--wait SECONDS` gives `_main` time before the call, `--timeout SECONDS` (default 60),
`--home DIR` picks the folder that stands in for `~/homebrew`.

Settings, data and logs go to `~/.cache/remora-dev/<folder>/{settings,data,logs}/<folder>`, so
the harness never touches the plugin's real files. Pass `--home` to use another folder, for
example one you prepared with fixture files.

It also makes a simple test runner:

```sh
test "$(remora call ./my-plugin add 1 2)" = 3
```

## A loader for your plugin only: `remora dev`

```sh
remora dev ./my-plugin                 # on 127.0.0.1:1337, no injection into Steam
remora dev ./my-plugin --port 13370    # next to an installed loader
remora dev ./my-plugin --inject        # inject the Remora frontend into a running Steam
```

This runs the whole loader as you, with your plugin and nothing else: the HTTP routes, the
WebSocket, the file routes for `dist/` and `data/`, crash restarts. It watches the plugin folder
and reloads the backend and tells the frontend to reload the plugin when `main.py`, any `.py`
file (outside `node_modules`) or `dist/index.js` changes. The `debug` flag isn't needed. Run your
frontend build in watch mode (`pnpm run watch` in the template) next to it.

The log prints the WebSocket URL with this run's token, for a script or test client that wants
to call routes like `loader/call_plugin_method` directly (see [docs/PROTOCOL.md](../docs/PROTOCOL.md)).

With `--inject` it needs Steam running with CEF remote debugging (the file
`~/.steam/steam/.cef-enable-remote-debugging` at Steam's start), and the regular loader stopped
(`sudo systemctl stop plugin_loader`), since both want port 1337 and Steam's JS context. Plugin
bundles hardcode `http://127.0.0.1:1337` for their assets, so keep the default port when
injecting.

## On a device

To try the plugin in the installed loader, copy (or link) its folder into the plugins folder
(`~/homebrew/plugins`, or Kettle's `/home/.kettle/decky/plugins`) and reload it from Remora's
plugin list, or add `"debug"` to its flags while you work on it so it reloads on change.
