# Decky plugin API usage survey

Date: 2026-10-17. Scope: 45 plugins: 40 store plugins (shallow clones of each default branch in `decky-ref/plugins/`, not the exact commit pinned by decky-plugin-database), plus SimpleDeckyTDP (widely used, not in the store), plus Kettle's five plugins (`packages/kettle-decky-plugins`). Nothing was built, installed or run; all results come from static greps of the source (`survey.py`, `report.py`, `md.py` in the session scratchpad). Counts are "number of plugins that reference it" in their own code (vendored `py_modules`, `node_modules`, `dist`, tests, CI and build scripts excluded except where noted).

Reference loader read for the contract: `decky-ref/decky-loader` (commit 67196ad), `loader-api` (@decky/api 1.1.x), `decky-frontend-lib` (@decky/ui 4.x), `rollup` (@decky/rollup 1.0.2).

## 0. Loader contract facts that the survey depends on

These are behaviours of the real loader that plugins rely on without calling a named API; a replacement has to copy them.

- **Frontend load type comes from `package.json` `"type": "module"`**, not from plugin.json. `module` -> ESM: `import("http://127.0.0.1:1337/plugins/<name>/dist/index.js")` then `default()` is called with no args. Otherwise legacy: fetch `/plugins/<name>/frontend_bundle` (with `X-Decky-Auth`), `eval()` it, and call the result with the legacy serverAPI object. Emuchievements is an IIFE that uses @decky/api (so it is eval-loaded but ignores serverAPI).
- **Backend calling convention comes from plugin.json `api_version`** (absent = 0). 0: `Plugin` is the class itself, methods are called as `Plugin.method(Plugin, **kwargs)` (`self` is the class), hooks `_main(Plugin)` etc. >=1: `Plugin()` is instantiated, methods called positionally `method(*args)`. A plugin can mix: MicroSDeck is ESM + api_version 0 (Rust backend, Python only spawns it). protondb-decky declares `api_version: 2` (treated as >=1).
- **`@decky/api` connects through `window.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit.connect(version, pluginName)`**, compiled into every v1 plugin bundle (plugin name comes from plugin.json at build time via `@decky/manifest`). It asks for version 2 and falls back to 1. This global is the whole frontend ABI for v1 plugins: `call, callable, addEventListener, removeEventListener, openFilePicker, executeInTab, fetchNoCors, getExternalResourceURL, injectCssIntoTab, removeCssFromTab, routerHook, toaster, _version` (+ `useQuickAccessVisible` at v2).
- **`window.DFL` must be a full @decky/ui.** Every @decky/rollup plugin maps `@decky/ui` -> global `DFL`; 17 of the 19 legacy plugins also map `decky-frontend-lib` -> `DFL` (only bash-shortcuts and decky-networkinfo bundle their own copy). So legacy v3-era bundles run against whatever the loader puts in `window.DFL` today (@decky/ui 4.x). `window.SP_REACT`, `window.SP_JSX` (`react/jsx-runtime`) and `window.SP_REACTDOM` are required by all plugins (JSX factory and rollup externals).
- **Asset URLs are hard-coded to `http://127.0.0.1:1337/plugins/<plugin.json name>/`** by rollup-plugin-import-assets (in @decky/rollup and every legacy template). The loader serves `/plugins/{name}/dist/*`, `/plugins/{name}/assets/*` (-> `dist/assets`), `/plugins/{name}/data/*` (-> `~/homebrew/data/<dir>`). The port and host are therefore ABI.
- **Python environment**: each backend runs in a forked child of the loader process (setuid to the user unless `root` flag), with `<plugin>/py_modules` appended to `sys.path`, `decky` and `decky_plugin` (same module) injected into `sys.modules`, and **every already-imported `decky_loader.*` module re-exported under its bare name** (`settings`, `helpers`, `injector`, `localplatform`, ...). The loader's own third-party packages (aiohttp, certifi, watchdog, multidict, yarl, async_timeout...) are importable because the child inherits the loader's interpreter.
- **Environment variables** set for each backend: `HOME, USER, DECKY_VERSION, DECKY_USER, DECKY_USER_HOME, DECKY_HOME, DECKY_PLUGIN_SETTINGS_DIR (~/homebrew/settings/<dir>), DECKY_PLUGIN_RUNTIME_DIR (~/homebrew/data/<dir>), DECKY_PLUGIN_LOG_DIR (~/homebrew/logs/<dir>), DECKY_PLUGIN_DIR, DECKY_PLUGIN_NAME, DECKY_PLUGIN_VERSION (from package.json), DECKY_PLUGIN_AUTHOR`. Plugins read both `decky.X` and `os.environ["X"]`.
- **Flags**: only `root` (run backend as root, plugin dir owned by root) and `debug` (reload on file change/refresh) have effect. `global-dfl` (PowerTools) and `_root` (AudioLoader, a disabled `root`) are ignored by the current loader.

## 1. Per-plugin table

Legend: **FE** = frontend loader API used; **BE** = backend `decky`/`decky_plugin` members, lifecycle hooks and loader modules imported; **UI-internals** = @decky/ui webpack finders and patch helpers (full @decky/ui list in section 2); **SC** = number of distinct `SteamClient.*` calls; **Globals** = Steam stores / window globals touched directly. Load: ESM = `type: module`, IIFE = legacy eval. api = plugin.json api_version.

