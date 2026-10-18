# First on-device test

Remora has run its 176 backend tests and the frontend's Node smoke test, but never inside Steam.
This is the order to try it on a device in, from most basic to most fragile. `journalctl -u
plugin_loader -f` shows the loader; Steam's CEF console (`ssh -L 8080:localhost:8080 <device>`, then
`http://localhost:8080` in Chrome) shows the frontend, whose log lines start
with `Remora`.

## Kettle Linux

The Kettle package (`packages/remora` in the kettlelinux repo) replaces `decky-loader`, keeps
`/home/.kettle/decky` (plugins, `loader.json`) and the image's plugin links, and provides
`decky-loader`, so the image's plugin packages stay installed.

```
sudo pacman -U remora-0.1.0-1-aarch64.pkg.tar.zst     # answers yes to removing decky-loader
sudo systemctl restart plugin_loader
```

Going back: `sudo pacman -U decky-loader-<version>.pkg.tar.zst` and restart the unit. Remora
only adds `remora.*` keys to `loader.json`, which Decky ignores.

## Checklist

1. **Loader up**: the journal shows `listening on 127.0.0.1:1337`, `loaded N plugins`, and one
   `started <name>` per plugin with a backend. `remora/status` errors, if any, show here too.
2. **Injection**: `attaching to SharedJSContext`, then in the CEF console `Remora` boot lines.
   Steam's UI must not reload in a loop. If it does, the injector stops after 5 rapid crashes;
   note what the console said.
3. **Hooks**: Remora settings → Steam hooks. Every lookup that says `failed` or `degraded` is
   worth a screenshot. The ones guessed without a device (docs/FRONTEND.md): QAM tab, router
   root/layout, toast renderer, error boundary.
4. **QAM tab**: the plugin icon tab appears, lists the plugins in the old order, opens one, and
   B goes back.
5. **Kettle's plugins**: Device Settings (TDP, fan, controller), Game Settings on a game page
   (a route patch), Game Stores (its pages are `addRoute` routes; a login flow uses events),
   Crash Reports, and Welcome's tour (it calls `DeckyPluginLoader.deckyState.setActivePlugin`).
6. **Toasts**: anything that toasts (Game Stores' install finished, a plugin failing to load).
   Check both the popup and the entry in the notification tray.
7. **Store**: Remora store lists Decky's plugins (if it says the source failed, the message is
   the reason), install one non-root plugin, update it, uninstall it. A root plugin should ask
   first.
8. **Crash recovery**: `sudo kill -9 <pid of a plugin host>` (`pgrep -af remora.host`). The
   plugin should come back within a few seconds and its QAM page keep working.
9. **Reload**: restart the unit while in Game Mode. The frontend should reconnect once Steam's
   context restarts.
10. **Sleep**: suspend and resume with the QAM open on a plugin.

## Useful commands

```
remora call /usr/share/decky/plugins/<plugin> <method> [json args]   # one backend method, no Steam
pgrep -af remora.host                                                  # one host per plugin
cat ~/homebrew/logs/<plugin folder>/*.log                              # a plugin's own log
```
