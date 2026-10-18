# Decky store, install and update flow: compatibility spec

Clean-room description of how Decky Loader talks to its plugin store, installs and
updates plugins, how plugin zips are produced and published, how the loader updates
itself, and how it is installed on SteamOS. Written for a plugin-compatible
replacement with an open, multi-source store. No code is copied; behaviour is
described in prose with file:line pointers.

Reference trees (read-only), all under
`decky-ref/`:

| tree | commit | notes |
|---|---|---|
| `decky-loader` | 67196ad (2026-10-17, = v3.3.0-pre1) | `DL/` below |
| `decky-plugin-database` | a8e6d7f (2026-10-14) | `DB/` below, AGPL, 121 submodules |
| `cli` | 6c32da7 (v0.0.8; store CI pins 0.0.7) | `CLI/` below |
| `decky-plugin-template` | 90d0780 (shallow, no `.github/` in this checkout) | `TPL/` below |

Live observations were made with GET (plus one CORS preflight OPTIONS) against
`plugins.deckbrew.xyz`, `testing.deckbrew.xyz` and the CDN on 2026-10-18 01:50-01:55 UTC.
Samples saved in `build/claude-scratch/decky-spec/api/` (`plugins.json`,
`versions.json`, `testing.json`, `openapi.json`, two downloaded zips in `dl/`).
The store server's own source is not in the reference set; its contract comes
from the loader code, the database CI, and the server's published `openapi.json`
(FastAPI).

---

## 0. Urgent finding: the live store rejects every loader older than v3.3.0

At 2026-10-18 01:55 UTC both `plugins.deckbrew.xyz` and `testing.deckbrew.xyz`
answer **HTTP 426, body `Unsupported loader version, please upgrade.`** to
`GET /plugins` and `GET /plugins/versions` when the `X-Decky-Version` header
holds a version below `v3.3.0` (tested: v2.0.0 through v3.2.10 → 426;
v3.3.0-pre0, v3.3.0-pre1, v3.3.0, v4.0.0 → 200). A missing header, or a value
that does not parse (`dev`, `unknown`, `garbage`), gets 200.

v3.3.0-pre1 came out at 01:21 UTC the same night, and v3.2.10 is still the
newest **stable** release. So either the gate went up too early or this is
deliberate. Kettle ships v3.2.10 (`packages/decky-loader/PKGBUILD:9`, and the
version reaches the header through the dist-info METADATA written at
`PKGBUILD:52-55`), so **the Decky store on Kettle images is broken as of
tonight**: the store list fails, and so do update checks. A replacement
must either send a version the server accepts or leave the header out, and it
must show a 426 to the user as "the store requires a newer loader" instead of
failing on a JSON parse error, which is what upstream does
(`DL/frontend/src/store.tsx:94-99` calls `r.json()` with no status check).

---

## 1. Store HTTP API

### 1.1 Base URL selection

The loader stores a numeric `store` setting and a `store-url` string in
`loader.json` (frontend `DL/frontend/src/store.tsx:6-10,50-79`; backend copy of the
same switch in `DL/backend/decky_loader/browser.py:217-222`):

| `store` | meaning | base URL |
|---|---|---|
| 0 (default, also any unknown value) | Default | `https://plugins.deckbrew.xyz/plugins` |
| 1 | Testing | `https://testing.deckbrew.xyz/plugins` |
| 2 | Custom | value of `store-url` (default the official URL) |

If `store` is unset, the frontend writes 0 (`store.tsx:58-62`). Choosing Custom
shows a third-party warning modal with a 5 s countdown until
`store_select.warn.third_party` is accepted once
(`DL/frontend/src/components/settings/pages/general/StoreSelect.tsx:15-18,45-55`).
Only one store can be active at a time.

The base URL **includes** the `/plugins` path segment; every endpoint below is
relative to it. A custom store must therefore serve `<base>`, `<base>/versions`,
and `<base>/<name>/versions/<version>/increment`.

### 1.2 Request conventions

* All store reads are made **by the frontend** (`fetch` from the Steam CEF page),
  not the Python backend. The page's origin is `https://steamloopback.host`, and
  the custom header forces a CORS preflight. The official server answers the
  preflight with `Access-Control-Allow-Origin: https://steamloopback.host`,
  `Access-Control-Allow-Headers: x-decky-version`, credentials allowed, max-age 600.
  **A custom store that leaves out CORS does not work.**
* Header `X-Decky-Version: <loader version>` on list, versions and increment
  (`store.tsx:96-98,108-110`; `browser.py:225-227`). The version string is shaped
  `vMAJOR.MINOR.PATCH[-preN][-devN]`, or `dev` / `unknown`
  (`DL/backend/decky_loader/helpers.py:70-90`). The server gates on it (see §0).
* Neither side sends authentication. The backend uses aiohttp with a certifi SSL
  context (`helpers.py:31-32`). A `Decky/<ver>` user agent is defined
  (`helpers.py:92`) but these calls do not send it.

### 1.3 `GET <base>` — plugin list

Called by the store page (`store.tsx:81-100`, `components/store/Store.tsx:91-98`).

