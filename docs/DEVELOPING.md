# Working on Remora

This is about changing Remora itself. For writing a plugin, see [sdk/README.md](../sdk/README.md).

Read [ARCHITECTURE.md](ARCHITECTURE.md) first, and the specs in [compat/](compat/README.md) for
what Remora must stay compatible with. Remora is a clean-room implementation: don't read or copy
Decky Loader's source (GPL-2.0). Work from the specs, and from plugins' and Steam's observable
behaviour.

## Layout

```
backend/remora/        the loader (Python 3.11+)
  app.py               wiring; cli.py the `remora` command
  server.py            HTTP on :1337: auth, CORS, file routes, /fetch
  wsrouter.py          the frontend WebSocket protocol
  utilities.py         loader/*, utilities/*, updater/*, remora/* routes
  plugins/             discovery (manifest.py), the per-plugin supervisor (process.py), manager.py
  host/                the per-plugin process that imports main.py
  compat/              the decky, decky_plugin, settings, helpers, localplatform, injector modules
  cdp.py               Steam's CEF devtools: injection, execute_in_tab
  fsutil.py            walking user-owned folders without following links
  store/               the plugin store; compat.py and elf.py: plugin compatibility
  device.py            the device facts compat lists name
  data/compat.json     the shipped compat list
backend/tests/         pytest
frontend/              TypeScript injected into Steam; built into backend/remora/static
sdk/                   decky.pyi and the plugin developer guide
tools/compat-scan.py   scans a Decky store for plugins that can't run on ARM (see below)
packaging/             systemd unit, start script, Arch PKGBUILD, SteamOS installer
```

## Backend setup

