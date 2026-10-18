# Packaging Remora

Remora replaces Decky Loader in place. Whatever installs it must keep Decky's drop-in contract
([ARCHITECTURE.md](../docs/ARCHITECTURE.md#drop-in-contract)):

- the unit is called **`plugin_loader.service`**, because plugins restart and stop the loader by
  that name
- HTTP and the WebSocket on `127.0.0.1:1337`, and Steam started with CEF remote debugging
  (`.cef-enable-remote-debugging` in Steam's folder), which Remora injects through
- `~/homebrew/{plugins,settings,data,logs}` stay where Decky had them, so plugins and their
  settings carry over

| File | What |
|---|---|
| `systemd/plugin_loader.service` | the unit: root, `Type=simple`, `Restart=always`, `KillMode=mixed`, `TimeoutStopSec=15`, runs `remora-start` |
| `remora-start` | the start script: finds the Game Mode user, makes its folders as that user, sets the CEF flag, runs `python3 -P -m remora serve` |
| `remora.env` | an example `/etc/remora/remora.env`, the settings `remora-start` reads |
| `requirements.txt` | the backend's dependencies, pinned with hashes (`uv pip compile backend/pyproject.toml --generate-hashes --universal`) |
| `arch/PKGBUILD` | an Arch package that installs into `/usr/lib/remora` |
| `steamos/install.sh`, `uninstall.sh` | install on SteamOS or Bazzite in place of Decky, and back |
| `steamos/make-tarball.sh` | builds the release tarball `install.sh` unpacks |

Kettle Linux's own package lives in the kettlelinux repository (`packages/remora`), not here.

## remora-start

It runs as root from the unit and:

1. reads `/etc/remora/remora.env` (refused unless it is root's and not writable by others)
2. picks the Game Mode user: `REMORA_USER` (a name or uid), else uid 1000
3. creates `~/homebrew/{settings,data,logs,services,plugins}` and the CEF flag files in
   `~/.local/share/Steam` and `~/.steam/steam` **as that user** (`setpriv`), never as root, so a
   link the user put there can't redirect a root write
4. exports `UNPRIVILEGED_USER`, `UNPRIVILEGED_PATH` (default `~/homebrew`) and `PRIVILEGED_PATH`
   (default the same), sets `PYTHONPATH` to the site folder and runs Python with `-P` from `/`

The site folder is `REMORA_SITE`, else `site/` next to the script (the SteamOS layout), else the
script's own folder (`/usr/lib/remora`, the distro layout). Python must be 3.11 or newer.

`PRIVILEGED_PATH` is where `plugins/` and `settings/loader.json` live. Left at `~/homebrew`, as
on a Steam Deck, the user can drop in a plugin with the `root` flag; Remora asks before it runs
a new root plugin, but a distro that wants more can point `PRIVILEGED_PATH` at a root-owned
folder (Kettle uses `/home/.kettle/decky`) and migrate `~/homebrew/plugins` there once.

## Arch (`arch/PKGBUILD`)

`makepkg` in `packaging/arch` builds the frontend with pnpm 10 and installs:

- `/usr/lib/remora/`: the `remora` package plus its dependencies from `requirements.txt`, as
  pure-Python wheels (hash-checked), so the package is `arch=any` and doesn't depend on the
  distro's `python-aiohttp`. Plugins import `aiohttp`, `certifi`, `watchdog` and
  `async_timeout` from there.
- `/usr/lib/remora/remora-start`, `/usr/lib/systemd/system/plugin_loader.service`,
  `/etc/remora/remora.env`, and `/usr/bin/remora` for `remora call` and `remora dev`

It `provides` and `conflicts` with `decky-loader`. After installing:
`sudo systemctl enable --now plugin_loader`.

## SteamOS and Bazzite (`steamos/`)

SteamOS's root filesystem is read-only and replaced on updates; `/home` and `/etc` persist. So,
like Decky's own installer, Remora goes into the user's home and its unit into `/etc`:

```sh
sudo bash install.sh                        # the latest release from GitHub
sudo bash install.sh --tarball remora.tar.gz [--sha256 HASH]
sudo bash uninstall.sh                      # Decky's service back, plugins untouched
```

`install.sh`:

1. downloads `remora.tar.gz` (and `remora.tar.gz.sha256`, checked if published) from the
   latest GitHub release, unless `--tarball` is given. That is the only network access.
2. checks the release imports on the system Python (3.11+)
3. stops `plugin_loader.service`, disables it if it is Decky's, and keeps Decky's unit file as
   `/etc/remora/plugin_loader.service.decky` (once; a reinstall doesn't overwrite it)
4. unpacks to `/home/.remora` (`--prefix` to change), owned by root, swapped in
   with a rename so a failed install leaves the old one; on SELinux (Bazzite) labels it `bin_t`
5. writes `/etc/remora/remora.env` if missing, installs Remora's unit as
   `/etc/systemd/system/plugin_loader.service` pointing at the new `remora-start`, enables it and
   (re)starts it

It never touches `~/homebrew/plugins`, `settings`, `data` or `logs`, or Decky's
`~/homebrew/services/PluginLoader`. Running it again updates Remora in place.

`uninstall.sh` stops and removes Remora's unit and install folder (only if it finds Remora's
files there), restores Decky's unit if it was kept and Decky's binary is still there, and enables
it. Plugins and settings stay. `--purge-config` also removes `/etc/remora`.

The release tarball (`make-tarball.sh`, `PIP="uvx pip"` if the system Python has no pip) is
`remora/{VERSION,LICENSE,remora-start,plugin_loader.service,site/}`, with `site/` holding only
pure-Python code: one tarball for x86_64 and aarch64, and any Python from 3.11 up. Publish
`remora.tar.gz`, `remora.tar.gz.sha256` and `install.sh` as release assets.

Known limits:

- The default `/home/.remora` is root's and sits under `/home`, so it survives SteamOS updates.
  Decky installs into `~/homebrew/services`, whose parent the user owns: the user could swap the
  folder for another one the root service then runs. Don't pass a `--prefix` inside the user's
  home for the same reason.
- `install.sh` and `uninstall.sh` haven't been run on a real SteamOS or Bazzite install yet.