Query parameters (the loader only ever sends the first two; the rest come from the
server's OpenAPI and were confirmed live):

| param | values | sent by loader | server behaviour |
|---|---|---|---|
| `sort_by` | `name` \| `date` \| `downloads` | yes | an invalid value gives HTTP 500 |
| `sort_direction` | `asc` \| `desc` (server default `asc`) | yes | |
| `query` | substring (case-sensitive: `CSS` hits, `css` misses) | no | the loader searches client-side |
| `tags` | repeatable | no | `tags=root` → 20 plugins |
| `hidden` | bool, default false | no | `true` adds the `visible:false` entries (110 → 130) |

The UI offers six sort combinations, default `name/asc`. The alphabetical labels
are swapped on purpose, for the sake of existing translations (`Store.tsx:69-85`).
The UI filters client-side by case-insensitive substring over name, description,
author and tags (`Store.tsx:233-240`).

Response: a JSON array of plugin objects. Live schema (110 entries tonight;
`openapi.json` `ListPluginResponse`):

| field | type | used by loader | meaning |
|---|---|---|---|
| `id` | int | typed only (`store.tsx:32`) | store DB id |
| `name` | string | **yes, the key** | must equal `plugin.json` `name` of the installed plugin; used for updates, increment path, install folder lookup |
| `author` | string | display | from `plugin.json` `author` |
| `description` | string | display + search | from `plugin.json` `publish.description` |
| `tags` | string[] | display + search; `root` tag shows a warning box linking deckbrew.xyz/root (`PluginCard.tsx:37,130-145`) | from `plugin.json` `publish.tags` |
| `image_url` | string (absolute URL) | display, 320x200 box (`PluginCard.tsx:51-60`) | re-hosted copy of `publish.image` |
| `versions` | array, **newest first** | yes | see below |
| `visible` | bool | no | always true unless `hidden=true` |
| `downloads` | int | no | sum of fresh installs |
| `updates` | int | no | sum of update installs |
| `created` / `updated` | ISO-8601 UTC string (`2026-10-07T19:45:20Z`) | no | first and last version upload |

Version object (`PluginVersionResponse`):

| field | type | used | meaning |
|---|---|---|---|
| `name` | string | yes | the version string, which is `package.json` `version` (CI: `DB/.github/workflows/build-plugins.yml:347`) |
| `hash` | string, 64 hex | yes | lowercase SHA-256 of the zip; doubles as the artifact's file name |
| `created` | ISO-8601 | no | |
| `downloads` / `updates` | int | no | per-version counters |
| `artifact` | string \| null | **optional, loader-only** | the loader's type allows an explicit zip URL (`store.tsx:23-27`); the official server never sends it |

The loader treats array order as release order. Picking an index below the
installed version's index means UPDATE; the same index means REINSTALL; a higher
index means DOWNGRADE; installed but not in the list means OVERWRITE; otherwise
INSTALL (`PluginCard.tsx:24-35`). The store must keep `versions` newest-first.

### 1.4 `GET <base>/versions` — update map

Called by `checkForPluginUpdates` (`store.tsx:102-113,137-161`). Response: a JSON
object whose keys are plugin **names** and whose values are tuples
`[version, hash]`, optionally `[version, hash, artifactURL]`
(`store.tsx:29`). Live: 130 keys, so it includes plugins that are not `visible`
(unlisted plugins still receive updates), and no entry has a 3rd element.

Update rule: a remote version counts as an update only if it differs from the
installed string, **both parse as semver** (`compare-versions` `validate`),
and remote > installed (`store.tsx:146-152`). An installed version that
is missing is treated as `0.0.0` (`store.tsx:142-144`). Frozen plugins are left out
of the check (`DL/frontend/src/plugin-loader.tsx:277-285`). Consequences: a
non-semver version never updates; testing-store versions like `1.2.3-abc1234`
are semver prereleases, so they sort *below* `1.2.3`.

When the check runs: after plugins load (`plugin-loader.tsx:222`), 30 s after
startup (`plugin-loader.tsx:152-160`), and through `notifyUpdates` when the backend
fires `loader/notify_updates` at the end of each loader self-update poll
(`plugin-loader.tsx:93,251-275`; `updater.py:134`). The "N plugin updates" toast
only comes from that last path (`plugin-loader.tsx:287-300`). Kettle's patch
stops the poll, so **on Kettle plugin updates show up only as a badge in
settings, never as a toast**.

### 1.5 `POST <base>/<name>/versions/<version>/increment?isUpdate=<True|False>`

Sent by the backend after a successful *download* from a URL, and before
extraction (`browser.py:216-229`). The name and version go in **un-encoded**
(names contain spaces, e.g. `CSS Loader`; aiohttp quotes them). `isUpdate` is
Python's `str(bool)`, i.e. `True`/`False`. A non-200 reply is only logged. Not
sent for `file://` installs. It goes to the *currently selected* store, even when
the artifact came from elsewhere. A replacement should send it only to the
source the plugin came from, or skip it when telemetry is off.

### 1.6 Artifact and image URLs

* Zip: `version.artifact` if present, else
  `https://cdn.tzatzikiweeb.moe/file/steam-deck-homebrew/versions/<hash>.zip`
  (`store.tsx:121,129,163-165`). It is a Backblaze B2 bucket behind Cloudflare
  (`x-bz-file-name: versions/<hash>.zip`, `content-type: application/zip`).
  Checked tonight: SHA-256 of the downloaded file equals the hash for
  `Example Plugin 0.0.1` and `SteamGridDB 1.8.1`. **Note:** a custom store that
  leaves out `artifact` silently sends clients to the official CDN.
* Image: `https://cdn.tzatzikiweeb.moe/file/steam-deck-homebrew/artifact_images/<url-encoded name>-<sha256 of image bytes>.<ext>`
  (verified: the SteamGridDB PNG hashes to the suffix in its URL). The server
  fetches `publish.image` at submit time and re-hosts it. The upload fails with
  "invalid or missing URL scheme" if the URL is bad (`build-plugins.yml:377-384`).

### 1.7 Other server endpoints (not used by the loader)

From `openapi.json`: `POST /__submit` (multipart: `name, author, description,
tags, version_name, image, file, force?`; `Authorization: <key>` header),
`POST /__update` (edit metadata or versions by id), `POST /__delete` (`{id}`),
`POST /__auth`, and `/v1/announcements[...]` CRUD plus `/-/current`. Nothing in this
loader revision reads announcements. `GET /plugins/<name>` does not exist (404).

---

## 2. Install flow

### 2.1 Entry points and the confirm handshake

Frontend → backend WebSocket routes (`DL/backend/decky_loader/utilities.py:62-66,123-144`):

| route | args | effect |
|---|---|---|
| `utilities/install_plugin` | artifact, name="No name", version="dev", hash="", install_type=0 | queues a request, emits `loader/add_plugin_install_prompt(name, version, request_id, hash, install_type)` (`browser.py:309-313`) |
| `utilities/install_plugins` | list of `{name, artifact, version, hash, install_type}` | one request id, emits `loader/add_multiple_plugins_install_prompt` (`browser.py:315-319`) |
| `utilities/confirm_plugin_install` | request_id | runs `_install` for each, one after another (`browser.py:321-326`) |
| `utilities/cancel_plugin_install` | request_id | drops it |
| `utilities/uninstall_plugin` | name | `uninstall_plugin` |

The request id is `str(time())`. Install type values: INSTALL 0, REINSTALL 1, UPDATE 2,
DOWNGRADE 3, OVERWRITE 4 (`browser.py:29-34`, `DL/frontend/src/plugin.ts:20-26`).
They only choose the modal's wording; the backend does not branch on them.

Callers: the store card (`PluginCard.tsx:167`), plugin list update/reinstall
(`settings/pages/plugin_list/index.tsx:35,129-130`), and developer settings
(below).

### 2.2 `_install(artifact, name, version, hash)` step by step (`browser.py:173-307`)

Progress events: `loader/plugin_download_start(name)`, then
`loader/plugin_download_info(percent, i18n-key)` at 5/10/70/75/80/90/95, then
`loader/plugin_download_finish(name)`.

1. **Already installed?** Scan every folder in `plugins/` for a `plugin.json`
   whose `name` matches (`find_plugin_folder`, `browser.py:108-118`). The folder
   name does not matter; identity is `plugin.json` `name`.
2. Snapshot `pluginOrder` (`browser.py:190`), then pause the file watcher.
3. **Fetch.** `file://` → read the local file. Otherwise a single GET with no streaming
   or size cap, read fully into memory. If the status is not 200, log it and the
   zip stays empty (`browser.py:195-214`). Then send the increment POST (§1.5).
4. **Dev installs** (`version == "dev"`, i.e. URL/zip installs from developer
   settings): the zip must hold exactly one `*/plugin.json` at depth one, and
   the plugin name is taken from it, falling back to the folder name
   (`browser.py:232-257`). Store installs skip this and trust the store's name.
5. If nothing was fetched, it aborts **without** re-enabling the watcher and without
   emitting finish (`browser.py:260-262`). That is an upstream bug.
6. **Remove the old version** if one is installed: a full `uninstall_plugin(name)` (§2.4),
   which **runs the old plugin's `_uninstall()` hook and clears its
   hidden/frozen/disabled/order settings** (`browser.py:265-271`).
7. **Hash check and extract** (`_unzip_to_plugin_dir`, `browser.py:58-64`): SHA-256
   of the in-memory bytes; if a hash was given and does not match, it refuses.
   With an empty hash (every dev/URL install) **there is no check at all**. Then
   `ZipFile.extractall(plugins/)`, so the *whole zip* lands in `plugins/`,
   whatever its top-level entries are.
8. **Find the new folder** again by `plugin.json` name (`browser.py:279-281`).
9. **Remote binaries** (`browser.py:66-106`): if `package.json` has a non-empty
   `remote_binary` array, chmod the plugin dir **0777 recursively** (root only),
   create `bin/`, then for each `{name, url, sha256hash}` download it and write it
   to `bin/<name>` only if the SHA-256 matches (`helpers.py:111-134`). Any failure
   fails the install. A missing field fails too.
10. **Ownership/permissions** (`set_plugin_dir_permissions`, `browser.py:120-137`)
    only when `CHOWN_PLUGIN_PATH=1` (the default): a recursive chown to the
    unprivileged user (or to root if `plugin.json` `flags` contains `root`), then the
    dir itself and `plugin.json` to root, a **recursive chmod 755**, and plugin.json
    755. Python's `extractall` does not restore Unix modes, so this chmod is what
    makes `bin/*` executable.
11. Stop and drop any loaded instance, sleep 1 s. For a fresh install, append the name
    to `pluginOrder`. Then `import_plugin(<dir>/main.py, folder)` (`browser.py:285-296`).
    On update the name is **not** put back into `pluginOrder` (the snapshot from step
    2 is never used), so an updated plugin drops out of the user's custom order.
    That is an upstream bug.
12. A hash mismatch is only logged (`browser.py:303-304`), and the old version has
    already been removed in step 6. Net effect: **a corrupted update uninstalls the
    plugin**.

### 2.3 Expected zip layout

One top-level folder holding the plugin. The store CI names it after the
database submodule directory (`-s directory`, `build-plugins.yml:304`); the CLI
default is the `plugin.json` name (`CLI/src/cli/plugin/build.rs:276-284`).
Observed: `decky-steamgriddb/…`, `decky-plugin-template/…`.

```
<folder>/
  plugin.json        required  {name, author, flags[], api_version?, publish{tags[],description,image}}
  package.json       required  version (the store version), type:"module" → ESM frontend, remote_binary[]
  main.py            optional  Python backend (class Plugin, _main/_unload/_uninstall/_migration)
  *.py               any other top-level .py files the build output contains
  dist/index.js      required  frontend bundle (+ assets, maps)
  bin/               optional  backend build output / bundled remote binaries
  py_modules/        optional  added to sys.path
  <defaults/*>       optional  contents of defaults/ flattened into the root
  LICENSE, README.md
```

What the loader requires at load time: a `plugin.json` in each folder
(`DL/backend/decky_loader/loader.py:198-201`). The installed version is
`package.json` `version` (`DL/backend/decky_loader/plugin/plugin.py:34-37`), and the
backend reads `name`, `author`, `flags`, `api_version`
(`plugin.py:40-43`).

### 2.4 Uninstall (`browser.py:139-171`)

1. Pause the watcher and find the folder by name.
2. Emit `loader/unload_plugin(name)` so the frontend unmounts it.
3. If loaded: `stop(uninstall=True)`. The sandbox process receives
   `{"uninstall": true}` first (`plugin.py:147-158`), then on shutdown calls
   `_unload()` and then **`_uninstall()`**
   (`DL/backend/decky_loader/plugin/sandboxed_plugin.py:146-185,195-197`). Hooks
   are called as methods when `api_version >= 1`, or with the class as `self` for
   legacy plugins. Then it removes the plugin's entries from `frozenPlugins`,
   `hiddenPlugins`, `pluginOrder` and `disabled_plugins`
   (`browser.py:331-358`).
4. `rmtree` the plugin folder.
5. **Not touched:** `~/homebrew/settings/<folder>`, `data/<folder>`,
   `logs/<folder>`. Those are the plugin's `DECKY_PLUGIN_SETTINGS_DIR`,
   `DECKY_PLUGIN_RUNTIME_DIR` and `DECKY_PLUGIN_LOG_DIR`
   (`sandboxed_plugin.py:77-91`). They are keyed by **folder name**, not
   plugin name, so a zip whose top folder gets renamed loses its data. Only the
   plugin's own `_uninstall` can delete them.

Disabled plugins are not loaded, so `name in self.plugins` is false and their
`_uninstall` is **not** called. Their settings entries are not cleaned up either
(`browser.py:154-161`).

### 2.5 Reinstall / update / downgrade / overwrite

All four take the same path: a full uninstall (with `_uninstall`), then a fresh
extract. Settings and data in `~/homebrew/{settings,data,logs}` survive unless the
plugin's `_uninstall` deletes them. Plugins that assume `_uninstall` means
"the user removed me" will wipe their own settings on every update. A
replacement has to choose: keep this behaviour for compatibility, or (better) add an
explicit `uninstall=false` update path and tell plugins through a new hook or env
var, falling back to the old semantics behind a flag.

### 2.6 Developer-mode installs

Developer settings page (`DL/frontend/src/components/settings/pages/developer/index.tsx`):
* **From zip**: a file picker rooted at the user's home with a `.zip` filter, then
  `file://<path>` (`index.tsx:28-40`).
* **From URL**: a text field; `https://` is prepended when there is no scheme. A
  third-party warning is shown (`index.tsx:95-106`).

Both go through `installFromURL` (`store.tsx:115-118`), which sends only the
artifact and a name taken from the URL's last segment minus `.zip`. The version
defaults to `dev` and the hash to empty, so the name comes from the zip, nothing is
verified, and a `file://` install sends no increment.

### 2.7 Testing store

`testing.deckbrew.xyz` has the same API and holds builds from **open pull requests**
to the database. PR CI runs in the `testing_env` GitHub environment, whose
`STORE_URL` secret points at the testing server (`build-plugins.yml:188-189`), and it
adds `-<sha7>` to the version (`build-plugins.yml:215-233`). The store page shows a
warning banner when Testing is selected (`Store.tsx:100-106`, banner around
`Store.tsx:196-222`). 98 plugins tonight.

---

## 3. Plugin build pipeline

### 3.1 Template (`TPL/`)

* `plugin.json`: `name`, `author`, `flags` (`debug`; `root` makes the backend run
  as root, and the template ships `_root`, i.e. off), `api_version: 1`, and
  `publish.{tags,description,image}` (`TPL/plugin.json:1-11`). A `dnu` tag in
  `publish.tags` stops the CI from uploading (`build-plugins.yml:366`).
* `package.json`: `version` is the store version, `type: module` (ESM loader),
  `scripts.build` = `rollup -c` with `@decky/rollup`. Optional
  `remote_binary[] {name,url,sha256hash}` and `remote_binary_bundling: bool`.
* `defaults/`: files copied into the plugin root
  (`TPL/defaults/defaults.txt`).
* `backend/`: `Dockerfile` (FROM `ghcr.io/steamdeckhomebrew/holo-base`, plus
  rust/go variants) with ENTRYPOINT `/backend/entrypoint.sh`, which runs `make`. The
  output goes to `backend/out`, which becomes `bin/`
  (`TPL/backend/Dockerfile:1-6`, `TPL/README.md:49-60`).
* Local dev: `.vscode/tasks.json` builds with the CLI and deploys by
  rsync/ssh/bsdtar into `~/homebrew/plugins` (`TPL/.vscode/tasks.json:87,96`).
* No `.github/workflows` in this checkout (it is a one-commit shallow clone). The
  template expects you to go through the database for store builds.

### 3.2 CLI `decky plugin build` (`CLI/src/cli/plugin/build.rs`)

1. Validate: `package.json` exists, `plugin.json` parses to `{name, author,
   flags}`. A `backend/` directory needs `backend/Dockerfile`
   (`CLI/src/plugin.rs:32-75`).
2. Make a temp root `<tmp>/<16 random chars>` (default `/tmp/decky`, must be
   absolute) (`build.rs:384-397,435-447`).
3. **Backend** (only if there is a `backend/`): `docker build` the plugin's Dockerfile
   with tag = lowercased name with dashes, then run it with `backend/` →
   `/backend`, `<tmp>/bin` → `/backend/out`, and the plugin root → `/plugin`
   (`build.rs:62-110`). `RELEASE_TYPE=development|production` env, `--user
   uid:gid` unless `-b` (build as root) (`CLI/src/container_engine.rs:84-120`).
4. **Frontend**: run `ghcr.io/steamdeckhomebrew/builder:latest` with plugin →
   `/plugin` and tmp → `/out` (`build.rs:43-60,448`). That image is
   `DB/builder/`: node 20 alpine with pnpm 9, which runs `pnpm i --frozen-lockfile`
   and `pnpm run build`, then rsyncs the whole plugin tree, minus `src/`,
   `node_modules`, `__pycache__`, into `/out` (`DB/builder/Dockerfile:1-11`,
   `DB/builder/entrypoint.sh:3-17`). A lockfile is mandatory.
5. **Remote binaries**: if `remote_binary_bundling` is true, download each
   `remote_binary`, check its SHA-256, and write it to `<tmp>/bin/<name>`
   (`build.rs:112-179`). The loader still downloads them again at install time
   (§2.2 step 9).
6. **py_modules**: copy, skipping `__pycache__`, symlinks optionally followed
   (`build.rs:181-226`).
7. **Zip** `<out>/<name>[-dev].zip` (`build.rs:274-382`): top-level folder = plugin
   name or source directory name (`-s plugin-name|directory`). Files: `LICENSE`,
   `main.py`, `package.json`, `plugin.json`, `README.md`, plus any top-level `*.py`.
   Directories: `dist/` (mandatory), `bin/` (mode 0755), `defaults/` (its
   prefix is stripped, so its contents land in the root), `py_modules/` (0755).
   Deflate level 9 by default (`-m store` and `-l` exist) (`build.rs:250-262`).

`decky plugin deploy` builds, then rsyncs to `deck@ip:<dir>/homebrew/plugins`
and restarts `plugin_loader.service` via `sudo -S` over ssh
(`CLI/src/cli/plugin/deploy.rs:40-160`).

### 3.3 Store CI (`DB/.github/workflows/build-plugins.yml`)

* The database repo holds **one git submodule per plugin** under `plugins/`
  (`DB/.gitmodules`, 121 entries; `DB/README.md:12-25`). To add or update a plugin,
  open a PR that adds the submodule or moves its commit.
* Triggers: push to `main` (paths `plugins/**`), `pull_request_target` (runs with
  secrets on PR head code, `allow-unsafe-pr-checkout: true`, lines 12-13,56-62), and
  manual dispatch with overrides (`plugin_override`, `force_revision`, `force_pnpm`,
  `test_build`) (lines 3-34).
* `find` job: diff submodules against the PR base or HEAD^ to get the changed plugins.
  A changed workflow means rebuild everything (but no uploads) (lines 64-131).
* `build` job per plugin: init the one submodule; on PRs add `-<sha7>` to
  `package.json` version; with `force_revision`, add or increment `-N` (lines
  215-274); optional forced lockfile or DFL bump (279-289); download the CLI
  **0.0.7** binary (291-297); `sudo decky plugin build -b -o /tmp/output -s
  directory plugins/$plugin` (299-307); upload it as a GitHub artifact (309-314).
* Upload (316-396): read `name`, `author`, `publish.description`, `publish.image`
  and `publish.tags` from the zip's `plugin.json`, and `version` from
  `package.json`, then `POST $STORE_URL/__submit` as multipart with `Authorization:
  $SUBMIT_AUTH_KEY` and the zip as `file`. "Version already exists" only warns, so
  **a version cannot be replaced without `force`**. The environment picks the store:
  `env` (production) for push to main or a dispatch with override, `testing_env` otherwise
  (188-189).

### 3.4 Review and publication

* PR templates: addition, update, transfer, removal, other
  (`DB/.github/pull_request_template.md:1-10`, `PULL_REQUEST_TEMPLATE/*.md`).
  The checklists ask for authorship, licence compliance, "generative AI was NOT
  used to write a majority of the code", a test on SteamOS Stable/Beta (or Preview
  for non-Python or dynamically linked backends), and optionally testing two
  other PRs (`PULL_REQUEST_TEMPLATE/plugin_addition.md:32-75`).