With [uv](https://docs.astral.sh/uv/):

```sh
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e 'backend[dev]'
```

or with plain pip: `python3.11 -m venv .venv && .venv/bin/pip install -e 'backend[dev]'`.

This installs the `remora` command into `.venv/bin`. Python 3.11 is what SteamOS and Kettle ship;
keep the code working there (CI also runs 3.12).

## Tests

```sh
cd backend && ../.venv/bin/python -m pytest -q
```

The suite takes about 15 seconds. It starts real plugin hosts as subprocesses and real aiohttp
servers on free ports, all under pytest's `tmp_path`, so it needs no root, no Steam and no
network. Shared fixtures are in `backend/tests/conftest.py`:

- `cfg`: a `Config` whose home, `~/homebrew` and plugins folder live in `tmp_path`, on a free port
- `make_plugin(dir, name, main_py, api_version=, flags=, files=)`: writes a plugin folder
- `manager` / `started()`: a `PluginManager` with a recording WebSocket (`ws.events`)
- `fast_restarts`: crash restart delays of 0.1 and 0.2 s instead of 1 to 30 s
- `wait_for(predicate)`: poll instead of sleeping a fixed time

| File | Covers |
|---|---|
| test_wsrouter.py | CALL/REPLY/ERROR, ack, FULL_SYNC replay and stale drop, DISCARD, events |
| test_host.py | real hosts: both calling conventions, lifecycle hooks, events, crashes and restarts, compat imports, live reload, `remora call` |
| test_server.py | Host check, auth (and no `/auth/token`), CORS, file routes and their link and `..` refusals, `/fetch` |
| test_fsutil.py | `O_NOFOLLOW` walks, the root-only branch with `geteuid` and `fchown` faked |
| test_settings.py | loader.json atomic writes, unknown keys, broken files; legacy `SettingsManager` |
| test_compat_decky.py | the `decky` module in a fresh interpreter: constants, `migrate_*`, logging |
| test_store*.py | the store |

Tests that need root are skipped unless you run as root. Don't add fixed `sleep`s where a
`wait_for` works; the suite must stay fast and must not flake on a slow CI runner.

## Frontend

```sh
cd frontend
npx --yes pnpm@10 install --frozen-lockfile
npx --yes pnpm@10 run typecheck
npx --yes pnpm@10 run build      # writes the bundle the loader serves at /frontend/
```

## Running the loader locally

As your own user, on a spare port, without touching Steam:

```sh
export UNPRIVILEGED_PATH=$PWD/.scratch/home PRIVILEGED_PATH=$PWD/.scratch/home SERVER_PORT=13370
mkdir -p .scratch/home/plugins
.venv/bin/remora serve --no-inject
```

Put plugin folders in `.scratch/home/plugins/` (or pass `--plugins DIR`). Settings, data and logs
go under `.scratch/home/`. When Remora isn't root it warns that every plugin, root ones included,
runs as you; that's expected here.

To drive it the way the frontend does, open the WebSocket with the token. Only the CDP bootstrap
hands the token to Steam; there is no HTTP route for it. Run `remora dev` instead of `remora serve`:
it logs the WebSocket URL with the token at startup. Take `token` from there, then send
`{"type": 0, "id": 1, "route": "loader/get_plugins", "args": []}` frames to
`ws://127.0.0.1:13370/ws?auth=$token`, acking each reply with `{"type": 3, "id": 1}`
(see [PROTOCOL.md](PROTOCOL.md)). A quick smoke run worth doing after backend changes:

1. a modern plugin (`api_version: 1`): `loader/call_plugin_method` with positional args, a
   method that raises (ERROR with both `message` and `error`), one that returns `object()`
   (an error, not a hang), a `decky.emit` (a `loader/plugin_event` EVENT)
2. a legacy plugin (no `api_version`): `loader/call_legacy_plugin_method` with kwargs, and
   `import decky_plugin`, `from settings import SettingsManager`, `import helpers` in its main.py
3. a plugin with a method that calls `os._exit(3)`: the call is DISCARDed, `remora/status` shows
   it crashed, and it answers again a second later
4. `utilities/disable_plugin` and `enable_plugin`, `loader/reload_plugin`
5. `GET /plugins/<name>/dist/index.js` (200), `.../dist/..%2Fplugin.json` (404), the
   `frontend_bundle` route without `X-Decky-Auth` (403), a request with `Host: evil:13370` (421)

`remora dev <plugin-dir>` and `remora call <plugin-dir> <method> [args]` (see the SDK guide) are
the shortest way to run one plugin.

## Against Steam

On a device (or a desktop Steam started with `-gamepadui` and CEF debugging), stop the installed
loader first, since Remora needs port 1337, port 8080's devtools and Steam's JS context to itself:

```sh
sudo systemctl stop plugin_loader
touch ~/.steam/steam/.cef-enable-remote-debugging   # then restart Steam once
sudo -E .venv/bin/remora serve                       # or without sudo: everything runs as you
```

`sudo systemctl start plugin_loader` puts the installed one back.

## Compat lists

`tools/compat-scan.py` reads the newest version of every plugin in a Decky-format store, in
memory (nothing is unpacked or run; `.py` files are only parsed), and writes `requires`
entries for used x86-only native code, x86-only `remote_binary` downloads and Python imports
nothing provides, with `upto` set to the scanned version. It merges into an existing list and
keeps hand-written fields; the `scan` record in each entry is what it wrote last time.

```sh
python3 -I tools/compat-scan.py -o new-compat.json --zips ~/store-zips   # downloads into --zips
python3 -I tools/compat-scan.py -o new-compat.json --merge backend/remora/data/compat.json \
    --offline --plugins plugins.json --zips ~/store-zips                  # no network
```

Review the result before it goes into `backend/remora/data/compat.json`: the "used" rule for
binaries is a heuristic (the file name appearing in the plugin's code), and so are imports.

## Packaging

[packaging/README.md](../packaging/README.md) covers the systemd unit, the start script, the Arch
package and the SteamOS installer.

## Pull requests

- `cd backend && ../.venv/bin/python -m pytest -q` passes, and new behaviour has a test
- frontend: `typecheck` and `build` pass
- a change to a Decky-compatible behaviour cites the spec section in `docs/compat/` it follows,
  or updates the spec
