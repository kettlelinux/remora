"""Plugin hosts as real processes, driven through PluginManager and PluginProcess
(compat/backend.md §3-§5)."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from remora.config import Config
from remora.plugins.manager import ROOT_MIGRATED, PluginManager
from remora.plugins.process import PluginCallError, PluginProcess, State
from remora.wsrouter import Discard, RouteError

from conftest import wait_for

MODERN = """
import asyncio, os, sys, decky

class Plugin:
    async def _main(self):
        self.main_ran = True
        decky.logger.info("main started")

    async def add(self, a, b):
        return a + b

    async def greet(self, who="world", punct="!"):
        return f"hello {who}{punct}"

    def sync_mul(self, a, b):
        return a * b

    async def boom(self):
        raise ValueError("kaboom")

    async def bad_return(self):
        return object()

    async def tuple_return(self):
        return (1, "two", {3: None})

    async def did_main(self):
        return getattr(self, "main_ran", False)

    async def shout(self, *args):
        await decky.emit("shouted", *args)
        return len(args)

    async def env(self):
        return {k: v for k, v in os.environ.items() if k.startswith(("DECKY_", "HOME", "USER"))}

    async def slow(self, seconds):
        await asyncio.sleep(seconds)
        return "done"

    async def die(self):
        os._exit(3)

    async def pid(self):
        return os.getpid()

    async def _secret(self):
        return "should not be reachable"

    async def _unload(self):
        open(os.path.join(decky.DECKY_PLUGIN_RUNTIME_DIR, "unloaded"), "w").close()

    async def _uninstall(self):
        open(os.path.join(decky.DECKY_PLUGIN_RUNTIME_DIR, "uninstalled"), "w").close()
"""

LEGACY = """
import decky_plugin
import helpers
import localplatform
from settings import SettingsManager

class Plugin:
    async def _main(self):
        self.settings = SettingsManager(name="legacy",
                                        settings_directory=decky_plugin.DECKY_PLUGIN_SETTINGS_DIR)
        self.counter = 0

    async def greet(self, who="world"):
        self.settings.setSetting("last", who)
        return f"hello {who} from {helpers.get_user()}"

    async def is_class(self):
        # api_version 0: self is the class itself
        return isinstance(self, type)

    async def bump(self, by=1):
        self.counter += by
        return self.counter

    async def fail(self):
        raise RuntimeError("legacy failure")

    async def compat(self):
        import decky
        return {
            "same_module": decky is decky_plugin,
            "user": helpers.get_user(),
            "homebrew": helpers.get_homebrew_path(),
            "user_home": helpers.get_home_path(),
            "version": helpers.get_loader_version(),
            "unpriv": localplatform.get_unprivileged_path(),
            "priv": localplatform.get_privileged_path(),
            "on_linux": localplatform.ON_LINUX,
            "settings_file": self.settings.path,
            "last": SettingsManager("legacy", decky_plugin.DECKY_PLUGIN_SETTINGS_DIR).getSetting("last"),
        }