* `pr-labeler.yml` labels by the headings in the description, assigns the author, adds
  the PR to the SDH Tracker project, and checks the testing checkbox text
  (`DB/.github/workflows/pr-labeler.yml:1-100`).
* CODEOWNERS: `@SteamDeckHomebrew/loader-team` owns everything
  (`DB/.github/CODEOWNERS:1`). A maintainer merging to main is the only gate.
* **Publishing a version = merge to main**, which builds and `__submit`s to
  production. Removal or hiding goes through `__update`/`__delete` by store admins.
  The server lists versions newest first, and the store shows them all, including
  old ones, which makes downgrades possible.

---

## 4. Loader self-update (`DL/backend/decky_loader/updater.py`)

* Repo from `DECKY_REPO` (default `SteamDeckHomebrew/decky-loader`). The git branch
  for service files comes from `DECKY_BRANCH` (default `dirty`) (`updater.py:45-46`).
* Channel setting `branch`: 0 stable, 1 prerelease (2 is accepted as prerelease
  for the service URL only). If unset, it is guessed from the running version (`-pre`
  → 1) (`updater.py:64-77`).
* Poll: 30 s after start, then every 6 h (`updater.py:137-144`). Check: `GET
  https://api.github.com/repos/<repo>/releases` (unauthenticated, header
  `X-GitHub-Api-Version: 2022-11-28`). Stable = the first `v*` tag that is not a
  prerelease and has no `-pre`; prerelease = the first `v*` tag. Then emit
  `loader/notify_updates` (`updater.py:107-135`). `get_version_info` returns
  current, repo, gitBranch, remote, all, updatable (false for `dev`/`unknown`)
  and supported (`updater.py:96-105`).
