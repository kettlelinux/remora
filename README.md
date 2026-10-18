# Remora

A plugin loader for Steam's Game Mode UI that runs existing Decky Loader plugins unchanged.

Goals over Decky:

- **Reliable:** a crashing plugin is restarted instead of left dead, calls time out instead of
  hanging, and a Steam update turns off only the feature it broke.
- **Secure:** plugins run as the user unless the user approves root, the loader never changes
  ownership of user paths, and installs are hash- and signature-checked and atomic.
- **Open store:** use the Decky store, add your own sources, or host a signed static index.
- **Nice to develop for:** hot reload, typed stubs, and a harness that runs plugins without Steam.

Status: design. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and the compatibility specs in
[docs/compat/](docs/compat/README.md).

## License

MIT, see [LICENSE](LICENSE). `@decky/ui`, which Remora loads unmodified for plugin compatibility,
keeps its own LGPL-2.1 license.

Remora is an independent project, not affiliated with Steam Deck Homebrew or Valve.