"""


def need_watches(manager: PluginManager) -> None:
    """Live reload tests need inotify watches; a machine out of them (ENOSPC) skips them."""
    err = manager.watch_error
    if err is not None:
        pytest.skip(f"no live reload here: {err}")


async def call(manager: PluginManager, name: str, method: str, *args):
    return await manager.call(name, method, *args)


# -- calling conventions --------------------------------------------------------------------

async def test_modern_positional_args(make_plugin, started) -> None:
    make_plugin("modern", "Modern", MODERN)
    manager = await started()
    assert await call(manager, "Modern", "add", 2, 3) == 5
    assert await call(manager, "Modern", "greet", "kettle", "?") == "hello kettle?"
    assert await call(manager, "Modern", "tuple_return") == [1, "two", {"3": None}]


async def test_sync_method_works(make_plugin, started) -> None:
    make_plugin("modern", "Modern", MODERN)
    manager = await started()
    assert await call(manager, "Modern", "sync_mul", 4, 5) == 20


async def test_exception_rejects_with_name_and_message(make_plugin, started) -> None:
    make_plugin("modern", "Modern", MODERN)
    manager = await started()
    with pytest.raises(RouteError) as e:
        await call(manager, "Modern", "boom")
    assert e.value.name == "ValueError" and e.value.message == "kaboom"
    assert "kaboom" in (e.value.tb or "")
    with pytest.raises(RouteError) as e:
        await call(manager, "Modern", "missing_method")
    assert e.value.name == "AttributeError"


async def test_private_methods_are_refused(make_plugin, started) -> None:
    make_plugin("modern", "Modern", MODERN)
    manager = await started()
    for method in ("_secret", "_unload", "__init__"):
        with pytest.raises(RouteError) as e:
            await call(manager, "Modern", method)
        assert e.value.name == "PermissionError"
    # the host refuses them too, should something call it directly
    proc = manager.plugins["Modern"].proc
    assert proc is not None
    with pytest.raises(PluginCallError) as e2:
        await proc.call("_secret")
    assert e2.value.error["name"] == "PermissionError"


async def test_non_json_return_is_an_error(make_plugin, started) -> None:
    make_plugin("modern", "Modern", MODERN)
    manager = await started()
    with pytest.raises(RouteError) as e:
        await asyncio.wait_for(call(manager, "Modern", "bad_return"), 5)
    assert e.value.name == "TypeError" and "JSON" in e.value.message
    assert await call(manager, "Modern", "add", 1, 1) == 2  # the host is still fine


async def test_legacy_kwargs_and_class_as_self(make_plugin, started, cfg: Config) -> None:
    make_plugin("legacy", "Legacy", LEGACY, api_version=None, version=None)
    manager = await started()
    assert await manager.call_legacy("Legacy", "greet", {"who": "kettle"}) == {
        "success": True, "result": f"hello kettle from {cfg.user}"}
    assert await manager.call_legacy("Legacy", "is_class", {}) == {"success": True, "result": True}
    assert (await manager.call_legacy("Legacy", "bump", {"by": 2}))["result"] == 2
    assert (await manager.call_legacy("Legacy", "bump", None))["result"] == 3  # state on the class
    assert await manager.call_legacy("Legacy", "fail", {}) == {"success": False, "result": "legacy failure"}
    assert (await manager.call_legacy("Legacy", "_main", {}))["success"] is False
    assert (await manager.call_legacy("Nobody", "greet", {}))["success"] is False


async def test_wrong_convention_is_refused(make_plugin, started) -> None:
    make_plugin("legacy", "Legacy", LEGACY, api_version=0)
    make_plugin("modern", "Modern", MODERN)
    manager = await started()
    with pytest.raises(RouteError) as e:
        await call(manager, "Legacy", "greet", "x")
    assert "api_version 1 or newer" in e.value.message
    result = await manager.call_legacy("Modern", "add", {"a": 1, "b": 2})
    assert result["success"] is False and "Legacy methods" in result["result"]


async def test_compat_imports(make_plugin, started, cfg: Config) -> None:
    make_plugin("legacy", "Legacy", LEGACY, api_version=None)
    manager = await started()
    await manager.call_legacy("Legacy", "greet", {"who": "saved"})
    result = (await manager.call_legacy("Legacy", "compat", {}))["result"]
    assert result["same_module"] is True
    assert result["user"] == cfg.user
    assert result["homebrew"] == result["unpriv"] == str(cfg.homebrew)
    assert result["priv"] == str(cfg.privileged)
    assert result["user_home"] == str(cfg.user_home)
    assert result["version"] == cfg.decky_version
    assert result["on_linux"] is True
    assert result["settings_file"] == str(cfg.homebrew / "settings" / "legacy" / "legacy.json")
    assert result["last"] == "saved"
    stored = json.loads((cfg.homebrew / "settings" / "legacy" / "legacy.json").read_text())
    assert stored == {"last": "saved"}


async def test_environment(make_plugin, started, cfg: Config) -> None:
    path = make_plugin("modern-dir", "Modern Name", MODERN, version="2.3.4")
    manager = await started()
    env = await call(manager, "Modern Name", "env")
    hb = cfg.homebrew
    assert env["HOME"] == str(cfg.user_home) and env["USER"] == cfg.user
    assert env["DECKY_USER"] == cfg.user and env["DECKY_USER_HOME"] == str(cfg.user_home)
    assert env["DECKY_HOME"] == str(hb)
    assert env["DECKY_VERSION"] == cfg.decky_version
    # paths go by folder name, not by plugin name
    assert env["DECKY_PLUGIN_SETTINGS_DIR"] == str(hb / "settings" / "modern-dir")
    assert env["DECKY_PLUGIN_RUNTIME_DIR"] == str(hb / "data" / "modern-dir")
    assert env["DECKY_PLUGIN_LOG_DIR"] == str(hb / "logs" / "modern-dir")
    assert env["DECKY_PLUGIN_DIR"] == str(path)
    assert env["DECKY_PLUGIN_NAME"] == "Modern Name"
    assert env["DECKY_PLUGIN_VERSION"] == "2.3.4"
    assert env["DECKY_PLUGIN_AUTHOR"] == "tester"
    for sub in ("settings", "data", "logs"):
        assert (hb / sub / "modern-dir").is_dir()


async def test_no_package_json_means_no_version(make_plugin, started) -> None:
    make_plugin("modern", "Modern", MODERN, version=None)
    manager = await started()
    assert "DECKY_PLUGIN_VERSION" not in await call(manager, "Modern", "env")
    assert manager.get_plugins() == [{"name": "Modern", "version": None, "load_type": 0,
                                      "disabled": False}]


async def test_py_modules_on_sys_path(make_plugin, started) -> None:
    make_plugin("mods", "Mods", """
        import helper_mod
        class Plugin:
            async def value(self):
                return helper_mod.VALUE
    """, files={"py_modules/helper_mod.py": "VALUE = 42\n"})
    manager = await started()
    assert await call(manager, "Mods", "value") == 42


async def test_passive_plugin(make_plugin, started) -> None:
    make_plugin("passive", "Passive", None, files={"dist/index.js": "export default 1"})
    manager = await started()
    assert manager.status("Passive")["state"] == "passive"
    with pytest.raises(RouteError) as e:
        await call(manager, "Passive", "anything")
    assert e.value.message == "This plugin is passive"


async def test_unknown_plugin(started) -> None:
    manager = await started()
    with pytest.raises(RouteError) as e:
        await call(manager, "Nobody", "x")
    assert e.value.name == "KeyError"


# -- events ---------------------------------------------------------------------------------

async def test_emit_events(make_plugin, started, ws) -> None:
    make_plugin("modern", "Modern", MODERN)
    manager = await started()
    assert await call(manager, "Modern", "shout", 1, "two", {"three": 3}) == 3
    await wait_for(lambda: ws.named("loader/plugin_event"), what="the event")
    assert ws.named("loader/plugin_event") == [
        [{"plugin": "Modern", "event": "shouted", "args": [1, "two", {"three": 3}]}]]


async def test_emit_from_main_before_the_frontend_asks(make_plugin, started, ws) -> None:
    make_plugin("early", "Early", """
        import decky
        class Plugin:
            async def _main(self):
                await decky.emit("hello", "early")
    """)
    await started()
    await wait_for(lambda: ws.named("loader/plugin_event"), what="the early event")
    assert ws.named("loader/plugin_event")[0] == [{"plugin": "Early", "event": "hello", "args": ["early"]}]


# -- lifecycle ------------------------------------------------------------------------------

async def test_migration_runs_before_calls(make_plugin, manager: PluginManager) -> None:
    make_plugin("mig", "Mig", """
        import asyncio
        class Plugin:
            migrated = False
            async def _migration(self):
                await asyncio.sleep(0.5)
                Plugin.migrated = True
            async def was_migrated(self):
                return self.migrated
    """)
    await manager.start()
    proc = manager.plugins["Mig"].proc
    assert proc is not None and proc.state == State.STARTING
    # the call waits for _migration instead of failing or running first
    assert await manager.call("Mig", "was_migrated") is True
    assert proc.state == State.RUNNING


async def test_main_runs_as_a_task(make_plugin, started) -> None:
    make_plugin("loop", "Loop", """
        import asyncio
        class Plugin:
            ticks = 0
            async def _main(self):
                while True:  # a _main that never returns doesn't block calls
                    Plugin.ticks += 1
                    await asyncio.sleep(0.01)
            async def get_ticks(self):
                return self.ticks
    """)
    manager = await started()
    first = await call(manager, "Loop", "get_ticks")
    await asyncio.sleep(0.1)
    assert await call(manager, "Loop", "get_ticks") > first


async def test_modern_main_ran(make_plugin, started) -> None:
    make_plugin("modern", "Modern", MODERN)
    manager = await started()
    assert await wait_for_call(manager, "Modern", "did_main")


async def wait_for_call(manager: PluginManager, name: str, method: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while True:
        value = await manager.call(name, method)
        if value or time.monotonic() > deadline:
            return value
        await asyncio.sleep(0.02)


async def test_unload_on_stop_uninstall_only_on_uninstall(make_plugin, started, cfg: Config) -> None:
    make_plugin("modern", "Modern", MODERN)
    make_plugin("gone", "Gone", MODERN)
    manager = await started()
    data = cfg.homebrew / "data"
    await manager.unload("Modern")
    assert (data / "modern" / "unloaded").exists()
    assert not (data / "modern" / "uninstalled").exists()
    assert "Modern" not in manager.plugins
    await manager.unload("Gone", uninstall=True)
    assert (data / "gone" / "unloaded").exists()
    assert (data / "gone" / "uninstalled").exists()


async def test_legacy_unload_hook(make_plugin, started, cfg: Config) -> None:
    make_plugin("legacy", "Legacy", """
        import os, decky_plugin
        class Plugin:
            async def _unload(self):
                assert isinstance(self, type)
                open(os.path.join(decky_plugin.DECKY_PLUGIN_RUNTIME_DIR, "unloaded"), "w").close()
    """, api_version=0)
    manager = await started()
    await manager.unload("Legacy")
    assert (cfg.homebrew / "data" / "legacy" / "unloaded").exists()


async def test_stuck_unload_is_killed_with_its_children(make_plugin, started, cfg: Config) -> None:
    cfg.stop_timeout = 0.5
    make_plugin("stuck", "Stuck", """
        import asyncio, subprocess
        class Plugin:
            async def _main(self):
                self.child = subprocess.Popen(["sleep", "60"])
            async def child_pid(self):
                return self.child.pid if hasattr(self, "child") else None
            async def _unload(self):
                await asyncio.sleep(60)
    """)
    manager = await started()
    child = await wait_for_call(manager, "Stuck", "child_pid")
    assert child
    proc = manager.plugins["Stuck"].proc
    assert proc is not None
    started_at = time.monotonic()
    await proc.stop()
    assert time.monotonic() - started_at < 3
    assert proc.state == State.STOPPED

    def child_gone() -> bool:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return True
        # a zombie of the killed host is gone as far as the plugin is concerned
        try:
            return Path(f"/proc/{child}/stat").read_text().split(")")[-1].split()[0] == "Z"
        except OSError:
            return True
    await wait_for(child_gone, timeout=3, what="the plugin's child to die")


async def test_call_timeout(make_plugin, started, cfg: Config) -> None:
    cfg.call_timeout = 0.3
    make_plugin("modern", "Modern", MODERN)
    manager = await started()
    with pytest.raises(RouteError) as e:
        await call(manager, "Modern", "slow", 5)
    assert e.value.name == "TimeoutError"
    assert await call(manager, "Modern", "slow", 0) == "done"


async def test_calls_run_concurrently(make_plugin, started) -> None:
    make_plugin("modern", "Modern", MODERN)
    manager = await started()
    t0 = time.monotonic()
    results = await asyncio.gather(*(call(manager, "Modern", "slow", 0.3) for _ in range(5)))
    assert results == ["done"] * 5
    assert time.monotonic() - t0 < 1.2


# -- crashes --------------------------------------------------------------------------------

async def test_crash_fails_pending_calls_and_restarts(make_plugin, started, fast_restarts, ws) -> None:
    make_plugin("modern", "Modern", MODERN)
    manager = await started()
    proc = manager.plugins["Modern"].proc
    assert proc is not None
    first_pid = await call(manager, "Modern", "pid")
    pending = asyncio.ensure_future(call(manager, "Modern", "slow", 30))
    await asyncio.sleep(0.1)
    # a crash is an error with the plugin's error text, not a DISCARD the frontend never settles
    with pytest.raises(RouteError) as died:
        await call(manager, "Modern", "die")
    assert died.value.name == "PluginStopped" and "exited with code 3" in died.value.message
    with pytest.raises(RouteError):
        await asyncio.wait_for(pending, 5)
    assert proc.state in (State.CRASHED, State.STARTING, State.RUNNING)
    crashed = [s for s in ws.named("remora/plugin_status") if s[0]["state"] == "crashed"]
    assert crashed and "exited with code 3" in crashed[0][0]["error"]
    assert f"restarting in {fast_restarts[0]:g} s" in crashed[0][0]["error"]
    await wait_for(lambda: proc.state == State.RUNNING and proc.restarts == 1, what="the restart")
    assert await call(manager, "Modern", "pid") != first_pid
    assert manager.status("Modern")["restarts"] == 1


async def test_crash_restarts_give_up_after_the_limit(make_plugin, started, fast_restarts) -> None:
    make_plugin("modern", "Modern", MODERN)
    manager = await started()
    proc = manager.plugins["Modern"].proc
    assert proc is not None
    for n in range(len(fast_restarts)):
        await wait_for(lambda: proc.state == State.RUNNING, what=f"run {n}")
        with pytest.raises(RouteError):
            await call(manager, "Modern", "die")
        assert f"restarting in {fast_restarts[n]:g} s" in (proc.error or "")
    await wait_for(lambda: proc.state == State.RUNNING, what="the last run")
    with pytest.raises(RouteError):
        await call(manager, "Modern", "die")
    await wait_for(lambda: proc.state == State.CRASHED and "not restarting" in (proc.error or ""),
                   what="giving up")
    await asyncio.sleep(max(fast_restarts) + 0.2)
    assert proc.state == State.CRASHED and proc.restarts == len(fast_restarts)
    with pytest.raises(RouteError) as e:
        await call(manager, "Modern", "add", 1, 1)
    assert "not restarting" in e.value.message
    # a reload starts it again, with a clean slate
    await manager.reload("Modern")
    assert await call(manager, "Modern", "add", 1, 1) == 2


async def test_plugin_failing_at_import(make_plugin, started, fast_restarts) -> None:
    make_plugin("broken", "Broken", """
        import does_not_exist_anywhere
        class Plugin:
            pass
    """)
    make_plugin("modern", "Modern", MODERN)
    manager = await started()
    proc = manager.plugins["Broken"].proc
    assert proc is not None
    await wait_for(lambda: proc.state == State.CRASHED and "not restarting" in (proc.error or ""),
                   what="giving up on the broken plugin")
    assert "ModuleNotFoundError" in (proc.error or "")
    assert proc.restarts == len(fast_restarts)
    status = manager.status("Broken")
    assert status["state"] == "crashed" and "does_not_exist_anywhere" in status["error"]
    # the others don't care
    assert await call(manager, "Modern", "add", 1, 2) == 3


async def test_call_while_failing_to_start_is_an_error(make_plugin, manager: PluginManager,
                                                       fast_restarts) -> None:
    make_plugin("broken", "Broken", "raise SystemExit('nope')\n")
    await manager.start()
    with pytest.raises(RouteError) as e:
        await asyncio.wait_for(manager.call("Broken", "x"), 5)
    assert e.value.name == "PluginStopped"


async def test_call_to_a_stopping_plugin_is_discarded(make_plugin, started) -> None:
    make_plugin("modern", "Modern", MODERN)
    manager = await started()
    pending = asyncio.ensure_future(call(manager, "Modern", "slow", 30))
    await asyncio.sleep(0.1)
    await manager.disable("Modern")
    with pytest.raises(Discard):
        await asyncio.wait_for(pending, 5)


async def test_missing_plugin_class(make_plugin, started, fast_restarts) -> None:
    make_plugin("noclass", "NoClass", "X = 1\n")
    manager = await started()
    proc = manager.plugins["NoClass"].proc
    assert proc is not None
    await wait_for(lambda: proc.error and "AttributeError" in proc.error, what="the error")


# -- manager --------------------------------------------------------------------------------

async def test_disable_and_enable(make_plugin, started, settings, ws) -> None:
    make_plugin("modern", "Modern", MODERN)
    manager = await started()
    await manager.disable("Modern")
    assert settings.get("disabled_plugins") == ["Modern"]
    assert manager.status("Modern")["state"] == "disabled"
    assert manager.get_plugins()[0]["disabled"] is True
    with pytest.raises(RouteError):
        await call(manager, "Modern", "add", 1, 1)
    assert ["Modern"] in ws.named("loader/disable_plugin")
    await manager.enable("Modern")
    assert settings.get("disabled_plugins") == []
    assert await call(manager, "Modern", "add", 1, 1) == 2
    assert ws.named("loader/import_plugin")[-1] == ["Modern", "1.0.0", 1, True, 15000]


async def test_disabled_at_start_is_not_started(make_plugin, started, settings) -> None:
    settings.set("disabled_plugins", ["Modern"])
    make_plugin("modern", "Modern", MODERN)
    manager = await started()
    assert manager.plugins["Modern"].proc is None


async def test_plugin_order(make_plugin, started, settings) -> None:
    make_plugin("b", "B", None)
    make_plugin("a", "A", None)
    make_plugin("c", "C", None)
    manager = await started()
    assert settings.get("pluginOrder") == ["A", "B", "C"]
    settings.set("pluginOrder", ["C", "A"])
    assert [p["name"] for p in manager.get_plugins()] == ["C", "A", "B"]


async def test_duplicate_names_are_refused(make_plugin, started) -> None:
    make_plugin("one", "Same", None)
    make_plugin("two", "Same", None)
    manager = await started()
    assert manager.plugins["Same"].info.dir_name == "one"
    assert any("two" in path for path, _ in manager.scan_errors)


async def test_bad_manifests_are_skipped(make_plugin, started, cfg: Config) -> None:
    make_plugin("noname", plugin_json={"author": "x", "flags": []})
    bad = cfg.plugins_dir / "badjson"
    bad.mkdir(parents=True)
    (bad / "plugin.json").write_text("{nope")
    make_plugin("ok", "Ok", None)
    manager = await started()
    assert list(manager.plugins) == ["Ok"]
    assert len(manager.scan_errors) == 2


async def test_root_plugins(make_plugin, started, settings, cfg: Config) -> None:
    # coming from Decky: root plugins already there are approved once
    make_plugin("rooty", "Rooty", MODERN, flags=["root"])
    manager = await started()
    assert manager.root_approved("Rooty")
    assert settings.get(ROOT_MIGRATED) is True
    assert manager.status("Rooty")["approved"] is True


async def test_new_root_plugin_waits_for_approval(make_plugin, manager: PluginManager, settings,
                                                  cfg: Config) -> None:
    settings.update({ROOT_MIGRATED: True})
    make_plugin("rooty", "Rooty", MODERN, flags=["root"])
    await manager.start()
    status = manager.status("Rooty")
    assert status["state"] == "blocked" and status["approved"] is False
    with pytest.raises(RouteError):
        await manager.call("Rooty", "add", 1, 1)
    await manager.set_root_permission("Rooty", True)
    assert await manager.call("Rooty", "add", 1, 1) == 2


async def test_reload_picks_up_changes(make_plugin, started, ws) -> None:
    path = make_plugin("modern", "Modern", MODERN)
    manager = await started()
    (path / "main.py").write_text(MODERN.replace("return a + b", "return a * b"))
    await manager.reload("Modern")
    assert await call(manager, "Modern", "add", 3, 4) == 12
    assert ws.named("loader/import_plugin")[-1][0] == "Modern"


async def test_live_reload_for_debug_plugins(make_plugin, manager: PluginManager, cfg: Config,
                                             ws) -> None:
    cfg.live_reload = True
    dbg = make_plugin("dbg", "Dbg", MODERN, flags=["debug"])
    plain = make_plugin("plain", "Plain", MODERN)
    await manager.start()
    need_watches(manager)
    await wait_for(lambda: all(e.proc and e.proc.state == State.RUNNING for e in manager.plugins.values()))
    await asyncio.sleep(0.3)  # starting reads main.py; that must not count as a change
    (dbg / "main.py").write_text(MODERN.replace("return a + b", "return a - b"))
    (plain / "main.py").write_text(MODERN.replace("return a + b", "return a - b"))
    await wait_for(lambda: ws.named("loader/import_plugin"), what="the live reload")
    await wait_for(lambda: manager.plugins["Dbg"].proc.state == State.RUNNING)  # type: ignore[union-attr]
    assert await manager.call("Dbg", "add", 5, 3) == 2
    assert await manager.call("Plain", "add", 5, 3) == 8  # no "debug" flag: no reload
    await asyncio.sleep(1.5)
    assert len(ws.named("loader/import_plugin")) == 1  # and no reload loop


async def test_status_route_shape(make_plugin, started) -> None:
    make_plugin("modern", "Modern", MODERN)
    manager = await started()
    status = manager.status("Modern")
    assert status == {"name": "Modern", "dir": "modern", "version": "1.0.0", "loadType": 1,
                      "state": "running", "restarts": 0, "error": None, "root": False,
                      "approved": True}


async def test_process_directly(make_plugin, cfg: Config) -> None:
    """PluginProcess on its own, as `remora call` uses it."""
    from remora.plugins.manifest import read_plugin

    info = read_plugin(make_plugin("modern", "Modern", MODERN))
    events: list[tuple[str, list]] = []
    states: list[str] = []

    async def on_event(event: str, args: list) -> None:
        events.append((event, args))

    async def on_state(p: PluginProcess) -> None:
        states.append(p.state.value)

    (cfg.homebrew / "data" / "modern").mkdir(parents=True)
    proc = PluginProcess(cfg, info, on_event, on_state)
    await proc.start()
    try:
        assert await proc.call("shout", ["x"]) == 1
        assert await proc.call("greet", [], {}) == "hello world!"
    finally:
        await proc.stop()
    assert states[0] == "starting" and "running" in states and states[-1] == "stopped"
    await wait_for(lambda: events, what="the event")
    assert events == [("shouted", ["x"])]


async def test_call_during_a_restart_waits_for_it(make_plugin, cfg: Config, fast_restarts) -> None:
    """A call that comes in while a crashed host is starting again waits instead of failing."""
    from remora.plugins.manifest import read_plugin

    info = read_plugin(make_plugin("modern", "Modern", MODERN))
    (cfg.homebrew / "data" / "modern").mkdir(parents=True)
    calls: list[asyncio.Future] = []
    starts = 0

    async def on_event(event: str, args: list) -> None:
        pass

    async def on_state(p: PluginProcess) -> None:
        nonlocal starts
        if p.state == State.STARTING:
            starts += 1
            if starts == 2:  # the restart, before the new host exists
                calls.append(asyncio.ensure_future(p.call("add", [1, 2])))

    proc = PluginProcess(cfg, info, on_event, on_state)
    await proc.start()
    try:
        with pytest.raises(Exception):
            await proc.call("die")
        await wait_for(lambda: calls, what="the restart")
        assert await asyncio.wait_for(calls[0], 5) == 3
    finally:
        await proc.stop()


async def test_live_reload_after_a_rename_save(make_plugin, manager: PluginManager, cfg: Config,
                                               ws) -> None:
    """Editors and sed -i write a temp file and rename it over main.py."""
    cfg.live_reload = True
    dbg = make_plugin("dbg", "Dbg", MODERN, flags=["debug"])
    await manager.start()
    need_watches(manager)
    await wait_for(lambda: manager.plugins["Dbg"].proc.state == State.RUNNING)  # type: ignore[union-attr]
    await asyncio.sleep(0.3)
    tmp = dbg / ".main.py.swp-new"
    tmp.write_text(MODERN.replace("return a + b", "return a - b"))
    os.replace(tmp, dbg / "main.py")
    await wait_for(lambda: ws.named("loader/import_plugin"), what="the live reload")
    await wait_for(lambda: manager.plugins["Dbg"].proc.state == State.RUNNING)  # type: ignore[union-attr]
    assert await manager.call("Dbg", "add", 5, 3) == 2


async def test_dev_mode_single_plugin_folder(make_plugin, manager: PluginManager, cfg: Config,
                                             tmp_path, ws) -> None:
    """remora dev: one plugin folder outside plugins/, reloaded without the debug flag."""
    cfg.live_reload = True
    cfg.reload_all = True
    src = make_plugin("my-plugin", "Dev Plugin", MODERN, into=tmp_path / "src")
    (src / "node_modules" / "x").mkdir(parents=True)
    cfg.extra_plugin_dirs = [src]
    make_plugin("other", "Other", None)  # plugins/ still loads too
    await manager.start()
    need_watches(manager)
    assert set(manager.plugins) == {"Dev Plugin", "Other"}
    await wait_for(lambda: manager.plugins["Dev Plugin"].proc.state == State.RUNNING)  # type: ignore[union-attr]
    assert await manager.call("Dev Plugin", "add", 2, 2) == 4
    await asyncio.sleep(0.3)
    (src / "node_modules" / "x" / "ignored.py").write_text("x = 1")
    await asyncio.sleep(0.8)
    assert ws.named("loader/import_plugin") == []  # node_modules doesn't count
    (src / "py_modules").mkdir()
    (src / "py_modules" / "dep.py").write_text("X = 1")
    await wait_for(lambda: ws.named("loader/import_plugin"), what="the live reload")
    assert ws.named("loader/import_plugin")[0][0] == "Dev Plugin"


# -- the `remora call` harness ---------------------------------------------------------------

def remora_cli(*args: str, home: Path) -> "subprocess.CompletedProcess[str]":
    import subprocess
    import sys

    from conftest import BACKEND

    env = {**os.environ, "PYTHONPATH": str(BACKEND)}
    return subprocess.run([sys.executable, "-m", "remora", *args, "--home", str(home)],
                          capture_output=True, text=True, timeout=60, env=env, cwd=str(BACKEND))


def test_cli_call_modern(make_plugin, tmp_path) -> None:
    path = make_plugin("modern", "Modern", MODERN, into=tmp_path / "src")
    home = tmp_path / "dev-home"
    res = remora_cli("call", str(path), "add", "2", "3", home=home)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == 5
    res = remora_cli("call", str(path), "greet", "kettle", '"?"', "--compact", home=home)
    assert res.stdout.strip() == '"hello kettle?"'
    res = remora_cli("call", str(path), "shout", '{"a": 1}', home=home)
    assert res.returncode == 0 and 'event shouted [{"a": 1}]' in res.stderr
    # _unload ran when it stopped, in the dev home
    assert (home / "data" / "modern" / "unloaded").exists()


def test_cli_call_errors(make_plugin, tmp_path) -> None:
    path = make_plugin("modern", "Modern", MODERN, into=tmp_path / "src")
    home = tmp_path / "dev-home"
    res = remora_cli("call", str(path), "boom", home=home)
    assert res.returncode == 1 and "ValueError: kaboom" in res.stderr and res.stdout == ""
    res = remora_cli("call", str(path), "die", home=home)
    assert res.returncode == 1 and "exited with code 3" in res.stderr
    res = remora_cli("call", str(tmp_path), "x", home=home)
    assert res.returncode == 2 and "no plugin.json" in res.stderr


def test_cli_call_legacy(make_plugin, tmp_path) -> None:
    path = make_plugin("legacy", "Legacy", LEGACY, api_version=None, into=tmp_path / "src")
    home = tmp_path / "dev-home"
    res = remora_cli("call", str(path), "greet", "who=kettle", "--wait", "0.2", home=home)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout).startswith("hello kettle from ")
    res = remora_cli("call", str(path), "bump", '{"by": 5}', "--legacy", home=home)
    assert json.loads(res.stdout) == 5