* Asset names: `PluginLoader` (x86_64 Linux), `PluginLoader-arm64` (aarch64 Linux),
  `-win[-arm64][-noconsole].exe`, `-mac` (`updater.py:146-172`). The arm64 Linux
  asset first appears in v3.3.0-pre1. v3.2.10 and older ship x86_64 only (live
  releases API).
* Apply (`do_update`, `updater.py:229-281`; `download_decky_binary`, 182-227):
  unless `KEEP_SYSTEMD_SERVICE=1`, download
  `raw.githubusercontent.com/<repo>/<branch>/dist/plugin_loader-{release|prerelease}.service`,
  substitute `${HOMEBREW_FOLDER}`, write it to `/etc/systemd/system/plugin_loader.service`
  and keep a copy in `services/.systemd/`. Then stream the asset into
  `<cwd>/PluginLoader.new` with percent events, write `.loader.version`, delete the
  old binary, move the new one into place, chmod 777, run `chcon -t bin_t` under SELinux,
  then `systemctl restart plugin_loader` (non-blocking). **There is no checksum or
  signature check** on the binary or the unit file, and the process runs as root.
* Testing builds: list open PRs, find the `Builder (<OS>, <arch>)` workflow run for
  the head sha, and download its artifact through `nightly.link` (a zip)
  (`updater.py:290-349`).