| Plugin | API style / load | api | flags | FE loader API | BE (decky / hooks / loader modules) | UI-internals | SC | Globals | py_modules / bin / native | Internals / risks |
|---|---|---|---|---|---|---|---|---|---|---|
| Bluetooth | v1 @decky/api / ESM | 1 | - | call / callable / serverAPI.callPluginMethod | import decky; DECKY_HOME, DECKY_USER_HOME, logger, migrate_logs, migrate_runtime, migrate_settings; hooks _main, _migration, _uninstall, _unload | - | 0 | LocalizationManager | needs pkgs: jeepney | - |
| ControllerTools | v1 @decky/api / ESM | 1 | - | call / callable / serverAPI.callPluginMethod, toaster.toast | import decky; DECKY_HOME, DECKY_PLUGIN_DIR, DECKY_PLUGIN_LOG, DECKY_PLUGIN_SETTINGS_DIR, logger; hooks _main, _unload; **loader modules: settings** | - | 0 | - | Docker backend; native: Rust | Rust backend in bin/ |
| DeckWebBrowser | v2 serverAPI / IIFE | 0 | - | call / callable / serverAPI.callPluginMethod, routerHook.addRoute, routerHook.removeRoute | import decky_plugin; DECKY_PLUGIN_DIR, logger; hooks _main, _unload; **loader modules: injector, settings**; loader pkgs: aiohttp | afterPatch, findClassModule, findInReactTree, findModuleChild, getFocusNavController, getReactRoot, replacePatch | 5 | settingsStore, scriptLoaded, stream | - | Py imports loader `injector` (get_tab, get_gamepadui_tab, Tab) to inject client.js into CEF tabs; opens localhost:8080 devtools |
| Decky-Framegen | v1 @decky/api / ESM | 1 | - | call / callable / serverAPI.callPluginMethod, definePlugin, openFilePicker, toaster.toast | import decky; DECKY_PLUGIN_DIR, HOME, logger; hooks _main, _unload | - | 2 | - | remote_binary: Optiscaler_0.9.4-final.20260718._MM.7z, amd_fidelityfx_upscaler_dx12.dll, amdxcffx64.dll ... | - |
| Decky-Undervolt | v1 @decky/api / ESM | 1 | root | addEventListener, call / callable / serverAPI.callPluginMethod, removeEventListener, routerHook.addRoute, routerHook.removeRoute | import decky; emit, logger; **loader modules: settings** | - | 2 | appStore | Docker backend | - |
| DiscordStatus | v1 @decky/api / ESM | 1 | - | call / callable / serverAPI.callPluginMethod, definePlugin, toaster.toast | import decky; DECKY_PLUGIN_SETTINGS_DIR, logger; hooks _main, _unload; **loader modules: settings** | - | 5 | appStore | - | - |
| Emuchievements | v1 @decky/api / IIFE | 1 | - | call / callable / serverAPI.callPluginMethod, definePlugin, fetchNoCors, routerHook.addPatch, routerHook.addRoute, routerHook.removePatch, routerHook.removeRoute, toaster.toast | import decky_plugin; DECKY_HOME, DECKY_PLUGIN_DIR, DECKY_PLUGIN_SETTINGS_DIR, migrate_settings; hooks _main, _migration, _unload | afterPatch, beforePatch, callOriginal, findInTree, findModuleChild, replacePatch | 3 | App, LocalizationManager, appAchievementProgressCache, appDetailsCache, appDetailsStore, appStore, collectionStore | Docker backend; native: C/C++ | IIFE (legacy eval) bundle using @decky/api |
| HueSync | v1 @decky/api / ESM | 1 | root | call / callable / serverAPI.callPluginMethod, toaster.toast | import decky; DECKY_HOME, DECKY_PLUGIN_LOG_DIR, DECKY_PLUGIN_RUNTIME_DIR, DECKY_PLUGIN_SETTINGS_DIR, DECKY_USER_HOME, logger, migrate_logs, migrate_runtime, migrate_settings; hooks _main, _migration, _unload; **loader modules: settings** | findModuleChild | 7 | - | py_modules: config, custom_rgb_manager, custom_zone_animator, devices, ec, huesync, id_info, led ...; Docker backend | - |
| Junk-Store | v2 serverAPI / IIFE | 0 | - | call / callable / serverAPI.callPluginMethod, routerHook.addRoute, routerHook.removeRoute, toaster.toast | import decky_plugin; DECKY_HOME, DECKY_PLUGIN_DIR, DECKY_PLUGIN_LOG_DIR, DECKY_PLUGIN_NAME, DECKY_PLUGIN_RUNTIME_DIR, DECKY_PLUGIN_SETTINGS_DIR, DECKY_USER, DECKY_USER_HOME, logger, migrate_logs, migrate_runtime, migrate_settings; hooks _main, _migration, _unload; loader pkgs: aiohttp | afterPatch, findClassModule | 14 | SteamUIStore, appDetailsCache, appDetailsStore, appStore, settingsStore | - | passes `http://localhost:1337/plugins` to its scripts as CONTENT_SERVER; own websocket server; needs loader aiohttp |
| MagicPodsDecky | v1 @decky/api / ESM | 1 | - | call / callable / serverAPI.callPluginMethod, routerHook.addGlobalComponent, routerHook.addRoute, routerHook.removeGlobalComponent, routerHook.removeRoute, toaster.toast | import decky; DECKY_PLUGIN_DIR, DECKY_PLUGIN_LOG_DIR, DECKY_PLUGIN_SETTINGS_DIR, DECKY_PLUGIN_VERSION; hooks _main, _migration, _unload | afterPatch, findClassModule, findModuleChild, findModuleDetailsByExport, findSP, getGamepadNavigationTrees | 2 | - | py_modules: mp; Docker backend | own websocket server on :2020 (native core in bin/) |
| MicroSDeck | v1 @decky/api / ESM | 0 | debug | routerHook.addPatch, routerHook.addRoute, routerHook.removePatch, routerHook.removeRoute, toaster.toast | no decky import; hooks _main, _unload | afterPatch, createReactTreePatcher, findInReactTree, findModule, findModuleChild | 0 | SP_REACT, collectionStore, MicroSDeck | Docker backend; native: Rust | ESM + api_version 0; publishes `window.MicroSDeck` for other plugins; Rust backend with its own HTTP port |
| MusicControl | v2 serverAPI / IIFE | 0 | - | call / callable / serverAPI.callPluginMethod, routerHook.addRoute, routerHook.removeRoute | import decky_plugin; DECKY_PLUGIN_RUNTIME_DIR, DECKY_USER_HOME; hooks _main | - | 0 | - | - | - |
| NonSteamLaunchers | v2 serverAPI / IIFE | 0 | debug | call / callable / serverAPI.callPluginMethod, toaster.toast | import decky_plugin; DECKY_PLUGIN_DIR, DECKY_PLUGIN_SETTINGS_DIR, DECKY_USER_HOME, logger; hooks _main, _migration, _unload; loader pkgs: aiohttp | - | 15 | appInfoStore, appStore, collectionStore | py_modules: externals, lib; needs pkgs: desktopC, desktopTM, requests | needs aiohttp from loader; own websocket server on :8675 |
| PowerTools | v2 serverAPI / IIFE | 0 | root, global-dfl | routerHook.addRoute, routerHook.removeRoute | no decky import; hooks _main, _unload | - | 5 | appStore | Docker backend; native: Rust | Rust backend + usdpl-front (own port, WASM client); `global-dfl` flag; no Python `decky` import |
| SDH-AnimationChanger | v2 serverAPI / IIFE | 0 | - | call / callable / serverAPI.callPluginMethod, routerHook.addRoute | import decky_plugin; DECKY_PLUGIN_LOG_DIR, DECKY_PLUGIN_RUNTIME_DIR, DECKY_PLUGIN_SETTINGS_DIR, logger, migrate_any, migrate_logs, migrate_settings; hooks _main, _migration, _unload; loader pkgs: aiohttp, certifi | findModule, findSP | 0 | - | - | needs aiohttp+certifi from loader; migrates ~/homebrew/animations |
| SDH-AudioLoader | v2 serverAPI / IIFE | 0 | _root | call / callable / serverAPI.callPluginMethod, fetchNoCors, routerHook.addRoute, toaster.toast | no decky import; hooks _main; loader pkgs: aiohttp | afterPatch, beforePatch, findModuleChild | 1 | AUDIOLOADER_MENUMUSIC | - | `_root` flag (not root); needs aiohttp from loader; sounds in ~/homebrew/sounds |
| SDH-CssLoader | v2 serverAPI / IIFE | 0 | - | call / callable / serverAPI.callPluginMethod, fetchNoCors, openFilePicker, routerHook.addRoute, toaster.toast | import decky_plugin; hooks _main; loader pkgs: aiohttp, watchdog | classMap, findModuleChild, getGamepadNavigationTrees, replacePatch | 0 | - | needs pkgs: PIL, pystray | Py backend talks to CEF DevTools on 127.0.0.1:8080 itself (websocket), creates `.cef-enable-remote-debugging` in the Steam dir and symlinks themes into steamui/themes_custom; needs aiohttp+watchdog from loader; watches ~/homebrew/themes |
| SDH-GameThemeMusic | v1 @decky/api / ESM | 1 | - | call / callable / serverAPI.callPluginMethod, definePlugin, routerHook.addPatch, routerHook.addRoute, routerHook.removePatch, routerHook.removeRoute, toaster.toast | import decky; DECKY_PLUGIN_DIR, DECKY_PLUGIN_RUNTIME_DIR, DECKY_PLUGIN_SETTINGS_DIR; hooks _main, _unload; **loader modules: settings**; loader pkgs: aiohttp, certifi | ReactRouter, afterPatch, createReactTreePatcher, fakeRenderComponent, findInReactTree, findInTree, findModuleByExport | 1 | LocalizationManager, appStore, AUDIOLOADER_MENUMUSIC | Docker backend; remote_binary: yt-dlp | needs aiohttp+certifi from loader; downloads yt-dlp to bin/ |
| SDH-PlayTime | v1 @decky/api / ESM | 1 | - | call / callable / serverAPI.callPluginMethod, openFilePicker, routerHook.addPatch, routerHook.addRoute, routerHook.removePatch, routerHook.removeRoute, toaster.toast | import decky; logger; hooks _main, _uninstall, _unload | afterPatch, findClass, findSP | 9 | App, SteamUIStore, appInfoStore, appStore, collectionStore | py_modules: association_manager, db, dto, files, games, helpers, schemas, statistics ... | hard-coded `http://127.0.0.1:1337/plugins/PlayTime/data/assets` (loader /data route) |
| SimpleDeckyTDP | v1 @decky/api / ESM | 1 | root | call / callable / serverAPI.callPluginMethod, definePlugin | import decky_plugin; DECKY_PLUGIN_VERSION, logger; hooks _main, _migration, _uninstall, _unload | - | 5 | LocalizationManager | py_modules: ac_power, advanced_options, charge_limit, cpu_utils, device_utils, devices, gpu_utils, i18n ...; Docker backend | runs `sudo systemctl restart plugin_loader.service` (OTA self-update); install scripts write into ~/homebrew/plugins |
| TabMaster | v1 @decky/api / ESM | 1 | debug | call / callable / serverAPI.callPluginMethod, definePlugin, openFilePicker, routerHook.addPatch, routerHook.addRoute, routerHook.removePatch, routerHook.removeRoute, routerHook.routerState, toaster.toast | import decky_plugin; logger; hooks _main, _migration, _unload; **loader modules: settings** | afterPatch, findInReactTree, findInTree, findModuleExport, replacePatch, wrapReactType | 4 | App, DeckyPluginLoader, LocalizationManager, appAchievementProgressCache, appStore, collectionStore, friendStore, loginStore, securitystore, settingsStore, MicroSDeck | - | `window.DeckyPluginLoader.pluginReloadQueue` and `.routerHook.routerState._routePatches` (private RouterHook state); reads `window.MicroSDeck`; `SP_REACT.__SECRET_INTERNALS...` |
| TunnelDeck | v2 serverAPI / IIFE | 0 | root | call / callable / serverAPI.callPluginMethod | no decky import; hooks _main, _unload; **loader modules: helpers, settings** | - | 0 | - | - | Py imports loader `helpers.get_user`; writes settings to hard-coded ~/homebrew/settings |
| bash-shortcuts | v2 serverAPI / IIFE (own DFL) | 0 | - | call / callable / serverAPI.callPluginMethod, routerHook.addRoute, routerHook.removeRoute, toaster.toast | no decky import; hooks _main, _unload; **loader modules: settings** | findModuleChild | 20 | App, appStore, collectionStore, loginStore | needs pkgs: webSocketClient | own websocket server; bundles its own DFL copy |
| deckfaqs | v2 serverAPI / IIFE | 0 | - | executeInTab, fetchNoCors, routerHook.addRoute, routerHook.removeRoute | no backend | findModuleChild, findSP | 6 | appStore, collectionStore | - | `executeInTab` + `fetchNoCors("http://localhost:8080/json")`; no backend (passive) |
| decky-autoflatpaks | v2 serverAPI / IIFE | 0 | root | call / callable / serverAPI.callPluginMethod, routerHook.addRoute, routerHook.removeRoute, toaster.toast | no decky import; **loader modules: helpers, settings** | findModuleChild | 1 | SystemPowerStore | - | Py imports loader `helpers.get_user_id` (+ dead refs to helpers.*Chown) and `settings` |
| decky-cloud-save | v2 serverAPI / IIFE | 0 | - | call / callable / serverAPI.callPluginMethod, openFilePicker, routerHook.addRoute, routerHook.removeRoute, toaster.toast | import decky_plugin; logger; hooks _main, _migration, _unload | - | 2 | SP_REACT, appStore | py_modules: decky_plugin, logger_utils, plugin_config, process_utils, rclone_setup_manager, rclone_sync_manager; Docker backend | - |
| decky-game-settings | v1 @decky/api / ESM | 1 | - | call / callable / serverAPI.callPluginMethod, definePlugin, fetchNoCors, toaster.toast | import decky; DECKY_HOME, DECKY_USER_HOME, emit, logger, migrate_logs, migrate_runtime, migrate_settings; hooks _main, _migration, _uninstall, _unload | - | 8 | collectionStore | - | - |
| decky-lsfg-vk | v1 @decky/api / ESM | 1 | - | call / callable / serverAPI.callPluginMethod, definePlugin, toaster.toast, useQuickAccessVisible | no decky import | - | 1 | LocalizationManager | py_modules: lsfg_vk; remote_binary: lsfg-vk-2.0.0.tar.xz | - |
| decky-networkinfo | v2 serverAPI / IIFE (own DFL) | 0 | - | call / callable / serverAPI.callPluginMethod | no decky import | - | 0 | - | - | bundles its own DFL copy |
| decky-recorder | v2 serverAPI / IIFE | 0 | debug | call / callable / serverAPI.callPluginMethod, openFilePicker, toaster.toast | import decky_plugin; DECKY_HOME, DECKY_PLUGIN_DIR, DECKY_PLUGIN_LOG_DIR, HOME, logger; hooks _main, _unload; **loader modules: settings** | - | 1 | - | Docker backend; needs pkgs: psutil | hard-codes `$HOME/homebrew/data/decky-recorder/...` path |
| decky-steamgriddb | v1 @decky/api / ESM | 1 | debug | call / callable / serverAPI.callPluginMethod, fetchNoCors, openFilePicker, routerHook.addPatch, routerHook.addRoute, routerHook.removePatch, routerHook.removeRoute, toaster.toast | import decky; DECKY_HOME, DECKY_PLUGIN_DIR, DECKY_PLUGIN_SETTINGS_DIR, DECKY_USER_HOME, logger, migrate_settings; hooks _main, _migration, _unload; **loader modules: helpers, settings** | IconsModule, afterPatch, callOriginal, createModuleMapping, fakeRenderComponent, findClassModule, findInReactTree, findInTree, findModule, findModuleByExport, findModuleExport, findSP, replacePatch, wrapReactType | 5 | App, LocalizationManager, appDetailsStore, appStore | needs pkgs: vdf | Py imports loader `helpers.get_ssl_context` and `settings` |
| decky-storage-cleaner | v1 @decky/api / ESM | 1 | - | call / callable / serverAPI.callPluginMethod | import decky; DECKY_PLUGIN_DIR, DECKY_USER_HOME | - | 1 | - | - | - |
| decky-terminal | v1 @decky/api / ESM | 1 | - | addEventListener, call / callable / serverAPI.callPluginMethod, removeEventListener, routerHook.addRoute, routerHook.removeRoute | import decky; logger; hooks _main, _migration, _unload | - | 0 | - | py_modules: decky_terminal | - |
| hltb-for-deck | v1 @decky/api / ESM | 1 | - | fetchNoCors, routerHook.addPatch, routerHook.addRoute, routerHook.removePatch, routerHook.removeRoute | no backend | afterPatch, createReactTreePatcher, fakeRenderComponent, findInReactTree, findInTree, findModuleChild, removeHookStubs | 0 | LocalizationManager | - | - |
| memory-deck | v2 serverAPI / IIFE | 0 | debug, root | call / callable / serverAPI.callPluginMethod, routerHook.addRoute, routerHook.removeRoute | no decky import; hooks _main | - | 0 | - | Docker backend; needs pkgs: consts, gi | - |
| moondeck | v1 @decky/api / ESM | 1 | - | call / callable / serverAPI.callPluginMethod, definePlugin, openFilePicker, routerHook.addPatch, routerHook.addRoute, routerHook.removePatch, routerHook.removeRoute, toaster.toast, useQuickAccessVisible | no decky import; hooks _main, _unload; loader pkgs: aiohttp | afterPatch, createReactTreePatcher, findInReactTree, findModuleExport, findSP | 20 | App, collectionStore | needs pkgs: pyglet, wakeonlan, zeroconf | vendors aiohttp etc. in defaults/python/externals; uses async_timeout |
| protondb-decky | v1 @decky/api / ESM | 2 | - | call / callable / serverAPI.callPluginMethod, fetchNoCors, routerHook.addGlobalComponent, routerHook.addPatch, routerHook.removeGlobalComponent, routerHook.removePatch, toaster.toast | import decky_plugin; DECKY_PLUGIN_SETTINGS_DIR; hooks _main, _unload | afterPatch, createReactTreePatcher, findInReactTree, findModuleExport | 0 | App, LocalizationManager, SteamUIStore, appStore, webpackChunksteamui | - | `fetchNoCors("http://localhost:8080/json")` (CEF DevTools list); pushes a chunk into `webpackChunksteamui` |
| reshadeck | v2 serverAPI / IIFE | 0 | debug | call / callable / serverAPI.callPluginMethod, toaster.toast | import decky_plugin; DECKY_PLUGIN_DIR, DECKY_USER_HOME, logger; hooks _main | - | 3 | - | needs pkgs: click | - |
| vibrantDeck | v2 serverAPI / IIFE | 0 | - | call / callable / serverAPI.callPluginMethod | no decky import | - | 0 | - | - | - |
| volume-boost | v2 serverAPI / IIFE | 0 | - | call / callable / serverAPI.callPluginMethod | import decky_plugin; logger | - | 0 | - | - | - |
| kettle/crash | v1 @decky/api / ESM | 1 | - | addEventListener, call / callable / serverAPI.callPluginMethod, definePlugin, removeEventListener, routerHook.addRoute, routerHook.removeRoute, toaster.toast | import decky; DECKY_PLUGIN_SETTINGS_DIR, DECKY_USER_HOME, emit, logger; hooks _main, _unload | - | 0 | - | py_modules: inotify | - |
| kettle/device-settings | v1 @decky/api / ESM | 1 | - | addEventListener, call / callable / serverAPI.callPluginMethod, definePlugin, removeEventListener, routerHook.addRoute, routerHook.removeRoute, toaster.toast | import decky; DECKY_PLUGIN_SETTINGS_DIR, DECKY_USER_HOME, emit, logger; hooks _main, _unload | findModuleExport | 1 | appStore | py_modules: buttons, gyro, inotify, lights, powerd, refresh, screens, system; needs pkgs: gi | - |
| kettle/game-settings | v1 @decky/api / ESM | 1 | - | call / callable / serverAPI.callPluginMethod, definePlugin, routerHook.addRoute, routerHook.removeRoute, toaster.toast | import decky; DECKY_PLUGIN_DIR, DECKY_PLUGIN_SETTINGS_DIR, DECKY_USER_HOME, logger; hooks _main, _unload | findModuleChild | 8 | appDetailsStore, appStore | py_modules: configfix, extras, framegen, gameengine, lumen, powerd, refresh, steamlib ...; needs pkgs: gi | - |
| kettle/stores | v1 @decky/api / ESM | 1 | - | addEventListener, call / callable / serverAPI.callPluginMethod, definePlugin, openFilePicker, removeEventListener, routerHook.addRoute, routerHook.removeRoute, toaster.toast | import decky; DECKY_PLUGIN_DIR, DECKY_PLUGIN_SETTINGS_DIR, DECKY_USER_HOME, emit, logger; hooks _main, _uninstall, _unload | - | 11 | appStore | py_modules: cdp, steamlib | own CEF DevTools client to 127.0.0.1:8080 (relies on the loader enabling remote debugging); reads sibling plugins' settings dirs (`~/homebrew/settings/kettle-*`) |
| kettle/welcome | v1 @decky/api / ESM | 1 | - | call / callable / serverAPI.callPluginMethod, definePlugin, routerHook.addRoute, routerHook.removeRoute | import decky; DECKY_PLUGIN_SETTINGS_DIR, DECKY_USER_HOME, logger; hooks _main, _unload | findModuleChild | 9 | DeckyPluginLoader, appDetailsStore, appStore | py_modules: steamlib | `window.DeckyPluginLoader.deckyState.setActivePlugin(name)` (opens a plugin in the QAM) |

