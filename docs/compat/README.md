# Decky compatibility specs

Remora runs unmodified Decky Loader plugins. These four specs say what that takes. They describe
Decky's behaviour and interfaces in our own words; no Decky code is copied into them, and none may
be copied into Remora (clean-room rule, see [../ARCHITECTURE.md](../ARCHITECTURE.md)).

| Spec | What it covers |
|---|---|
| [frontend.md](frontend.md) | CEF injection, globals (`SP_REACT`, `DFL`, the `@decky/api` init hook), plugin bundle loading, every `@decky/api` and legacy ServerAPI call, the WebSocket protocol, QAM/router/toaster hooks, fragile Steam lookups |
| [backend.md](backend.md) | On-disk layout, `plugin.json`/`package.json`, the Python plugin lifecycle and calling conventions, the process model and IPC, the `decky` module, HTTP routes on :1337, `loader.json`, security and reliability weak points |
| [store.md](store.md) | The store HTTP API, install/update/uninstall, plugin build pipeline, loader self-update, SteamOS install, the migration contract, and a proposal for a multi-source signed index |
| [plugin-survey.md](plugin-survey.md) | What 45 real plugins actually use, ranked; plugins that touch loader internals; the minimum API set |

## Reference versions

`file:line` references point into these upstream checkouts (`decky-ref/<repo>/…`), taken 2026-10-17:

| Repo | Commit | License |
|---|---|---|
| SteamDeckHomebrew/decky-loader | `67196ad` (main, 2026-10-17, after v3.3.0-pre1) | GPL-2.0 |
| SteamDeckHomebrew/loader-api (`@decky/api`) | `d1b7f16` | LGPL-2.1 |
| SteamDeckHomebrew/decky-frontend-lib (`@decky/ui`) | `5419bcb` | LGPL-2.1 |
| SteamDeckHomebrew/decky-plugin-template | `90d0780` | BSD-3-Clause |
| SteamDeckHomebrew/decky-plugin-database | `a8e6d7f` | AGPL-3.0 |
| SteamDeckHomebrew/cli | `6c32da7` | — |
| `@decky/rollup` (npm) | 1.0.2 | BSD-3-Clause |

The plugin survey used each plugin's default branch on 2026-10-17, not the commit the store pins.