* Routes: `updater/get_version_info`, `check_for_updates`, `do_restart`,
  `do_shutdown`, `do_update`, `get_testing_versions`, `download_testing_version`
  (`updater.py:55-61`).

**Kettle:** `packages/decky-loader/0001-kettle-disable-self-update.patch` forces
`updatable: False`, makes `version_reloader` return right away (no GitHub polling,
and so no `notify_updates`, see §1.4), and turns `do_update` and
`download_testing_version` into logged no-ops. Kettle's unit sets
`KEEP_SYSTEMD_SERVICE=1` (`decky-loader-start:160`). The patch's context lines match
v3.2.10, which Kettle pins. Against v3.3.0-pre1, `get_version_info` has gained
`repo`/`gitBranch`/`supported` keys and the patch will no longer apply at
`--fuzz=0`. Its comment saying PR builds are x86_64-only is also out of date now that an
arm64 builder exists. A replacement for Kettle should have no in-process
self-updater at all: the image (RAUC A/B) owns the loader.

---

## 5. Loader install on SteamOS, and what a drop-in must preserve

### 5.1 Upstream install

`DL/dist/install_{release,prerelease}.sh` are stubs pointing at
`decky-installer/cli/install_*.sh` (not in the reference set; fetched as data into
`decky-spec/installer/`). That script:
1. Re-runs itself under sudo. It needs `jq` and checks that github.com and
   api.github.com are reachable.