### 1a. Per-plugin detail (full lists)

<details><summary><b>Bluetooth</b></summary>

- @decky/ui / DFL (runtime names): DialogButton, Field, Focusable, PanelSection, PanelSectionRow, ToggleField, definePlugin, staticClasses
- SteamClient: -
- Globals/stores members: LocalizationManager.m_mapFallbackTokens, LocalizationManager.m_mapTokens
- Backend env/decky: DECKY_HOME, DECKY_USER_HOME, logger, migrate_logs, migrate_runtime, migrate_settings

</details>
<details><summary><b>ControllerTools</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, PanelSection, PanelSectionRow, ToggleField, definePlugin, gamepadDialogClasses, joinClassNames, staticClasses
- SteamClient: -
- Globals/stores members: -
- Backend env/decky: DECKY_HOME, DECKY_PLUGIN_DIR, DECKY_PLUGIN_LOG, DECKY_PLUGIN_SETTINGS_DIR, logger
- internals grep hits: loader websocket (ws://) [fe:src/notifications.ts]

</details>
<details><summary><b>DeckWebBrowser</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, ConfirmModal, DialogBodyText, DialogButton, DialogButtonSecondary, DialogSubHeader, DropdownItem, Field, Focusable, GamepadButton, Marquee, Menu, MenuGroup, MenuItem, ModalPosition, ModalRoot, Navigation, PanelSection, PanelSectionRow, Router, SimpleModal, SteamSpinner, Tab, Tabs, TextField, ToggleField, afterPatch, definePlugin, findClassModule, findInReactTree, findModuleChild, gamepadContextMenuClasses, gamepadDialogClasses, gamepadTabbedPageClasses, getFocusNavController, getReactRoot, quickAccessControlsClasses, replacePatch, showContextMenu, showModal, sleep, staticClasses
- @decky/ui type-only imports: DropdownProps, FieldProps, FocusableProps, FooterLegendProps, GamepadEvent, GamepadEventDetail, MenuGroupProps, MenuItemProps, Module, PatchOptions, ServerAPI, ServerResponse, SingleDropdownOption
- SteamClient: GameSessions.RegisterForAppLifetimeNotifications, Input.ControllerKeyboardSetKeyState, Input.SetMousePosition, Overlay.RegisterForOverlayActivated, System, System.RegisterForOnResumeFromSuspend
- Globals/stores members: settingsStore.m_ClientSettings
- `window.*` read: getSelection, location, scriptLoaded, stream
- Backend env/decky: DECKY_PLUGIN_DIR, DECKY_PLUGIN_SETTINGS_DIR, logger
- internals grep hits: CEF debugger 8080 [fe:src/classes/TabManager.ts]; CEF debugger 8080 [fe:src/components/QAMContent.tsx]; loader websocket (ws://) [fe:client.js]; loader websocket (ws://) [fe:src/classes/WSManager.ts]
- dev/install scripts touching loader: plugin_loader service [Makefile]; ~/homebrew path [Makefile]

</details>
<details><summary><b>Decky-Framegen</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, ConfirmModal, DropdownItem, Field, PanelSection, PanelSectionRow, ToggleField, showModal
- SteamClient: Apps.RegisterForAppDetails, Apps.SetAppLaunchOptions
- Globals/stores members: -
- `window.*` read: clearTimeout, open, setTimeout
- Backend env/decky: DECKY_PLUGIN_DIR, HOME, logger

</details>
<details><summary><b>Decky-Undervolt</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, DialogBody, DialogButton, DialogFooter, DialogHeader, DropdownItem, ModalRoot, Navigation, PanelSection, PanelSectionRow, Router, SidebarNavigation, SliderField, TextField, ToggleField, definePlugin, showModal
- SteamClient: GameSessions.RegisterForAppLifetimeNotifications, System.RegisterForOnResumeFromSuspend
- Globals/stores members: appStore.GetAppOverviewByGameID
- Backend env/decky: DECKY_PLUGIN_DIR, DECKY_PLUGIN_SETTINGS_DIR, emit, logger
- dev/install scripts touching loader: plugin_loader service [install.sh]; ~/homebrew path [install.sh]

</details>
<details><summary><b>DiscordStatus</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, DropdownItem, Field, PanelSection, PanelSectionRow, Router, Spinner, TextField, sleep, staticClasses
- @decky/ui type-only imports: AppDetails, AppLifetimeNotification, ESuspendResumeProgressState, SuspendProgress
- deep imports: @decky/ui/dist/globals/steam-client/App, @decky/ui/dist/globals/steam-client/GameSessions, @decky/ui/dist/globals/steam-client/User
- SteamClient: Apps.RegisterForAppDetails, Apps.RunGame, GameSessions.RegisterForAppLifetimeNotifications, User.RegisterForPrepareForSystemSuspendProgress, User.RegisterForResumeSuspendedGamesProgress
- Globals/stores members: appStore.GetAppOverviewByAppID, appStore.GetAppOverviewByGameID, appStore.GetCustomVerticalCapsuleURLs, appStore.GetVerticalCapsuleURLForApp
- `window.*` read: __DISCORD_STATUS_BUILD, addEventListener, console, localStorage, removeEventListener
- Backend env/decky: DECKY_PLUGIN_SETTINGS_DIR, logger

</details>
<details><summary><b>Emuchievements</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, Dropdown, Field, Focusable, Navigation, PanelSection, PanelSectionRow, ProgressBarItem, ProgressBarWithInfo, Router, SidebarNavigation, TextField, ToggleField, afterPatch, beforePatch, callOriginal, findInTree, findModuleChild, replacePatch, sleep, staticClasses
- @decky/ui type-only imports: Patch
- SteamClient: Apps.RegisterForAppDetails, GameSessions.RegisterForAppLifetimeNotifications, User.RegisterForLoginStateChange
- Globals/stores members: App.WaitForServicesInitialized, LocalizationManager.m_rgLocalesToUse, appAchievementProgressCache.RequestCacheUpdate, appAchievementProgressCache.SaveCacheFile, appAchievementProgressCache.m_achievementProgress, appDetailsCache.SetCachedDataForApp, appDetailsStore.GetAchievements, appDetailsStore.GetAppData, appStore.GetAppOverviewByAppID, appStore.allApps, collectionStore.BIsHidden, collectionStore.SetAppsAsHidden, collectionStore.deckDesktopApps
- `window.*` read: App, LocalizationManager, console
- Backend env/decky: DECKY_HOME, DECKY_PLUGIN_DIR, DECKY_PLUGIN_SETTINGS_DIR, migrate_settings
- internals grep hits: other plugins dir (/plugins/) [py:main.py]; other plugins dir (/plugins/) [py:src/py/main.py]

</details>
<details><summary><b>HueSync</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, DialogButton, DropdownItem, Field, Focusable, Marquee, ModalRoot, PanelSection, PanelSectionRow, Router, SliderField, Spinner, TextField, ToggleField, definePlugin, findModuleChild, gamepadSliderClasses, showModal, staticClasses
- @decky/ui type-only imports: ButtonItemProps, DropdownOption, ItemProps, Module, NotchLabel, SuspendProgress, SystemInfo
- deep imports: @decky/ui/dist/components/Item, @decky/ui/dist/globals/steam-client/User, @decky/ui/dist/globals/steam-client/system
- SteamClient: Settings.GetCurrentLanguage, System.GetSystemInfo, System.RegisterForBatteryStateChanges, System.RegisterForOnResumeFromSuspend, System.RegisterForOnSuspendRequest, User.RegisterForPrepareForSystemSuspendProgress, User.RegisterForResumeSuspendedGamesProgress
- Globals/stores members: -
- `window.*` read: devicePixelRatio
- Backend env/decky: DECKY_HOME, DECKY_PLUGIN_LOG_DIR, DECKY_PLUGIN_RUNTIME_DIR, DECKY_PLUGIN_SETTINGS_DIR, DECKY_USER_HOME, logger, migrate_logs, migrate_runtime, migrate_settings
- py_modules shipped: config, custom_rgb_manager, custom_zone_animator, devices, ec, huesync, id_info, led, lib_hid, logging_handler, portio.so, power_led_backend, serial, software_effects, submodule, update, utils, wincontrols
- internals grep hits: ~/homebrew path [py:main.py]
- dev/install scripts touching loader: plugin_loader service [Makefile]; plugin_loader service [install.sh]; ~/homebrew path [Makefile]; ~/homebrew path [install.sh]

</details>
<details><summary><b>Junk-Store</b></summary>

- @decky/ui / DFL (runtime names): Button, ButtonItem, ConfirmModal, DialogBody, DialogButton, DialogControlsSection, DialogLabel, Dropdown, Field, Focusable, GamepadButton, Marquee, Menu, MenuItem, ModalPosition, ModalRoot, Navigation, PanelSection, PanelSectionRow, ProgressBar, ScrollPanelGroup, SidebarNavigation, SidebarNavigationPage, SliderField, Spinner, SteamSpinner, Tab, Tabs, TextField, ToggleField, afterPatch, definePlugin, findClassModule, gamepadDialogClasses, gamepadTabbedPageClasses, joinClassNames, quickAccessControlsClasses, quickAccessMenuClasses, scrollPanelClasses, showContextMenu, showModal, sleep, staticClasses, useParams
- @decky/ui type-only imports: FocusableProps, FooterLegendProps, ModalRootProps, ServerAPI, ToastData, Toaster
- SteamClient: Apps.AddShortcut, Apps.GetAvailableCompatTools, Apps.RemoveShortcut, Apps.RunGame, Apps.SetAppLaunchOptions, Apps.SetCustomArtworkForApp, Apps.SetShortcutExe, Apps.SetShortcutLaunchOptions, Apps.SetShortcutName, Apps.SetShortcutStartDir, Apps.SpecifyCompatTool, GameSessions.RegisterForAppLifetimeNotifications, Input.RegisterForControllerInputMessages, Installs.OpenInstallWizard
- Globals/stores members: SteamUIStore.WindowStore, appDetailsCache.FetchDataForApp, appDetailsStore.GetAppDetails, appDetailsStore.RequestAppDetails, appStore.GetAppOverviewByAppID, appStore.allApps, settingsStore.settings
- `window.*` read: console
- Backend env/decky: DECKY_HOME, DECKY_PLUGIN_DIR, DECKY_PLUGIN_LOG_DIR, DECKY_PLUGIN_NAME, DECKY_PLUGIN_RUNTIME_DIR, DECKY_PLUGIN_SETTINGS_DIR, DECKY_USER, DECKY_USER_HOME, logger, migrate_logs, migrate_runtime, migrate_settings
- internals grep hits: loader HTTP port 1337 [py:main.py]; loader websocket (ws://) [fe:src/About.tsx]; ~/homebrew path [py:main.py]
- dev/install scripts touching loader: ~/homebrew path [defaults/scripts/Extensions/Epic/epic-launcher.sh]; ~/homebrew path [defaults/scripts/Extensions/Epic/get-epic-args.sh]; ~/homebrew path [defaults/scripts/Extensions/Epic/login.sh]; ~/homebrew path [defaults/scripts/install_deps.sh]; ~/homebrew path [defaults/scripts/settings.sh]

</details>
<details><summary><b>MagicPodsDecky</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, DialogButton, Field, Focusable, Menu, MenuItem, ModalRoot, Navigation, PanelSection, PanelSectionRow, Router, SliderField, Tabs, ToggleField, afterPatch, definePlugin, findClassModule, findModuleChild, findModuleDetailsByExport, findSP, gamepadDialogClasses, gamepadSliderClasses, getGamepadNavigationTrees, joinClassNames, quickAccessMenuClasses, showContextMenu, showModal, staticClasses
- @decky/ui type-only imports: Patch
- SteamClient: Input.RegisterForControllerStateChanges, System.Audio
- Globals/stores members: -
- `window.*` read: clearInterval, setInterval
- Backend env/decky: DECKY_PLUGIN_DIR, DECKY_PLUGIN_LOG_DIR, DECKY_PLUGIN_SETTINGS_DIR, DECKY_PLUGIN_VERSION
- py_modules shipped: mp
- internals grep hits: loader websocket (ws://) [fe:src/backend.tsx]

</details>
<details><summary><b>MicroSDeck</b></summary>

- @decky/ui / DFL (runtime names): ConfirmModal, DialogButton, DialogCheckbox, Field, Focusable, Menu, MenuItem, ModalPosition, Navigation, PanelSection, ReorderableList, ScrollPanelGroup, SidebarNavigation, SidebarNavigationPage, SimpleModal, TextField, ToggleField, afterPatch, appDetailsClasses, createReactTreePatcher, definePlugin, findInReactTree, findModule, findModuleChild, gamepadDialogClasses, quickAccessControlsClasses, showContextMenu, showModal, staticClasses
- @decky/ui type-only imports: Module, ReorderableEntry
- SteamClient: -
- Globals/stores members: SP_REACT.createElement, collectionStore.deckDesktopApps
- `window.*` read: MicroSDeck, SP_REACT
- Backend env/decky: -

</details>
<details><summary><b>MusicControl</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, DialogButton, Focusable, PanelSection, PanelSectionRow, Router, SidebarNavigation, SliderField, SteamSpinner, definePlugin, staticClasses
- @decky/ui type-only imports: ServerAPI
- SteamClient: -
- Globals/stores members: -
- Backend env/decky: DECKY_PLUGIN_RUNTIME_DIR, DECKY_USER_HOME

</details>
<details><summary><b>NonSteamLaunchers</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, ConfirmModal, DialogBody, DialogBodyText, DialogButton, DialogHeader, Focusable, ModalRoot, PanelSection, PanelSectionRow, ProgressBarWithInfo, SteamSpinner, TextField, ToggleField, definePlugin, showModal, staticClasses
- @decky/ui type-only imports: ServerAPI
- SteamClient: Apps.AddShortcut, Apps.CreateDesktopShortcutForApp, Apps.RegisterForGameActionStart, Apps.SetAppLaunchOptions, Apps.SetCustomArtworkForApp, Apps.SetShortcutExe, Apps.SetShortcutIcon, Apps.SetShortcutName, Apps.SetShortcutSortAs, Apps.SetShortcutStartDir, Apps.SpecifyCompatTool, Apps.TerminateApp, GameSessions.RegisterForAppLifetimeNotifications, Overlay, Overlay.SetOverlayState, User.StartRestart
- Globals/stores members: appInfoStore.OnAppOverviewChange, appInfoStore._originalOnAppOverviewChange, appStore.GetAppOverviewByAppID, appStore.m_mapApps, collectionStore.DeleteCollection, collectionStore.GetCollection, collectionStore.GetCollectionIDByUserTag, collectionStore.NewUnsavedCollection, collectionStore.collectionsFromStorage
- `window.*` read: appInfoStore, appStore, collectionStore, g_CollectionStore, open
- Backend env/decky: DECKY_PLUGIN_DIR, DECKY_PLUGIN_SETTINGS_DIR, DECKY_USER_HOME, logger
- py_modules shipped: externals, lib
- internals grep hits: loader websocket (ws://) [fe:src/hooks/getUpdate.tsx]; loader websocket (ws://) [fe:src/hooks/installSites.tsx]; loader websocket (ws://) [fe:src/hooks/scan.tsx]; loader websocket (ws://) [fe:src/hooks/useLauncherStatus.tsx]; loader websocket (ws://) [fe:src/hooks/useLogUpdates.tsx]
- dev/install scripts touching loader: ~/homebrew path [NonSteamLaunchers.sh]

</details>
<details><summary><b>PowerTools</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, DialogButton, Dropdown, Field, Focusable, ModalRoot, Navigation, PanelSection, PanelSectionRow, QuickAccessTab, Router, SliderField, Spinner, TextField, ToggleField, definePlugin, showModal, staticClasses
- @decky/ui type-only imports: ModalRootProps, ServerAPI, ShowModalResult, SingleDropdownOption
- SteamClient: Apps.RegisterForGameActionEnd, Apps.RegisterForGameActionStart, GameSessions.RegisterForAppLifetimeNotifications, User.GetLoginUsers, User.RegisterForCurrentUserChanges
- Globals/stores members: appStore.GetAppOverviewByGameID
- `window.*` read: globalThis, window
- Backend env/decky: -
- dev/install scripts touching loader: CEF debugger 8080 [Makefile]; plugin_loader service [Makefile]

</details>
<details><summary><b>SDH-AnimationChanger</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, DialogButton, Dropdown, DropdownItem, Focusable, ModalRoot, Navigation, PanelSection, PanelSectionRow, Router, Spinner, Tabs, TextField, ToggleField, definePlugin, findModule, findSP, showModal, staticClasses
- @decky/ui type-only imports: DropdownOption, ModalRootProps, ServerAPI
- SteamClient: -
- Globals/stores members: -
- Backend env/decky: DECKY_PLUGIN_LOG_DIR, DECKY_PLUGIN_RUNTIME_DIR, DECKY_PLUGIN_SETTINGS_DIR, logger, migrate_any, migrate_logs, migrate_settings
- internals grep hits: ~/homebrew path [py:main.py]

</details>
<details><summary><b>SDH-AudioLoader</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, DialogButton, Dropdown, DropdownItem, Focusable, Navigation, PanelSection, PanelSectionRow, Router, SliderField, Tabs, TextField, ToggleField, afterPatch, beforePatch, definePlugin, findModuleChild, gamepadDialogClasses, staticClasses
- @decky/ui type-only imports: DropdownOption, Module, ServerAPI, SingleDropdownOption
- SteamClient: GameSessions.RegisterForAppLifetimeNotifications
- Globals/stores members: -
- `window.*` read: AUDIOLOADER_MENUMUSIC
- Backend env/decky: DECKY_HOME, DECKY_USER, DECKY_USER_HOME
- internals grep hits: ~/homebrew path [fe:src/classes.ts]

</details>
<details><summary><b>SDH-CssLoader</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, ColorPickerModal, ConfirmModal, DialogButton, DialogCheckbox, Dropdown, DropdownItem, Focusable, ModalRoot, Navigation, Panel, PanelSection, PanelSectionRow, ScrollPanelGroup, SidebarNavigation, SliderField, Tabs, TextField, Toggle, ToggleField, classMap, definePlugin, findModuleChild, gamepadDialogClasses, gamepadSliderClasses, getGamepadNavigationTrees, replacePatch, showModal, staticClasses
- @decky/ui type-only imports: DropdownOption, Module, Patch, ServerAPI, SingleDropdownOption
- SteamClient: -
- Globals/stores members: -
- `window.*` read: getComputedStyle
- Backend env/decky: DECKY_HOME, DECKY_USER
- internals grep hits: CEF debugger 8080 [py:css_browserhook.py]; CEF remote-debug flag file [py:css_utils.py]; SharedJSContext / tab names [py:css_inject.py]; ~/homebrew path [fe:src/pages/settings/PluginSettings.tsx]; ~/homebrew path [py:css_utils.py]

</details>
<details><summary><b>SDH-GameThemeMusic</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, ConfirmModal, DialogButton, DropdownItem, Field, Focusable, Menu, MenuItem, ModalRoot, Navigation, PanelSection, PanelSectionRow, ProgressBarWithInfo, ReactRouter, SliderField, SteamSpinner, Tabs, TextField, afterPatch, appDetailsClasses, createReactTreePatcher, fakeRenderComponent, findInReactTree, findInTree, findModuleByExport, showContextMenu, showModal, staticClasses, useParams
- @decky/ui type-only imports: Patch, ShowModalResult
- SteamClient: GameSessions.RegisterForAppLifetimeNotifications
- Globals/stores members: LocalizationManager.m_rgLocalesToUse, appStore.GetAppOverviewByGameID
- `window.*` read: AUDIOLOADER_MENUMUSIC, LocalizationManager
- Backend env/decky: DECKY_PLUGIN_DIR, DECKY_PLUGIN_RUNTIME_DIR, DECKY_PLUGIN_SETTINGS_DIR

</details>
<details><summary><b>SDH-PlayTime</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, ConfirmModal, DialogButton, Dropdown, Field, Focusable, Menu, MenuGroup, MenuItem, ModalRoot, Navigation, PanelSection, PanelSectionRow, ScrollPanelGroup, SidebarNavigation, SidebarNavigationPage, Tabs, TextField, afterPatch, definePlugin, findClass, findSP, showContextMenu, showModal, staticClasses, useParams
- @decky/ui type-only imports: DropdownOption
- SteamClient: Apps.RegisterForAppDetails, Input.RegisterForControllerInputMessages, Storage.GetJSON, Storage.SetObject, System, System.RegisterForOnResumeFromSuspend, System.RegisterForOnSuspendRequest, User, User.RegisterForLoginStateChange, User.RegisterForPrepareForSystemSuspendProgress, User.RegisterForResumeSuspendedGamesProgress
- Globals/stores members: App.m_CurrentUser, SteamUIStore.RunningApps, appInfoStore.OnAppOverviewChange, appInfoStore.OriginalOnAppOverviewChange, appStore.GetAppOverviewByAppID, appStore.GetAppOverviewByGameID, appStore.GetCustomImageURLs, appStore.GetVerticalCapsuleURLForApp, appStore.allApps, appStore.m_mapApps, collectionStore.deckDesktopApps
- Backend env/decky: DECKY_PLUGIN_DIR, DECKY_PLUGIN_RUNTIME_DIR, DECKY_USER_HOME, logger
- py_modules shipped: __init__, association_manager, db, dto, files, games, helpers, schemas, statistics, tests, time_tracking, tracking_manager, user_manager, utils
- internals grep hits: DECKY_* env vars [fe:src/app/backend.ts]; DECKY_* env vars [fe:src/constants.ts]; loader HTTP port 1337 [fe:src/components/GameCard.tsx]

</details>
<details><summary><b>SimpleDeckyTDP</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, Field, PanelSection, PanelSectionRow, Router, SliderField, ToggleField
- SteamClient: System.RegisterForBatteryStateChanges, System.RegisterForOnResumeFromSuspend, System.RegisterForOnSuspendRequest, User.RegisterForPrepareForSystemSuspendProgress, User.RegisterForResumeSuspendedGamesProgress
- Globals/stores members: LocalizationManager.m_rgLocalesToUse
- `window.*` read: LocalizationManager, clearInterval, localStorage, setInterval, setTimeout
- Backend env/decky: DECKY_PLUGIN_VERSION, logger
- py_modules shipped: ac_power, advanced_options, charge_limit, cpu_utils, device_utils, devices, gpu_utils, i18n, migrations, plugin_enums, plugin_settings, plugin_timeout, plugin_update, plugin_utils, power_utils, ryzenadj, steam_info
- internals grep hits: decky_loader import [py:main.py]
- dev/install scripts touching loader: plugin_loader service [desktop_reload.sh]; plugin_loader service [install.sh]; plugin_loader service [ota_update.sh]; plugin_loader service [reload.sh]; ~/homebrew path [desktop_reload.sh]; ~/homebrew path [install.sh]; ~/homebrew path [ota_update.sh]; ~/homebrew path [reload.sh]

</details>
<details><summary><b>TabMaster</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, ConfirmModal, DialogBody, DialogButton, DialogCheckbox, DialogControlsSection, DialogHeader, Dropdown, DropdownItem, Field, Focusable, GamepadButton, Marquee, Menu, MenuGroup, MenuItem, ModalPosition, ModalRoot, Navigation, PanelSection, PanelSectionRow, ReorderableList, ScrollPanelGroup, SidebarNavigation, SidebarNavigationPage, SliderField, TextField, ToggleField, achievementClasses, afterPatch, findInReactTree, findInTree, findModuleExport, gamepadContextMenuClasses, gamepadDialogClasses, gamepadSliderClasses, gamepadTabbedPageClasses, joinClassNames, mainMenuAppRunningClasses, quickAccessControlsClasses, quickAccessMenuClasses, replacePatch, scrollPanelClasses, showContextMenu, showModal, sleep, wrapReactType
- @decky/ui type-only imports: AppAchievements, ConfirmModalProps, DropdownOption, DropdownProps, FieldProps, FocusableProps, FooterLegendProps, GamepadEvent, MenuItemProps, Patch, ReorderableEntry, Screenshot, SingleDropdownOption, SliderFieldProps
- deep imports: @decky/ui/dist/globals/steam-client/App, @decky/ui/dist/globals/steam-client/Screenshots
- SteamClient: Apps.GetStoreTagLocalization, System, System.RegisterForOnResumeFromSuspend, System.RestartPC, User.RegisterForLoginStateChange
- Globals/stores members: App.WaitForServicesInitialized, App.m_CurrentUser, DeckyPluginLoader.pluginReloadQueue, DeckyPluginLoader.routerHook, LocalizationManager.LocalizeString, LocalizationManager.m_mapFallbackTokens, LocalizationManager.m_mapTokens, LocalizationManager.m_rgLocalesToUse, appAchievementProgressCache.GetAchievementProgress, appAchievementProgressCache.m_achievementProgress, appStore.m_mapStoreTagLocalization, collectionStore.GetCollection, collectionStore.GetUserCollectionsByName, collectionStore.NewUnsavedCollection, collectionStore.appTypeCollectionMap, collectionStore.sharedLibrariesCollections, collectionStore.userCollections, friendStore.allFriends, loginStore.m_strAccountName, securitystore.IsLockScreenActive, settingsStore.m_ClientSettings
- `window.*` read: App, LocalizationManager, MicroSDeck
- Backend env/decky: DECKY_PLUGIN_SETTINGS_DIR, DECKY_USER_HOME, logger
- internals grep hits: ~/homebrew path [fe:src/components/qam/InvalidSettingsNotice.tsx]

</details>
<details><summary><b>TunnelDeck</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, Field, Navigation, PanelSection, PanelSectionRow, ToggleField, definePlugin, staticClasses
- @decky/ui type-only imports: ServerAPI
- SteamClient: -
- Globals/stores members: -
- `window.*` read: setTimeout
- Backend env/decky: -
- internals grep hits: ~/homebrew path [py:main.py]

</details>
<details><summary><b>bash-shortcuts</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, ConfirmModal, DialogButton, Dropdown, Field, Focusable, GamepadButton, Navigation, PanelSection, PanelSectionRow, ReorderableList, SidebarNavigation, TextField, ToggleField, definePlugin, findModuleChild, gamepadDialogClasses, quickAccessControlsClasses, showModal, sleep, staticClasses
- @decky/ui type-only imports: DropdownOption, FieldProps, FocusableProps, FooterLegendProps, GamepadEvent, ReorderableEntry, ServerAPI, ServerResponse
- SteamClient: Apps.AddShortcut, Apps.RegisterForAppDetails, Apps.RegisterForGameActionEnd, Apps.RegisterForGameActionStart, Apps.RemoveShortcut, Apps.RunGame, Apps.SetAppLaunchOptions, Apps.SetShortcutExe, Apps.SetShortcutName, Apps.SetShortcutStartDir, Apps.TerminateApp, Downloads.RegisterForDownloadItems, Downloads.RegisterForDownloadOverview, GameSessions.RegisterForAchievementNotification, GameSessions.RegisterForAppLifetimeNotifications, GameSessions.RegisterForScreenshotNotification, User.RegisterForLoginStateChange, User.RegisterForPrepareForSystemSuspendProgress, User.RegisterForShutdownStart, User.StartRestart
- Globals/stores members: App.WaitForServicesInitialized, appStore.GetAppOverviewByAppID, collectionStore.BIsHidden, collectionStore.SetAppsAsHidden, collectionStore.allAppsCollection, collectionStore.deckDesktopApps, collectionStore.localGamesCollection, collectionStore.userCollections, loginStore.m_strAccountName
- `window.*` read: App
- Backend env/decky: DECKY_PLUGIN_LOG_DIR, DECKY_PLUGIN_SETTINGS_DIR, DECKY_USER
- internals grep hits: loader websocket (ws://) [fe:src/WebsocketClient.ts]; loader websocket (ws://) [py:defaults/py_backend/jsInterop.py]; loader websocket (ws://) [py:defaults/py_backend/serverTest.py]; other plugins dir (/plugins/) [fe:src/lib/controllers/InstancesController.ts]; other plugins dir (/plugins/) [py:main.py]; ~/homebrew path [fe:src/lib/controllers/InstancesController.ts]; ~/homebrew path [py:defaults/py_backend/serverTest.py]; ~/homebrew path [py:main.py]

</details>
<details><summary><b>deckfaqs</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, DialogButton, Dropdown, Focusable, ModalRoot, Navigation, PanelSection, PanelSectionRow, QuickAccessTab, Router, TextField, ToggleField, definePlugin, findModuleChild, findSP, showModal, staticClasses
- @decky/ui type-only imports: ModalRootProps, ServerAPI
- SteamClient: Apps.RegisterForGameActionStart, BrowserView.Destroy, GameSessions.RegisterForAppLifetimeNotifications, InstallFolder.GetInstallFolders, Storage.GetJSON, Storage.SetObject
- Globals/stores members: appStore.GetAppOverviewByGameID, collectionStore.deckDesktopApps
- `window.*` read: console, navigator, setTimeout
- Backend env/decky: -
- internals grep hits: CEF debugger 8080 [fe:src/utils.ts]; executeInTab/execute_in_tab [fe:src/utils.ts]
- dev/install scripts touching loader: plugin_loader service [Makefile]; plugin_loader service [scripts/uninstall.sh]; ~/homebrew path [Makefile]; ~/homebrew path [scripts/install_plugin.sh]; ~/homebrew path [scripts/uninstall.sh]

</details>
<details><summary><b>decky-autoflatpaks</b></summary>

- @decky/ui / DFL (runtime names): ConfirmModal, DialogButton, Dropdown, Focusable, ModalRoot, PanelSection, PanelSectionRow, Router, ScrollPanel, SteamSpinner, Tabs, TextField, Toggle, ToggleField, definePlugin, findModuleChild, showModal, staticClasses
- @decky/ui type-only imports: ConfirmModalProps, DialogButtonProps, Module, ServerAPI, ToggleProps
- SteamClient: InstallFolder.GetInstallFolders
- Globals/stores members: -
- `window.*` read: SystemPowerStore
- Backend env/decky: DECKY_HOME, DECKY_PLUGIN_LOG_DIR, DECKY_PLUGIN_SETTINGS_DIR, DECKY_USER_HOME

</details>
<details><summary><b>decky-cloud-save</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, ConfirmModal, Menu, MenuItem, Navigation, PanelSection, PanelSectionRow, TextField, ToggleField, definePlugin, showContextMenu, showModal, sleep, staticClasses
- @decky/ui type-only imports: FilePickerRes, LifetimeNotification, ServerAPI
- SteamClient: GameSessions.RegisterForAppLifetimeNotifications, Settings.GetCurrentLanguage
- Globals/stores members: SP_REACT.createElement, appStore.GetAppOverviewByGameID
- `window.*` read: SP_REACT
- Backend env/decky: logger
- py_modules shipped: decky_plugin, logger_utils, plugin_config, process_utils, rclone_setup_manager, rclone_sync_manager
- dev/install scripts touching loader: ~/homebrew path [backend/openLastLog.sh]

</details>
<details><summary><b>decky-game-settings</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, ConfirmModal, DialogBody, DialogButton, Dropdown, Field, Focusable, GamepadButton, ModalRoot, Navigation, PanelSection, PanelSectionRow, Router, ScrollPanelGroup, SliderField, Spinner, TextField, ToggleField, gamepadDialogClasses, scrollPanelClasses, showModal, staticClasses
- @decky/ui type-only imports: AppLifetimeNotification, FooterLegendProps, ModalRootProps
- deep imports: @decky/ui/dist/globals/steam-client/GameSessions
- SteamClient: GameSessions.RegisterForAppLifetimeNotifications, InstallFolder.GetInstallFolders, Screenshots.GetAllAppsLocalScreenshots, Screenshots.GetAllLocalScreenshots, Screenshots.GetLocalScreenshotPath, System, System.GetSystemInfo, System.OpenInSystemBrowser, User.StartRestart
- Globals/stores members: collectionStore.allGamesCollection
- `window.*` read: collectionStore, localStorage
- Backend env/decky: DECKY_HOME, DECKY_USER_HOME, decky_LOG_DIR, decky_RUNTIME_DIR, decky_SETTINGS_DIR, emit, logger, migrate_logs, migrate_runtime, migrate_settings
- internals grep hits: ~/homebrew path [py:main.py]

</details>
<details><summary><b>decky-lsfg-vk</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, ConfirmModal, DialogButton, Field, Focusable, ModalRoot, PanelSection, PanelSectionRow, Router, SliderField, Spinner, Tabs, ToggleField, gamepadDialogClasses, showModal, staticClasses
- SteamClient: Apps, Apps.TerminateApp
- Globals/stores members: LocalizationManager.m_rgLocalesToUse
- `window.*` read: LocalizationManager, SteamClient, clearInterval, clearTimeout, setInterval, setTimeout
- Backend env/decky: -
- py_modules shipped: lsfg_vk
- dev/install scripts touching loader: plugin_loader service [scripts/deploy-to-deck.sh]; ~/homebrew path [scripts/deploy-to-deck.sh]

</details>
<details><summary><b>decky-networkinfo</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, Field, Focusable, PanelSection, PanelSectionRow, Spinner, definePlugin, staticClasses
- @decky/ui type-only imports: ServerAPI
- SteamClient: -
- Globals/stores members: -
- Backend env/decky: -
- dev/install scripts touching loader: plugin_loader service [Makefile]; ~/homebrew path [Makefile]

</details>
<details><summary><b>decky-recorder</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, Dropdown, PanelSection, PanelSectionRow, Router, SliderField, ToggleField, definePlugin, staticClasses
- @decky/ui type-only imports: DropdownOption, ServerAPI, SingleDropdownOption
- SteamClient: Input.RegisterForControllerStateChanges
- Globals/stores members: -
- `window.*` read: SteamClient
- Backend env/decky: DECKY_HOME, DECKY_PLUGIN_DIR, DECKY_PLUGIN_LOG_DIR, DECKY_PLUGIN_SETTINGS_DIR, HOME, logger
- internals grep hits: ~/homebrew path [py:main.py]
- dev/install scripts touching loader: plugin_loader service [Makefile]; ~/homebrew path [Makefile]

</details>
<details><summary><b>decky-steamgriddb</b></summary>

- @decky/ui / DFL (runtime names): ConfirmModal, ControlsList, DialogBody, DialogBodyText, DialogButton, DialogButtonPrimary, DialogCheckbox, DialogControlsSection, DialogControlsSectionHeader, DialogFooter, DialogHeader, Dropdown, Field, Focusable, IconsModule, Marquee, Menu, MenuItem, ModalRoot, Navigation, PanelSection, PanelSectionRow, ProgressBar, ReorderableList, SliderField, Spinner, Tabs, TextField, ToggleField, afterPatch, appDetailsHeaderClasses, callOriginal, createModuleMapping, definePlugin, fakeRenderComponent, findClassModule, findInReactTree, findInTree, findModule, findModuleByExport, findModuleExport, findSP, joinClassNames, quickAccessMenuClasses, replacePatch, showContextMenu, showModal, useParams, wrapReactType
- @decky/ui type-only imports: AppDetails, DialogCheckboxProps, Export, FocusableProps, FooterLegendProps, GamepadEvent, Module, Patch, ReorderableEntry, SliderFieldProps, TabsProps, TextFieldProps
- deep imports: @decky/ui/dist/globals/steam-client/App
- SteamClient: Apps.ClearCustomArtworkForApp, Apps.RegisterForAppDetails, Apps.SetCustomArtworkForApp, Apps.SetShortcutName, User.StartRestart
- Globals/stores members: App.m_CurrentUser, LocalizationManager.m_mapFallbackTokens, LocalizationManager.m_mapTokens, LocalizationManager.m_rgLocalesToUse, appDetailsStore.GetCustomLogoPosition, appDetailsStore.SaveCustomLogoPosition, appStore.GetAppOverviewByAppID, appStore.GetIconURLForApp
- `window.*` read: App, LocalizationManager, appDetailsStore, appStore, console, document, getComputedStyle, location
- Backend env/decky: DECKY_HOME, DECKY_PLUGIN_DIR, DECKY_PLUGIN_SETTINGS_DIR, DECKY_USER_HOME, logger, migrate_settings

</details>
<details><summary><b>decky-storage-cleaner</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, ConfirmModal, DialogCheckbox, PanelSection, PanelSectionRow, definePlugin, showModal, staticClasses
- SteamClient: Apps.RegisterForAppDetails
- Globals/stores members: -
- Backend env/decky: DECKY_PLUGIN_DIR, DECKY_USER_HOME

</details>
<details><summary><b>decky-terminal</b></summary>

- @decky/ui / DFL (runtime names): DialogButton, Dropdown, Field, Focusable, GamepadButton, PanelSection, Router, SidebarNavigation, SteamSpinner, TextField, ToggleField, definePlugin, staticClasses, useParams
- @decky/ui type-only imports: GamepadEvent
- SteamClient: -
- Globals/stores members: -
- `window.*` read: location
- Backend env/decky: logger
- py_modules shipped: decky_terminal

</details>
<details><summary><b>hltb-for-deck</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, DialogButtonPrimary, DropdownItem, MenuItem, Navigation, PanelSection, PanelSectionRow, SliderField, SteamSpinner, ToggleField, afterPatch, appDetailsClasses, appDetailsHeaderClasses, createReactTreePatcher, definePlugin, fakeRenderComponent, findInReactTree, findInTree, findModuleChild, removeHookStubs, staticClasses
- @decky/ui type-only imports: Patch
- SteamClient: -
- Globals/stores members: LocalizationManager.m_rgLocalesToUse
- `window.*` read: LocalizationManager
- Backend env/decky: -
- dev/install scripts touching loader: plugin_loader service [Makefile]; ~/homebrew path [Makefile]

</details>
<details><summary><b>memory-deck</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, DialogButton, DropdownItem, Focusable, PanelSection, PanelSectionRow, Router, SteamSpinner, definePlugin, gamepadDialogClasses, joinClassNames, staticClasses
- @decky/ui type-only imports: ServerAPI
- SteamClient: -
- Globals/stores members: -
- Backend env/decky: -

</details>
<details><summary><b>moondeck</b></summary>

- @decky/ui / DFL (runtime names): Button, ButtonItem, ConfirmModal, DialogBody, DialogButton, DialogButtonPrimary, DialogButtonSecondary, DialogControlsSection, DialogControlsSectionHeader, DialogHeader, Dropdown, Field, Focusable, Menu, MenuItem, ModalRoot, Navigation, PanelSection, PanelSectionRow, SidebarNavigation, Spinner, TextField, ToggleField, afterPatch, appDetailsClasses, appDetailsHeaderClasses, basicAppDetailsSectionStylerClasses, createReactTreePatcher, findInReactTree, findModuleExport, findSP, joinClassNames, playSectionClasses, showContextMenu, showModal, sleep, staticClasses
- @decky/ui type-only imports: AppDetails, AppLifetimeNotification, DropdownOption, EThirdPartyControllerConfiguration, NavEntryPositionPreferences, Unregisterable
- deep imports: @decky/ui/dist/globals/steam-client/App, @decky/ui/dist/globals/steam-client/GameSessions, @decky/ui/dist/globals/steam-client/Input
- SteamClient: Apps.AddShortcut, Apps.CancelGameAction, Apps.GetResolutionOverrideForApp, Apps.RegisterForAppDetails, Apps.RegisterForGameActionEnd, Apps.RegisterForGameActionStart, Apps.RegisterForGameActionTaskChange, Apps.RemoveShortcut, Apps.RunGame, Apps.SetAppLaunchOptions, Apps.SetAppResolutionOverride, Apps.SetShortcutName, Apps.SetThirdPartyControllerConfiguration, Apps.TerminateApp, Apps.ToggleOverrideResolutionForInternalDisplay, GameSessions.RegisterForAppLifetimeNotifications, System.Audio, System.DisplayManager, User.RegisterForLoginStateChange, User.StartRestart
- Globals/stores members: App.WaitForServicesInitialized, App.m_CurrentUser, collectionStore.BIsHidden, collectionStore.GetCollection, collectionStore.GetCollectionForAppType, collectionStore.GetCollectionIDByUserTag, collectionStore.NewUnsavedCollection, collectionStore.SetAppsAsHidden, collectionStore.userCollections
- `window.*` read: App
- Backend env/decky: -

</details>
<details><summary><b>protondb-decky</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, ConfirmModal, DialogButton, DropdownItem, Focusable, Navigation, PanelSection, PanelSectionRow, ScrollPanel, ScrollPanelGroup, ToggleField, afterPatch, appDetailsClasses, appDetailsHeaderClasses, createReactTreePatcher, definePlugin, findInReactTree, findModuleExport, showModal, staticClasses
- @decky/ui type-only imports: ButtonItemProps, DialogButtonProps, PanelSectionProps
- SteamClient: Apps
- Globals/stores members: App.WaitForServicesInitialized, LocalizationManager.m_rgLocalesToUse, SteamUIStore.WindowStore, appStore.GetAppOverviewByAppID, appStore.GetAppOverviewByGameID, appStore.GetInstalledApps, appStore.m_mapApps, webpackChunksteamui.push
- `window.*` read: App, LocalizationManager, SteamClient, appStore, open, webpackChunksteamui
- Backend env/decky: DECKY_PLUGIN_SETTINGS_DIR, DECKY_PLUGIN_VERSION, DECKY_VERSION
- internals grep hits: CEF debugger 8080 [fe:src/patches/StorePatch.ts]

</details>
<details><summary><b>reshadeck</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, DialogButton, Dropdown, Menu, MenuItem, PanelSection, PanelSectionRow, Router, ToggleField, definePlugin, showContextMenu, staticClasses
- @decky/ui type-only imports: DropdownOption, ServerAPI, SingleDropdownOption
- SteamClient: Input.RegisterForControllerStateChanges, System.RegisterForOnResumeFromSuspend, System.RegisterForOnSuspendRequest
- Globals/stores members: -
- `window.*` read: SteamClient
- Backend env/decky: DECKY_PLUGIN_DIR, DECKY_USER_HOME, logger
- dev/install scripts touching loader: plugin_loader service [Makefile]; ~/homebrew path [Makefile]

</details>
<details><summary><b>vibrantDeck</b></summary>

- @decky/ui / DFL (runtime names): PanelSection, PanelSectionRow, Router, SliderField, ToggleField, definePlugin, staticClasses
- @decky/ui type-only imports: ServerAPI
- SteamClient: -
- Globals/stores members: -
- Backend env/decky: -

</details>
<details><summary><b>volume-boost</b></summary>

- @decky/ui / DFL (runtime names): PanelSection, PanelSectionRow, SliderField, ToggleField, definePlugin, staticClasses
- @decky/ui type-only imports: ServerAPI
- SteamClient: -
- Globals/stores members: -
- Backend env/decky: logger

</details>
<details><summary><b>kettle/crash</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, ConfirmModal, DialogButton, Field, Focusable, ModalRoot, Navigation, PanelSection, PanelSectionRow, showModal, staticClasses, useParams
- SteamClient: -
- Globals/stores members: -
- Backend env/decky: DECKY_PLUGIN_SETTINGS_DIR, DECKY_USER_HOME, emit, logger
- py_modules shipped: inotify
- Kettle shared files used: engines.ts, inotify.py

</details>
<details><summary><b>kettle/device-settings</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, ConfirmModal, DialogButton, DropdownItem, Field, Focusable, Navigation, PanelSection, PanelSectionRow, ProgressBarWithInfo, Router, SidebarNavigation, SliderField, ToggleField, findModuleExport, showModal, staticClasses
- SteamClient: GameSessions.RegisterForAppLifetimeNotifications
- Globals/stores members: appStore.GetAppOverviewByAppID
- `window.*` read: appStore
- Backend env/decky: DECKY_PLUGIN_SETTINGS_DIR, DECKY_USER_HOME, emit, logger
- py_modules shipped: buttons, gyro, inotify, lights, powerd, refresh, screens, system
- Kettle shared files used: GamePicker.tsx, Tabs.tsx, powerd.py, refresh.py, inotify.py

</details>
<details><summary><b>kettle/game-settings</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, ConfirmModal, DialogButton, DropdownItem, Field, Focusable, ModalRoot, Navigation, PanelSection, PanelSectionRow, ProgressBarWithInfo, Router, SliderField, TextField, ToggleField, findModuleChild, showModal, staticClasses, useParams
- SteamClient: Apps.GetAvailableCompatTools, Apps.RegisterForAppDetails, Apps.SetAppLaunchOptions, Apps.SetAppResolutionOverride, Apps.SpecifyCompatTool, Apps.ToggleOverrideResolutionForInternalDisplay, GameSessions.RegisterForAppLifetimeNotifications, User.StartRestart
- Globals/stores members: appDetailsStore.GetAppDetails, appStore.GetAppOverviewByAppID
- `window.*` read: appStore
- Backend env/decky: DECKY_PLUGIN_DIR, DECKY_PLUGIN_SETTINGS_DIR, DECKY_USER_HOME, logger
- py_modules shipped: configfix, extras, framegen, gameengine, lumen, powerd, refresh, steamlib, upscaling
- Kettle shared files used: GamePicker.tsx, engines.ts, Tabs.tsx, gameFixes.ts, launchOptions.ts, gameengine.py, powerd.py, refresh.py, steamlib.py

</details>
<details><summary><b>kettle/stores</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, ConfirmModal, DialogButton, Dropdown, DropdownItem, Field, Focusable, Navigation, PanelSection, PanelSectionRow, ProgressBarWithInfo, SidebarNavigation, TextField, ToggleField, showModal, staticClasses, useParams
- SteamClient: Apps, Apps.AddShortcut, Apps.GetAvailableCompatTools, Apps.RemoveShortcut, Apps.RunGame, Apps.SetAppLaunchOptions, Apps.SetShortcutExe, Apps.SetShortcutIcon, Apps.SetShortcutLaunchOptions, Apps.SetShortcutName, Apps.SetShortcutStartDir, Apps.SpecifyCompatTool, URL
- Globals/stores members: appStore.GetAppOverviewByAppID
- `window.*` read: ControllerStore, controllerConfiguratorStore
- Backend env/decky: DECKY_PLUGIN_DIR, DECKY_PLUGIN_SETTINGS_DIR, DECKY_SETTINGS, DECKY_USER_HOME, emit, logger
- py_modules shipped: cdp, steamlib
- Kettle shared files used: steamlib.py

</details>
<details><summary><b>kettle/welcome</b></summary>

- @decky/ui / DFL (runtime names): ButtonItem, Focusable, Navigation, PanelSection, PanelSectionRow, QuickAccessTab, SidebarNavigation, findModuleChild, staticClasses
- SteamClient: Apps, Apps.AddShortcut, Apps.RegisterForAppDetails, Apps.RunGame, Apps.SetAppLaunchOptions, Apps.SetShortcutExe, Apps.SetShortcutIcon, Apps.SetShortcutName, System.SwitchToDesktop, URL, User.RegisterForLoginStateChange
- Globals/stores members: DeckyPluginLoader.deckyState, appDetailsStore.GetAppDetails, appStore.GetAppOverviewByAppID
- `window.*` read: ControllerStore, DeckyPluginLoader, controllerConfiguratorStore, settingsStore
- Backend env/decky: DECKY_PLUGIN_SETTINGS_DIR, DECKY_USER_HOME, logger
- py_modules shipped: steamlib
- Kettle shared files used: gameFixes.ts, launchOptions.ts

</details>

## 2. Aggregate ranking

### 2.1 Frontend loader API (v1 @decky/api and v2 serverAPI merged)

`call`, `callable` and serverAPI.`callPluginMethod` are merged (same backend RPC). `definePlugin` from @decky/api is an identity wrapper, as is DFL's. Not used by any surveyed plugin: `getExternalResourceURL`, `injectCssIntoTab`, `removeCssFromTab`, `openFilePickerV2`, `callServerMethod` (the legacy utility RPC).

| # plugins | API |
|---:|---|
| 41 | `call / callable / serverAPI.callPluginMethod` |
| 28 | `toaster.toast` |
| 28 | `routerHook.addRoute` |
| 25 | `routerHook.removeRoute` |
| 14 | `definePlugin` |
| 9 | `openFilePicker` |
| 9 | `routerHook.addPatch` |
| 9 | `routerHook.removePatch` |
| 8 | `fetchNoCors` |
| 5 | `addEventListener` |
| 5 | `removeEventListener` |
| 2 | `routerHook.addGlobalComponent` |
| 2 | `routerHook.removeGlobalComponent` |
| 2 | `useQuickAccessVisible` |
| 1 | `routerHook.routerState` |
| 1 | `window.MicroSDeck (cross-plugin)` |
| 1 | `window.DeckyPluginLoader.routerHook` |
| 1 | `window.DeckyPluginLoader.pluginReloadQueue` |
| 1 | `executeInTab` |
| 1 | `webpackChunksteamui.push` |
| 1 | `window.DeckyPluginLoader.deckyState` |
| 1 | `window.DeckyPluginLoader.deckyState.setActivePlugin` |
| 1 | `window.DeckyPluginLoader` |

### 2.2 Legacy v2 serverAPI members (19 legacy plugins)

| # plugins | API |
|---:|---|
| 17 | `callPluginMethod` |
| 12 | `routerHook.addRoute` |
| 9 | `routerHook.removeRoute` |
| 9 | `toaster.toast` |
| 3 | `fetchNoCors` |
| 3 | `openFilePicker` |
| 1 | `executeInTab` |

### 2.3 Backend (Python) surface

`settings` = loader's `SettingsManager(name, settings_directory)` with `read/commit/getSetting/setSetting/.settings`. `helpers`: `get_ssl_context`, `get_user`, `get_user_id`. `injector`: `get_tab`, `get_gamepadui_tab`, `Tab`.

| # plugins | API |
|---:|---|
| 43 | `main.py Plugin class` |
| 36 | `hook _main` |
| 31 | `hook _unload` |
| 25 | `decky.logger` |
| 22 | `decky.DECKY_PLUGIN_SETTINGS_DIR (or env var)` |
| 18 | `decky.DECKY_USER_HOME (or env var)` |
| 18 | `import decky` |
| 16 | `decky.DECKY_PLUGIN_DIR (or env var)` |
| 14 | `import decky_plugin` |
| 13 | `hook _migration` |
| 12 | `loader module settings` |
| 11 | `decky.DECKY_HOME (or env var)` |
| 8 | `loader-bundled pkg aiohttp` |
| 7 | `decky.migrate_settings` |
| 7 | `decky.DECKY_PLUGIN_LOG_DIR (or env var)` |
| 6 | `decky.DECKY_PLUGIN_RUNTIME_DIR (or env var)` |
| 5 | `hook _uninstall` |
| 5 | `decky.migrate_logs` |
| 5 | `decky.emit` |
| 4 | `decky.migrate_runtime` |
| 4 | `decky.DECKY_USER (or env var)` |
| 3 | `decky.DECKY_PLUGIN_VERSION (or env var)` |
| 3 | `loader module helpers` |
| 2 | `decky.HOME (or env var)` |
| 2 | `loader-bundled pkg certifi` |
| 1 | `decky.DECKY_PLUGIN_LOG (or env var)` |
| 1 | `loader module injector` |
| 1 | `decky.DECKY_PLUGIN_NAME (or env var)` |
| 1 | `decky.migrate_any` |
| 1 | `loader-bundled pkg watchdog` |
| 1 | `decky.DECKY_VERSION (or env var)` |

### 2.4 plugin.json flags

| # plugins | API |
|---:|---|
| 7 | `root` |
| 7 | `debug` |
| 1 | `global-dfl` |
| 1 | `_root` |

### 2.5 Styles

- Frontend load: ESM v1: 25, legacy IIFE eval: 20
- API style: v1 @decky/api: 26, v2 serverAPI: 19
- Backend api_version: 0: 20, 1: 24, 2: 1
- No Python backend (passive): deckfaqs, hltb-for-deck
- Native backends / Docker builds: ControllerTools (backend/Cargo.toml), Decky-Undervolt (Dockerfile only), Emuchievements (backend/CMakeLists.txt), HueSync (Dockerfile only), MagicPodsDecky (Dockerfile only), MicroSDeck (backend/Cargo.toml), PowerTools (backend/Cargo.toml, backend/community_settings_core/Cargo.toml, backend/community_settings_srv/Cargo.toml, backend/limits_core/Cargo.toml, backend/limits_srv/Cargo.toml), SDH-GameThemeMusic (Dockerfile only), SimpleDeckyTDP (Dockerfile only), decky-cloud-save (Dockerfile only), decky-recorder (Dockerfile only), memory-deck (Dockerfile only)
- `remote_binary` downloads in package.json (fetched by the Decky CLI at package time into bin/): Decky-Framegen, SDH-GameThemeMusic, decky-lsfg-vk

### 2.6 @decky/ui / DFL runtime names

Type-only imports excluded. Webpack/patch internals (rank in brackets) are the fragile part: `afterPatch` 13, `findModuleChild` 13, `findInReactTree` 8, `findSP` 6, `replacePatch` 5, `findInTree` 5, `createReactTreePatcher` 5, `findModuleExport` 5, `findClassModule` 4, `findModule` 3, `fakeRenderComponent` 3, `beforePatch` 2, `callOriginal` 2, `getGamepadNavigationTrees` 2, `findModuleByExport` 2, `wrapReactType` 2, `getFocusNavController` 1, `getReactRoot` 1, `findModuleDetailsByExport` 1, `classMap` 1, `ReactRouter` 1, `findClass` 1, `IconsModule` 1, `createModuleMapping` 1, `removeHookStubs` 1.

| # plugins | API |
|---:|---|
| 45 | `PanelSection` |
| 43 | `PanelSectionRow` |
| 40 | `staticClasses` |
| 38 | `ButtonItem` |
| 36 | `ToggleField` |
| 32 | `Focusable` |
| 31 | `DialogButton` |
| 31 | `definePlugin` |
| 28 | `showModal` |
| 27 | `Navigation` |
| 26 | `Field` |
| 25 | `TextField` |
| 22 | `ConfirmModal` |
| 22 | `Router` |
| 20 | `ModalRoot` |
| 20 | `SliderField` |
| 18 | `Dropdown` |
| 16 | `DropdownItem` |
| 14 | `SidebarNavigation` |
| 13 | `afterPatch` |
| 13 | `findModuleChild` |
| 12 | `gamepadDialogClasses` |
| 12 | `MenuItem` |
| 11 | `Menu` |
| 11 | `Tabs` |
| 11 | `showContextMenu` |
| 10 | `Spinner` |
| 9 | `SteamSpinner` |
| 8 | `findInReactTree` |
| 8 | `sleep` |
| 8 | `useParams` |
| 7 | `joinClassNames` |
| 7 | `DialogBody` |
| 7 | `ScrollPanelGroup` |
| 6 | `GamepadButton` |
| 6 | `ProgressBarWithInfo` |
| 6 | `findSP` |
| 5 | `Marquee` |
| 5 | `quickAccessControlsClasses` |
| 5 | `replacePatch` |
| 5 | `DialogHeader` |
| 5 | `findInTree` |
| 5 | `DialogCheckbox` |
| 5 | `appDetailsClasses` |
| 5 | `createReactTreePatcher` |
| 5 | `findModuleExport` |
| 4 | `ModalPosition` |
| 4 | `findClassModule` |
| 4 | `gamepadSliderClasses` |
| 4 | `DialogControlsSection` |
| 4 | `SidebarNavigationPage` |
| 4 | `quickAccessMenuClasses` |
| 4 | `ReorderableList` |
| 4 | `appDetailsHeaderClasses` |
| 3 | `DialogBodyText` |
| 3 | `MenuGroup` |
| 3 | `gamepadTabbedPageClasses` |
| 3 | `scrollPanelClasses` |
| 3 | `findModule` |
| 3 | `QuickAccessTab` |
| 3 | `fakeRenderComponent` |
| 3 | `DialogButtonPrimary` |
| 2 | `DialogButtonSecondary` |
| 2 | `SimpleModal` |
| 2 | `Tab` |
| 2 | `gamepadContextMenuClasses` |
| 2 | `DialogFooter` |
| 2 | `beforePatch` |
| 2 | `callOriginal` |
| 2 | `Button` |

Also used (fewer plugins): `ProgressBar` (2), `getGamepadNavigationTrees` (2), `Toggle` (2), `findModuleByExport` (2), `wrapReactType` (2), `ScrollPanel` (2), `DialogControlsSectionHeader` (2), `DialogSubHeader` (1), `getFocusNavController` (1), `getReactRoot` (1), `ProgressBarItem` (1), `DialogLabel` (1), `findModuleDetailsByExport` (1), `ColorPickerModal` (1), `Panel` (1), `classMap` (1), `ReactRouter` (1), `findClass` (1), `achievementClasses` (1), `mainMenuAppRunningClasses` (1), `ControlsList` (1), `IconsModule` (1), `createModuleMapping` (1), `removeHookStubs` (1), `basicAppDetailsSectionStylerClasses` (1), `playSectionClasses` (1)

### 2.7 SteamClient calls made directly by plugin code

These go straight to Steam, not through the loader, but they define which Steam client builds the replacement must tolerate. Namespaces touched: `Apps` 18, `GameSessions` 16, `User` 14, `System` 11, `Input` 6, `InstallFolder` 3, `Overlay` 2, `Settings` 2, `Storage` 2, `URL` 2, `Installs` 1, `Downloads` 1, `BrowserView` 1, `Screenshots` 1.

| # plugins | API |
|---:|---|
| 16 | `GameSessions.RegisterForAppLifetimeNotifications` |
| 10 | `Apps.RegisterForAppDetails` |
| 8 | `Apps.SetAppLaunchOptions` |
| 7 | `System.RegisterForOnResumeFromSuspend` |
| 7 | `Apps.SetShortcutName` |
| 6 | `Apps.RunGame` |
| 6 | `User.RegisterForLoginStateChange` |
| 6 | `Apps.AddShortcut` |
| 6 | `User.StartRestart` |
| 5 | `User.RegisterForPrepareForSystemSuspendProgress` |
| 5 | `Apps.SetShortcutExe` |
| 5 | `Apps.RegisterForGameActionStart` |
| 4 | `User.RegisterForResumeSuspendedGamesProgress` |
| 4 | `System.RegisterForOnSuspendRequest` |
| 4 | `Apps.RemoveShortcut` |
| 4 | `Apps.SetShortcutStartDir` |
| 4 | `Apps.SpecifyCompatTool` |
| 4 | `Apps.TerminateApp` |
| 3 | `Apps.GetAvailableCompatTools` |
| 3 | `Apps.SetCustomArtworkForApp` |
| 3 | `Input.RegisterForControllerStateChanges` |
| 3 | `Apps.SetShortcutIcon` |
| 3 | `Apps.RegisterForGameActionEnd` |
| 3 | `InstallFolder.GetInstallFolders` |
| 2 | `Settings.GetCurrentLanguage` |
| 2 | `System.GetSystemInfo` |
| 2 | `System.RegisterForBatteryStateChanges` |
| 2 | `Apps.SetShortcutLaunchOptions` |
| 2 | `Input.RegisterForControllerInputMessages` |
| 2 | `System.Audio` |
| 2 | `Storage.GetJSON` |
| 2 | `Storage.SetObject` |
| 2 | `Apps.SetAppResolutionOverride` |
| 2 | `Apps.ToggleOverrideResolutionForInternalDisplay` |
| 1 | `Input.ControllerKeyboardSetKeyState` |
| 1 | `Input.SetMousePosition` |
| 1 | `Overlay.RegisterForOverlayActivated` |
| 1 | `Installs.OpenInstallWizard` |
| 1 | `Apps.CreateDesktopShortcutForApp` |
| 1 | `Apps.SetShortcutSortAs` |
| 1 | `Overlay.SetOverlayState` |
| 1 | `User.GetLoginUsers` |
| 1 | `User.RegisterForCurrentUserChanges` |
| 1 | `Apps.GetStoreTagLocalization` |
| 1 | `System.RestartPC` |
| 1 | `Downloads.RegisterForDownloadItems` |
| 1 | `Downloads.RegisterForDownloadOverview` |
| 1 | `GameSessions.RegisterForAchievementNotification` |
| 1 | `GameSessions.RegisterForScreenshotNotification` |
| 1 | `User.RegisterForShutdownStart` |
| 1 | `BrowserView.Destroy` |
| 1 | `Screenshots.GetAllAppsLocalScreenshots` |
| 1 | `Screenshots.GetAllLocalScreenshots` |
| 1 | `Screenshots.GetLocalScreenshotPath` |
| 1 | `System.OpenInSystemBrowser` |
| 1 | `Apps.ClearCustomArtworkForApp` |
| 1 | `Apps.CancelGameAction` |
| 1 | `Apps.GetResolutionOverrideForApp` |
| 1 | `Apps.RegisterForGameActionTaskChange` |
| 1 | `Apps.SetThirdPartyControllerConfiguration` |

Also used (fewer plugins): `System.DisplayManager` (1), `System.SwitchToDesktop` (1)

### 2.8 Steam store globals / window globals used directly

| # plugins | API |
|---:|---|
| 18 | `appStore` |
| 9 | `LocalizationManager` |
| 9 | `collectionStore` |
| 7 | `App` |
| 5 | `appDetailsStore` |
| 3 | `settingsStore` |
| 3 | `SteamUIStore` |
| 2 | `appAchievementProgressCache` |
| 2 | `appDetailsCache` |
| 2 | `SP_REACT` |
| 2 | `appInfoStore` |
| 2 | `DeckyPluginLoader` |
| 2 | `loginStore` |
| 1 | `securitystore` |
| 1 | `friendStore` |
| 1 | `webpackChunksteamui` |

### 2.8a Store members (top)

| # plugins | API |
|---:|---|
| 12 | `appStore.GetAppOverviewByAppID` |
| 8 | `appStore.GetAppOverviewByGameID` |
| 8 | `LocalizationManager.m_rgLocalesToUse` |
| 5 | `App.WaitForServicesInitialized` |
| 5 | `collectionStore.deckDesktopApps` |
| 4 | `App.m_CurrentUser` |
| 3 | `LocalizationManager.m_mapFallbackTokens` |
| 3 | `LocalizationManager.m_mapTokens` |
| 3 | `appStore.allApps` |
| 3 | `collectionStore.BIsHidden` |
| 3 | `collectionStore.SetAppsAsHidden` |
| 3 | `appDetailsStore.GetAppDetails` |
| 3 | `appStore.m_mapApps` |
| 3 | `collectionStore.GetCollection` |
| 3 | `collectionStore.NewUnsavedCollection` |
| 3 | `collectionStore.userCollections` |
| 2 | `settingsStore.m_ClientSettings` |
| 2 | `appStore.GetVerticalCapsuleURLForApp` |
| 2 | `appAchievementProgressCache.m_achievementProgress` |
| 2 | `SteamUIStore.WindowStore` |
| 2 | `SP_REACT.createElement` |
| 2 | `appInfoStore.OnAppOverviewChange` |
| 2 | `collectionStore.GetCollectionIDByUserTag` |
| 2 | `loginStore.m_strAccountName` |
| 1 | `appStore.GetCustomVerticalCapsuleURLs` |
| 1 | `appAchievementProgressCache.RequestCacheUpdate` |
| 1 | `appAchievementProgressCache.SaveCacheFile` |
| 1 | `appDetailsCache.SetCachedDataForApp` |
| 1 | `appDetailsStore.GetAchievements` |
| 1 | `appDetailsStore.GetAppData` |
| 1 | `appDetailsCache.FetchDataForApp` |
| 1 | `appDetailsStore.RequestAppDetails` |
| 1 | `settingsStore.settings` |
| 1 | `appInfoStore._originalOnAppOverviewChange` |
| 1 | `collectionStore.DeleteCollection` |
| 1 | `collectionStore.collectionsFromStorage` |
| 1 | `SteamUIStore.RunningApps` |
| 1 | `appInfoStore.OriginalOnAppOverviewChange` |
| 1 | `appStore.GetCustomImageURLs` |
| 1 | `DeckyPluginLoader.pluginReloadQueue` |

Also used (fewer plugins): `DeckyPluginLoader.routerHook` (1), `LocalizationManager.LocalizeString` (1), `appAchievementProgressCache.GetAchievementProgress` (1), `appStore.m_mapStoreTagLocalization` (1), `collectionStore.GetUserCollectionsByName` (1), `collectionStore.appTypeCollectionMap` (1), `collectionStore.sharedLibrariesCollections` (1), `friendStore.allFriends` (1), `securitystore.IsLockScreenActive` (1), `collectionStore.allAppsCollection` (1), `collectionStore.localGamesCollection` (1), `collectionStore.allGamesCollection` (1), `appDetailsStore.GetCustomLogoPosition` (1), `appDetailsStore.SaveCustomLogoPosition` (1), `appStore.GetIconURLForApp` (1), `collectionStore.GetCollectionForAppType` (1), `appStore.GetInstalledApps` (1), `webpackChunksteamui.push` (1), `DeckyPluginLoader.deckyState` (1)

### 2.9 Python packages imported that are neither stdlib, loader-provided nor in the plugin's own py_modules

Expected to arrive in the release zip's py_modules (built by the plugin's CI / Docker), from the system (`gi` = PyGObject) or to be optional. A replacement that runs backends on a different interpreter breaks `gi` users if PyGObject is missing.

| # plugins | API |
|---:|---|
| 3 | `gi` |
| 1 | `jeepney` |
| 1 | `desktopC` |
| 1 | `desktopTM` |
| 1 | `requests` |
| 1 | `PIL` |
| 1 | `pystray` |
| 1 | `webSocketClient` |
| 1 | `psutil` |
| 1 | `vdf` |
| 1 | `consts` |
| 1 | `pyglet` |
| 1 | `wakeonlan` |
| 1 | `zeroconf` |
| 1 | `click` |

### 2.10 DECKY_* environment variables read (as os.environ or decky.X)

| # plugins | API |
|---:|---|
| 22 | `DECKY_PLUGIN_SETTINGS_DIR` |
| 18 | `DECKY_USER_HOME` |
| 16 | `DECKY_PLUGIN_DIR` |
| 11 | `DECKY_HOME` |
| 7 | `DECKY_PLUGIN_LOG_DIR` |
| 6 | `DECKY_PLUGIN_RUNTIME_DIR` |
| 4 | `DECKY_USER` |
| 3 | `DECKY_PLUGIN_VERSION` |
| 1 | `DECKY_PLUGIN_LOG` |
| 1 | `DECKY_PLUGIN_NAME` |
| 1 | `DECKY_VERSION` |
| 1 | `DECKY_SETTINGS` |

## 3. Compatibility risks: plugins that touch loader internals or undocumented behaviour

Ordered from hardest to keep compatible to easiest.

| Risk | Plugins | What the replacement must do |
|---|---|---|
| Private frontend loader object `window.DeckyPluginLoader` | TabMaster (`pluginReloadQueue`, `routerHook.routerState._routePatches`), kettle/welcome (`deckyState.setActivePlugin`) | Expose a `window.DeckyPluginLoader` with at least `pluginReloadQueue: {name,version}[]`, `routerHook` (same object as the API one, with a `routerState._routePatches: Map<route, Set<patch>>`), and `deckyState.setActivePlugin(name)`. |
| Loader Python modules re-exported into plugin processes | 12 plugins `from settings import SettingsManager`; 3 import `helpers` (steamgriddb get_ssl_context, TunnelDeck get_user, autoflatpaks get_user_id); DeckWebBrowser imports `injector` (get_tab, get_gamepadui_tab, Tab). protondb-decky and NonSteamLaunchers ship their own settings.py but still get the loader's, because the alias is installed before main.py runs. | Ship stub modules `settings`, `helpers`, `injector` on the plugin sys.path with the same signatures and file formats (`<settings_dir>/<name>.json`). |
| Loader's bundled third-party Python packages | aiohttp: SDH-CssLoader, SDH-AudioLoader, SDH-AnimationChanger, SDH-GameThemeMusic, Junk-Store, NonSteamLaunchers, DeckWebBrowser; certifi: AnimationChanger, GameThemeMusic; watchdog: CssLoader; async_timeout: moondeck | Run backends on an interpreter that has aiohttp (3.x), certifi, watchdog, multidict, yarl, async_timeout importable. This is the largest silent dependency: CSS Loader and Audio Loader, two of the most installed plugins, fail without it. |
| CEF DevTools on 127.0.0.1:8080 | SDH-CssLoader (own websocket client, creates `.cef-enable-remote-debugging`), protondb-decky and deckfaqs (`fetchNoCors(localhost:8080/json)`), DeckWebBrowser (`injector` + devtools URL), kettle/stores (own CDP client) | Keep Steam's remote debugging enabled on 8080 (that is how the loader injects itself anyway) and do not firewall it from plugin processes. |
| Hard-coded loader HTTP origin 127.0.0.1:1337 | All asset imports (7 plugins + every rollup template), SDH-PlayTime `/plugins/PlayTime/data/assets`, Junk-Store passes `localhost:1337/plugins` to scripts | Serve `/plugins/{name}/dist/*`, `/plugins/{name}/assets/*` (dist/assets) and `/plugins/{name}/data/*` (~/homebrew/data/<dir>) on 127.0.0.1:1337 without auth for GET. |
| ~/homebrew directory layout | TunnelDeck (`~/homebrew/settings`), decky-recorder (`~/homebrew/data/decky-recorder`), CssLoader (`~/homebrew/themes`), AudioLoader (`~/homebrew/sounds`), AnimationChanger (`~/homebrew/animations` migration), kettle/stores (sibling `~/homebrew/settings/kettle-*`), many `migrate_*` calls from old paths | Keep `DECKY_HOME=~/homebrew` with `plugins/ settings/<dir> data/<dir> logs/<dir>` or symlink it. |
| Loader service name `plugin_loader.service` | SimpleDeckyTDP restarts it from its backend (OTA update); many install/dev scripts (not runtime) use it | Provide a `plugin_loader.service` unit or alias. |
| Webpack chunk injection and Steam internals via DFL | protondb-decky pushes into `webpackChunksteamui`; TabMaster reads `SP_REACT.__SECRET_INTERNALS_DO_NOT_USE...`; webpack finders in 22 plugins, patch helpers in 14 plugins | Ship the real @decky/ui (LGPL-2.1) as `window.DFL` rather than reimplementing it; these break with Steam updates regardless of the loader. |
| Legacy calling convention | 20 plugins have api_version 0 (`self` = class, kwargs) and 20 use the legacy eval loader | Implement both RPC paths (`loader/call_legacy_plugin_method` kwargs vs positional) and both frontend load paths. |
| Cross-plugin globals | MicroSDeck publishes `window.MicroSDeck`, TabMaster consumes it and waits for MicroSDeck in `DeckyPluginLoader.pluginReloadQueue` | All plugins must share one window/JS realm (no per-plugin sandboxing of `window`). |
| Ignored/odd flags | PowerTools `global-dfl`, AudioLoader `_root` | Ignore unknown flags silently. |
| Process environment / cwd | Decky-Undervolt runs `./bin/ryzenadj` with cwd=plugin dir (explicit), PowerTools/MicroSDeck compute an unused HOME_DIR from cwd; root plugins call `sudo` | Keep cwd = `~/homebrew/services` (systemd WorkingDirectory) and keep `sudo` usable for root-flag backends. |
| Own servers on fixed ports | NonSteamLaunchers :8675 ws, MagicPods :2020 ws, Junk-Store ws, bash-shortcuts ws, PowerTools usdpl, MicroSDeck HTTP | Nothing to emulate; just do not block localhost ports or apply a CSP to the SharedJSContext that forbids `ws://localhost:*`. |

## 4. Minimum API set

Greedy search over the loader-owned surface (frontend loader API, backend `decky` module, hooks, env vars, loader Python modules/packages, HTTP routes; @decky/ui and SteamClient excluded since they are a library and Steam itself). 41 of 45 plugins (>=90%) are fully covered by the 46 items below; the plugins left out and the extra items each one needs:

- TabMaster: `fe:routerHook.routerState`, `fe:window.DeckyPluginLoader.pluginReloadQueue`, `fe:window.DeckyPluginLoader.routerHook`, `fe:window.MicroSDeck (cross-plugin)`
- kettle/welcome: `fe:window.DeckyPluginLoader`, `fe:window.DeckyPluginLoader.deckyState`, `fe:window.DeckyPluginLoader.deckyState.setActivePlugin`
- Junk-Store: `be:decky.DECKY_PLUGIN_NAME (or env var)`, `http:/plugins/{name}/* (CONTENT_SERVER for scripts)`
- protondb-decky: `be:decky.DECKY_VERSION (or env var)`, `fe:webpackChunksteamui.push`

Items (with plugin counts):

| # plugins | item |
|---:|---|
| 43 | `be:main.py Plugin class` |
| 41 | `fe: call / callable / serverAPI.callPluginMethod` |
| 36 | `be:hook _main` |
| 31 | `be:hook _unload` |
| 28 | `fe:routerHook.addRoute` |
| 28 | `fe:toaster.toast` |
| 25 | `be:decky.logger` |
| 25 | `fe:routerHook.removeRoute` |
| 22 | `be:decky.DECKY_PLUGIN_SETTINGS_DIR (or env var)` |
| 18 | `be:decky.DECKY_USER_HOME (or env var)` |
| 18 | `be:import decky` |
| 16 | `be:decky.DECKY_PLUGIN_DIR (or env var)` |
| 14 | `be:import decky_plugin` |
| 14 | `fe:definePlugin` |
| 13 | `be:hook _migration` |
| 12 | `be:loader module settings` |
| 11 | `be:decky.DECKY_HOME (or env var)` |
| 9 | `fe:openFilePicker` |
| 9 | `fe:routerHook.addPatch` |
| 9 | `fe:routerHook.removePatch` |
| 8 | `be:loader-bundled pkg aiohttp` |
| 8 | `fe:fetchNoCors` |
| 7 | `be:decky.DECKY_PLUGIN_LOG_DIR (or env var)` |
| 7 | `be:decky.migrate_settings` |
| 7 | `http:/plugins/{name}/<asset> (bundled image import)` |
| 6 | `be:decky.DECKY_PLUGIN_RUNTIME_DIR (or env var)` |
| 5 | `be:decky.emit` |
| 5 | `be:decky.migrate_logs` |
| 5 | `be:hook _uninstall` |
| 5 | `fe:addEventListener` |
| 5 | `fe:removeEventListener` |
| 4 | `be:decky.DECKY_USER (or env var)` |
| 4 | `be:decky.migrate_runtime` |
| 3 | `be:decky.DECKY_PLUGIN_VERSION (or env var)` |
| 3 | `be:loader module helpers` |
| 2 | `be:decky.HOME (or env var)` |
| 2 | `be:loader-bundled pkg certifi` |
| 2 | `fe:routerHook.addGlobalComponent` |
| 2 | `fe:routerHook.removeGlobalComponent` |
| 2 | `fe:useQuickAccessVisible` |
| 1 | `be:decky.DECKY_PLUGIN_LOG (or env var)` |
| 1 | `be:decky.migrate_any` |
| 1 | `be:loader module injector` |
| 1 | `be:loader-bundled pkg watchdog` |
| 1 | `fe:executeInTab` |
| 1 | `http:/plugins/{name}/data/*` |

**Practical minimum, in priority order** (everything else is used by <=2 plugins):

1. Plugin discovery: `~/homebrew/plugins/*/plugin.json` + `package.json` (`version`, `type`), flags `root`/`debug`, both frontend load paths (ESM `dist/index.js`, legacy eval of `frontend_bundle` with a serverAPI argument).
2. Globals: `window.SP_REACT`, `SP_JSX`, `SP_REACTDOM`, `DFL` (= real @decky/ui 4.x), and the `__DECKY_SECRET_INTERNALS_..._deckyLoaderAPIInit.connect()` object.
3. Plugin RPC: `call`/`callable` (positional, api_version>=1) and `callPluginMethod(name, kwargs)` (legacy). 41 plugins.
4. QAM panel from `definePlugin` result (`name`, `content`, `icon`, `titleView`, `alwaysRender`, `onDismount`; legacy bundles also return `title`) and its visibility hook (`useQuickAccessVisible`, also exported by DFL).
5. `routerHook.addRoute/removeRoute` (28), `addPatch/removePatch` (9), `addGlobalComponent/removeGlobalComponent` (2).
6. `toaster.toast` (28).
7. `fetchNoCors` (8), `openFilePicker` (9), `addEventListener/removeEventListener` + backend `decky.emit` (5/5).
8. Backend: `decky`/`decky_plugin` module with `logger`, all `DECKY_*` constants + env vars, `migrate_settings/runtime/logs/any`, `emit`; lifecycle `_main`, `_unload`, `_migration`, `_uninstall`; `py_modules` on sys.path; root/user setuid.
9. Compatibility shims: `settings.SettingsManager`, `helpers.get_ssl_context/get_user/get_user_id`, `injector.get_tab/get_gamepadui_tab/Tab`; aiohttp + certifi + watchdog importable.
10. HTTP 127.0.0.1:1337 `/plugins/{name}/dist|assets|data/*`; CEF debugging left on 8080; `window.DeckyPluginLoader` stub (`pluginReloadQueue`, `routerHook.routerState`, `deckyState.setActivePlugin`).

Unused by every surveyed plugin (safe to defer): `getExternalResourceURL`, `injectCssIntoTab`, `removeCssFromTab`, `openFilePickerV2`, `callServerMethod` and its legacy utilities (`http_request`, `get_setting`, `set_setting`, `ping`...), `executeInTab` (deckfaqs only), `DECKY_PLUGIN_AUTHOR`.

## 5. Plugin list and selection

Store plugins cloned (default branch, depth 1): Bluetooth, ControllerTools, DeckWebBrowser, Decky-Framegen, Decky-Undervolt, DiscordStatus, Emuchievements, HueSync, Junk-Store, MagicPodsDecky, MicroSDeck, MusicControl, NonSteamLaunchers, PowerTools, SDH-AnimationChanger, SDH-AudioLoader, SDH-CssLoader, SDH-GameThemeMusic, SDH-PlayTime, TabMaster, TunnelDeck, bash-shortcuts, deckfaqs, decky-autoflatpaks, decky-cloud-save, decky-game-settings, decky-lsfg-vk, decky-networkinfo, decky-recorder, decky-steamgriddb, decky-storage-cleaner, decky-terminal, hltb-for-deck, memory-deck, moondeck, protondb-decky, reshadeck, vibrantDeck, volume-boost. 
Not in the store but included because it is common on non-Deck handhelds: SimpleDeckyTDP. Old v2 plugins included on purpose: vibrantDeck, volume-boost, decky-networkinfo, TunnelDeck, memory-deck, MusicControl, deckfaqs, reshadeck, decky-recorder, SDH-AudioLoader, decky-cloud-save, decky-autoflatpaks, bash-shortcuts, DeckWebBrowser, SDH-AnimationChanger. Note: the store builds from the commit pinned in decky-plugin-database; default branches can be newer.

Caveats: the scan is regex-based. It misses calls built dynamically (e.g. `SteamClient[ns][fn]`), counts a name once per plugin even if only referenced in dead code, and treats `api_version` absent as 0. Minified vendored JS was excluded. SteamClient counts include type-only references in a few plugins.