2. As the user, creates `~/homebrew/plugins` and touches
   `~/.steam/steam/.cef-enable-remote-debugging`, plus the Flatpak Steam location
   `~/.var/app/com.valvesoftware.Steam/data/Steam/` if present.
3. Picks the asset by `uname -m`, takes the first non-prerelease (or
   prerelease) release from the GitHub API, wipes `~/homebrew/services`, downloads
   the binary to `services/PluginLoader`, chmod +x, chcon under SELinux, writes
   `services/.loader.version`.
4. Stops and disables any user-level and system `plugin_loader`, fetches
   `dist/plugin_loader-release.service` from main (falling back to a built-in copy),
   substitutes the homebrew path, installs it as
   `/etc/systemd/system/plugin_loader.service`, and keeps copies in `services/.systemd/`.
5. `daemon-reload`, `start`, `enable`.

Unit (`DL/dist/plugin_loader-release.service:1-16`): `User=root`,
`Restart=always`, `KillMode=process`, `TimeoutStopSec=15`, `ExecStart` and
`WorkingDirectory` under `~/homebrew/services`, env `UNPRIVILEGED_PATH` =
`PRIVILEGED_PATH` = `~/homebrew`, `LOG_LEVEL=INFO` (prerelease: DEBUG),
`WantedBy=multi-user.target`. The loader restarts and stops itself by the name
`plugin_loader` (`updater.py:283-288`), so a replacement must keep that unit
name, or patch those calls.

CEF debugging: Steam opens its DevTools port on `localhost:8080` only if
`.cef-enable-remote-debugging` exists in its data dir when the client starts. The
injector talks to `http://localhost:8080` (`DL/backend/decky_loader/injector.py:12`),
and a workaround closes a stale 8080 listener after a Steam restart
(`localplatformlinux.py:309-315`). Kettle touches
`~/.local/share/Steam/.cef-enable-remote-debugging` on every start
(`decky-loader-start:141-142`).

Uninstall (`DL/dist/uninstall.sh`, same as the installer repo's version):
disable --now and delete the unit, delete `services/PluginLoader`, delete the CEF
flag files. **Plugins, settings, data and logs in `~/homebrew` stay.**

### 5.2 Paths and environment the loader reads

| env | default | used for |
|---|---|---|
| `UNPRIVILEGED_PATH` | parent of `PLUGIN_PATH`, else 2 levels above the binary, else `/home/deck/homebrew` | "homebrew" dir: `settings/`, `data/`, `logs/` per plugin (`localplatformlinux.py:257-283`) |
| `PRIVILEGED_PATH` | = unprivileged | `plugins/` and `settings/loader.json` (`main.py:47,75`; `localplatformlinux.py:238-246`) |
| `UNPRIVILEGED_USER` | owner derived from the homebrew path, else `deck` | chown target, `DECKY_USER` (`localplatformlinux.py:285-307`) |
| `CHOWN_PLUGIN_PATH` | 1 | all chown/chmod on install and load (`localplatform.py:32-33`) |
| `KEEP_SYSTEMD_SERVICE` | 0 | updater leaves the unit alone |
| `SERVER_HOST`/`SERVER_PORT` | 127.0.0.1:1337 | backend HTTP/WS; the injected script imports `http://localhost:1337/frontend/...` (`helpers.py:56-57`) |
| `LIVE_RELOAD`, `LOG_LEVEL`, `USE_CEF_CLOSE_WORKAROUND`, `DECKY_REPO`, `DECKY_BRANCH`, `DECKY_NOCONSOLE` | | |

Settings migration quirk: any `*.json` lying in the homebrew root is moved into
`settings/` at startup (`DL/backend/decky_loader/settings.py:22-27`).

### 5.3 Drop-in migration contract (keep existing `~/homebrew` working)

To pick up an existing install without reinstalling plugins, a replacement must:
1. **Plugin dirs**: load every `plugins/<folder>/` that has a `plugin.json`, with
   identity = `plugin.json.name` and version = `package.json.version`. Keep
   the folder names as they are (data dirs are keyed by them).
2. **Per-plugin env and dirs** exactly as `sandboxed_plugin.py:77-91`:
   `DECKY_HOME`, `DECKY_USER`, `DECKY_USER_HOME`, `DECKY_VERSION`,
   `DECKY_PLUGIN_{DIR,NAME,VERSION,AUTHOR,SETTINGS_DIR,RUNTIME_DIR,LOG_DIR}`, and
   `py_modules` on `sys.path`. Settings, data and logs are under
   `<homebrew>/{settings,data,logs}/<folder>`.
3. **`settings/loader.json`** keys, read and written with the same meaning: `store`,
   `store-url`, `store_select.warn.third_party`, `branch`, `pluginOrder`,
   `hiddenPlugins`, `frozenPlugins`, `disabled_plugins`, developer toggles,
   `user_info.*`. The file is pretty-printed JSON and gets rewritten whole on every
   set (`settings.py:51-60`).
4. **Root flag**: `flags` contains `root` → backend runs as root and the folder is
   owned by root; otherwise the folder is owned by the user, with the dir itself and
   `plugin.json` owned by root (`browser.py:120-134`, `plugin.py:50-59`).
5. **Lifecycle hooks**: `_main`, `_unload`, `_uninstall`, `_migration`, with the
   legacy (api_version 0) class-as-self calling convention
   (`sandboxed_plugin.py:121-185`).
6. **Unit name** `plugin_loader.service`, the CEF flag file, and port 1337.
7. **Store wire format** in §1, so old and new clients can share stores.

Kettle differs on purpose: plugins and `loader.json` live in root-owned
`/home/.kettle/decky` (`PRIVILEGED_PATH`), per-plugin data stays in `~/homebrew`,
`CHOWN_PLUGIN_PATH=0`, image-shipped plugins are symlinked from
`/usr/share/decky/plugins`, and a one-shot migration moves root-owned plugin
folders and `loader.json` out of `~/homebrew` and sets user-written folders
aside in `~/homebrew/plugins-aside` (`packages/decky-loader/decky-loader-start:31-165`).

**Kettle bugs that follow from §2.2:** with `CHOWN_PLUGIN_PATH=0`,
`set_plugin_dir_permissions` does nothing (`browser.py:135-137`), so:
(a) store plugins with a `bin/` directory come out of `extractall` with
umask modes (0644) and their binaries are **not executable**;
(b) plugins using `remote_binary` keep the **recursive 0777** from
`browser.py:79`, which nothing resets. That leaves a world-writable folder that the
root-run loader imports, and possibly runs as root: a local privilege-escalation path.
The replacement (or a Kettle patch) should apply modes from the zip's external
attributes, or set 0755 dirs / 0644 files / 0755 `bin/*` under root ownership
itself, and never chmod 777.

---

## 6. Gaps an open store should fix, and a multi-source index proposal

### 6.1 Problems with the current design

1. **Single gatekeeper.** One GitHub org reviews, builds, signs off and hosts.
   The client allows exactly one store at a time; a custom store replaces the official
   one.
2. **No signatures.** Integrity is a SHA-256 that comes from the same server
   that serves the zip. TLS is the only authenticity check. The loader's own
   self-update has no check at all and runs as root.
3. **Unverified dev and URL installs** (empty hash), and the store name is trusted
   over the zip's `plugin.json` for store installs.
4. **The zip can write anywhere under `plugins/`**: `extractall` writes every top-level
   entry, so one zip can overwrite another plugin's folder. Identity is by
   `plugin.json` name scan, with no namespace (two authors, same name: collision).
5. **Destructive updates**: the old version is removed before the new one is checked,
   `_uninstall` runs on update, pluginOrder/hidden/frozen are lost, and a failed
   download or hash leaves nothing installed. The install is not atomic and there is no
   rollback.
6. **Whole download in memory**, no size limit, no resume, no progress.
7. **Silent CDN fallback**: a store that leaves out `artifact` sends clients to the
   official CDN by hash.
8. **Telemetry by default**: the increment POST goes to whichever store is selected, carries
   the loader version, and cannot be turned off.
9. **Version gate** (§0) is set server-side with no negotiation. An old client
   breaks with a JSON parse error.
10. **Non-semver versions never update**; testing builds sort below the release.
11. **No compatibility metadata**: no architecture (aarch64 vs x86_64 `bin/`),
    minimum loader version, API version, or required Steam client. ARM devices get
    x86_64 binaries.
12. **Root plugins** are marked only by a free-text tag for display. The real
    `flags: root` is only discovered after extraction.
13. **CI runs PR code with secrets** (`pull_request_target` +
    `allow-unsafe-pr-checkout`), and builds use `:latest` builder images and an
    unpinned holo-base, so builds are not reproducible.
14. **No metadata for licence, source repo, changelog, or homepage** in the API.

### 6.2 Proposal: multi-source index, compatible with the existing API

**Client model.** An ordered list of *sources*, each `{id, url, kind, enabled,
trust}`. `kind = "decky-v1"` means a server with the existing API (official,
testing, any custom store). `kind = "index-v1"` means a static JSON index (below) that
can live on any static host (GitHub Pages, a Kettle mirror, a local file). The client
merges all sources into one list, keyed by `(source id, plugin id)`, and
remembers per installed plugin which source it came from
(`<homebrew>/settings/store-sources.json`, a new file, so `loader.json` stays
compatible). Update checks only ask a plugin's own source unless the user
pins it elsewhere. Existing `store`/`store-url` settings map onto the source list
on first start (0 → official, 1 → testing, 2 → custom URL).

**Static index format (`index-v1`)** is a superset of the Decky list response, so
the same file can also be served as `<base>` for old clients:

```jsonc
// GET <source>/index.json  (also valid as a decky-v1 "<base>" list)
{
  "format": "decky-index/1",
  "source": { "id": "org.kettlelinux.main", "name": "Kettle", "homepage": "https://…",
              "keys": [ { "id": "k2026", "alg": "ed25519", "pub": "base64…" } ] },
  "generated": "2026-10-18T00:00:00Z",
  "plugins": [
    {
      // --- decky-v1 fields, unchanged meaning ---
      "id": 36, "name": "SteamGridDB", "author": "SteamGridDB",
      "description": "…", "tags": ["artwork"], "image_url": "https://…/img/<sha256>.png",
      "visible": true,
      "versions": [
        { "name": "1.8.1", "hash": "<sha256 of zip>", "created": "…",
          "artifact": "https://…/zips/<sha256>.zip",          // ALWAYS present here
          // --- extensions (ignored by old clients) ---
          "size": 3438092,
          "min_loader": "v3.2.0", "api_version": 1,
          "arch": ["any"],                                     // or ["x86_64"], ["aarch64"]
          "flags": ["root"],                                   // mirrored from plugin.json
          "folder": "decky-steamgriddb",                       // the zip's single top-level dir
          "sig": { "key": "k2026", "alg": "ed25519", "value": "base64…" }, // over the zip hash + name + version
          "changelog": "https://…", "source_url": "https://github.com/…", "license": "GPL-3.0"
        }
      ],
      // --- plugin-level extensions ---
      "plugin_id": "com.steamgriddb.decky",   // reverse-DNS, collision-free identity
      "upstream": { "source": "com.deckbrew.official", "name": "SteamGridDB" } // mirror/provenance
    }
  ]
}
```

and `versions.json` with exactly the `/versions` tuple shape, always with the
3rd element (artifact URL). Files: `index.json`, `versions.json`,
`index.json.sig` (detached ed25519 over the index bytes), plus content-addressed
`zips/<sha256>.zip` and `img/<sha256>.<ext>`. A `decky-v1` server can be
mirrored into this format by a cron job: fetch list and versions, download zips
by hash, verify them, sign them with the mirror's key, and record `upstream`.

**Client rules for a better install:**
1. Resolve the artifact URL from that source only, and never fall back to another CDN.
2. Stream it to a temp file under `plugins/.staging/`, enforce `size`, check
   SHA-256, and check `sig` when the source has keys (fail closed if a signed
   source sends unsigned versions). Unsigned sources show a trust prompt.
3. Validate the zip: exactly one top-level folder, no absolute paths or `..`, no
   symlinks or device files, and its `plugin.json.name` must match the listing. Show
   `flags: root` **before** confirming.
4. Extract into staging, set modes ourselves (dirs 0755, files 0644, `bin/*` 0755,
   root-owned; non-root plugins get user-owned settings, data and logs only),
   fetch `remote_binary` into staging with the same hash rule, then **swap atomically**
   with a rename, keeping the previous version as `.prev` for rollback.
5. On update: unload the old version (`_unload`, not `_uninstall`), keep
   pluginOrder, hidden, frozen and disabled, and set `DECKY_PLUGIN_UPDATING=1` for the
   new instance's `_migration`. `_uninstall` runs only on a real uninstall. For
   strict compatibility a per-plugin quirk can bring back the old behaviour.
6. Telemetry (the increment POST) is opt-in per source.
7. Send `X-Decky-Version` as the upstream version we are compatible with. Treat 426
   as "this source requires a newer loader" and show it in the UI.
8. Respect `arch` and `min_loader`. Hide or flag versions that do not fit the device
   (aarch64 on Kettle).

This keeps every existing store, plugin zip and `~/homebrew` layout working,
allows any number of community or vendor sources (Kettle's own signed mirror
among them), and moves trust from "whoever runs the one server" to keys the user
can see and choose.
