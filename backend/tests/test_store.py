"""The store: sources, listing, updates, the install pipeline, uninstall."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import pwd
import stat
import zipfile
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from remora.config import Config
from remora.plugins.manager import PluginManager
from remora.settings import Settings
from remora.store import StoreService
from remora.store import index as store_index
from remora.wsrouter import RouteError, WSRouter

MAIN_PY = """
import os

class Plugin:
    async def _main(self):
        pass

    async def _uninstall(self):
        with open(os.path.join(os.environ["DECKY_PLUGIN_RUNTIME_DIR"], "uninstalled"), "w") as f:
            f.write("yes")
"""


# -- helpers ---------------------------------------------------------------------------------

def plugin_files(name: str, version: str, folder: str = "my-plugin", root: bool = False,
                 main: bool = False, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    files: dict[str, Any] = {
        f"{folder}/plugin.json": json.dumps({"name": name, "author": "me",
                                             "flags": ["root"] if root else [], "api_version": 1}),
        f"{folder}/package.json": json.dumps({"version": version, "type": "module"}),
        f"{folder}/dist/index.js": "export default 1;",
        f"{folder}/bin/tool": "#!/bin/sh\necho hi\n",
    }
    if main:
        files[f"{folder}/main.py"] = MAIN_PY
    files.update(extra or {})
    return files


def make_zip(files: dict[str, Any], symlinks: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, data in files.items():
            zf.writestr(path, data if isinstance(data, bytes) else str(data).encode())
        for path, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(path)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, target)
    return buf.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RecordingWS(WSRouter):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, tuple[Any, ...]]] = []
        self.root_answer: bool | None = None
        self.store: StoreService | None = None

    async def emit(self, event: str, *args: Any) -> None:
        self.events.append((event, args))
        if event == "remora/root_prompt" and self.root_answer is not None and self.store:
            await self.store.answer_root(args[0], self.root_answer)

    def named(self, event: str) -> list[tuple[Any, ...]]:
        return [a for e, a in self.events if e == event]


class FakeStore:
    """One aiohttp server playing a Decky store, an index source, and a 426-ing old store."""

    def __init__(self) -> None:
        self.zips: dict[str, bytes] = {}
        self.decky: list[dict[str, Any]] = []
        self.index: dict[str, Any] = {"format": "decky-index/1", "source": {"id": "test"}, "plugins": []}
        self.increments: list[str] = []
        self.headers: list[str | None] = []
        app = web.Application()
        app.router.add_get("/plugins", self.list)
        app.router.add_get("/plugins/versions", self.versions)
        app.router.add_post("/plugins/{name}/versions/{version}/increment", self.increment)
        app.router.add_get("/zips/{file}", self.zip)
        app.router.add_get("/idx/index.json", self.get_index)
        app.router.add_get("/old/plugins", self.too_old)
        self.server = TestServer(app)

    def url(self, path: str) -> str:
        return str(self.server.make_url(path))

    def add_zip(self, data: bytes) -> tuple[str, str]:
        h = sha(data)
        self.zips[h] = data
        return h, self.url(f"/zips/{h}.zip")

    def publish(self, name: str, version: str, data: bytes, **extra: Any) -> str:
        h, url = self.add_zip(data)
        entry = next((p for p in self.decky if p["name"] == name), None)
        if entry is None:
            entry = {"id": len(self.decky) + 1, "name": name, "author": "me", "description": f"{name} desc",
                     "tags": extra.pop("tags", []), "image_url": None, "versions": [], "downloads": 0}
            self.decky.append(entry)
        entry["versions"].insert(0, {"name": version, "hash": h, "artifact": url,
                                     "created": "2026-10-01T00:00:00Z"})
        return h

    async def list(self, request: web.Request) -> web.Response:
        self.headers.append(request.headers.get("X-Decky-Version"))
        return web.json_response(self.decky)

    async def versions(self, request: web.Request) -> web.Response:
        return web.json_response({p["name"]: [p["versions"][0]["name"], p["versions"][0]["hash"],
                                               p["versions"][0]["artifact"]] for p in self.decky})

    async def increment(self, request: web.Request) -> web.Response:
        self.increments.append(f"{request.match_info['name']}/{request.match_info['version']}"
                               f"?{request.query_string}")
        return web.json_response({})

    async def zip(self, request: web.Request) -> web.Response:
        data = self.zips.get(request.match_info["file"].removesuffix(".zip"))
        if data is None:
            raise web.HTTPNotFound()
        return web.Response(body=data, content_type="application/zip")

    async def get_index(self, request: web.Request) -> web.Response:
        return web.json_response(self.index)

    async def too_old(self, request: web.Request) -> web.Response:
        return web.Response(status=426, text="Unsupported loader version, please upgrade.")


@pytest.fixture
async def fake_store():
    store = FakeStore()
    await store.server.start_server()
    yield store
    await store.server.close()


def make_cfg(tmp_path: Path) -> Config:
    me = pwd.getpwuid(os.getuid())
    home = tmp_path / "home"
    homebrew = home / "homebrew"
    (homebrew / "plugins").mkdir(parents=True)
    (homebrew / "settings").mkdir(parents=True)
    return Config(user=me.pw_name, uid=me.pw_uid, gid=me.pw_gid, user_home=home, homebrew=homebrew,
                  privileged=homebrew, live_reload=False, inject=False, stop_timeout=3.0)


class Env:
    def __init__(self, tmp_path: Path, fake: FakeStore, settings: dict[str, Any] | None = None) -> None:
        self.cfg = make_cfg(tmp_path)
        if settings:
            self.cfg.settings_file.write_text(json.dumps(settings))
        self.settings = Settings(self.cfg.settings_file)
        self.ws = RecordingWS()
        self.manager = PluginManager(self.cfg, self.settings, self.ws)
        self.store = StoreService(self.cfg, self.settings, self.manager, self.ws)
        self.ws.store = self.store
        self.store.register(self.ws)
        self.fake = fake

    async def start(self) -> "Env":
        await self.manager.start()
        # only the fake store, so tests never touch the network
        await self.store.set_sources([
            {"id": "decky", "enabled": False},
            {"id": "local", "name": "Local", "kind": "decky", "url": self.fake.url("/")},
        ])
        return self

    async def stop(self) -> None:
        await self.manager.shutdown()
        await self.store.close()

    async def install(self, name: str, version: str, h: str, artifact: str | None = None) -> None:
        artifact = artifact or self.fake.url(f"/zips/{h}.zip")
        await self.ws.routes["utilities/install_plugin"](artifact, name, version, h, 0)
        request_id = self.ws.named("loader/add_plugin_install_prompt")[-1][2]
        await self.ws.routes["utilities/confirm_plugin_install"](request_id)

    def failures(self) -> list[tuple[Any, ...]]:
        return self.ws.named("remora/install_failed")

    def leftovers(self) -> list[str]:
        return [p for p in os.listdir(self.cfg.plugins_dir) if p.startswith(".")]


@pytest.fixture
async def env(tmp_path: Path, fake_store: FakeStore):
    e = await Env(tmp_path, fake_store).start()
    yield e
    await e.stop()


# -- sources ---------------------------------------------------------------------------------

async def test_decky_custom_store_migrates(tmp_path: Path, fake_store: FakeStore) -> None:
    e = Env(tmp_path, fake_store, {"store": 2, "store-url": "https://example.org/plugins"})
    sources = await e.ws.routes["remora/store/sources"]()
    by_id = {s["id"]: s for s in sources}
    assert by_id["decky"]["enabled"] and by_id["decky"]["builtin"]
    assert not by_id["decky-testing"]["enabled"]
    assert by_id["custom"] == {"id": "custom", "name": "example.org", "kind": "decky",
                               "url": "https://example.org/plugins", "enabled": True, "builtin": False}
    assert e.settings.get("remora.storeSources")
    await e.store.close()


async def test_set_sources_keeps_builtins_and_checks_urls(env: Env) -> None:
    result = await env.ws.routes["remora/store/set_sources"]([{"name": "Mine", "kind": "index",
                                                               "url": "https://x.org/index.json"}])
    assert [s["id"] for s in result] == ["decky", "decky-testing", "mine"]
    with pytest.raises(RouteError):
        await env.ws.routes["remora/store/set_sources"]([{"kind": "index", "url": "file:///etc/x.json"}])
    with pytest.raises(RouteError):
        await env.ws.routes["remora/store/set_sources"]([{"kind": "bogus", "url": "https://x.org"}])


# -- listing ---------------------------------------------------------------------------------

async def test_list_merges_sources_and_reports_426(env: Env, fake_store: FakeStore) -> None:
    fake_store.publish("Zeta", "1.0.0", make_zip(plugin_files("Zeta", "1.0.0")), tags=["root"])
    h, url = fake_store.add_zip(make_zip(plugin_files("Alpha", "2.0.0")))
    fake_store.index["plugins"] = [{"name": "Alpha", "author": "a", "description": "first",
                                    "tags": ["x"], "versions": [{"name": "2.0.0", "hash": h,
                                                                 "artifact": f"../zips/{h}.zip"}]}]
    await env.ws.routes["remora/store/set_sources"]([
        {"id": "local", "name": "Local", "kind": "decky", "url": fake_store.url("/plugins")},
        {"id": "idx", "name": "Idx", "kind": "index", "url": fake_store.url("/idx")},
        {"id": "old", "name": "Old", "kind": "decky", "url": fake_store.url("/old")},
    ])
    result = await env.ws.routes["remora/store/list"]()
    status = {s["id"]: s for s in result["sources"]}
    assert status["local"]["ok"] and status["idx"]["ok"]
    assert not status["old"]["ok"] and "newer loader" in status["old"]["error"]
    assert [(p["name"], p["source"]) for p in result["plugins"]] == [("Alpha", "idx"), ("Zeta", "local")]
    alpha = result["plugins"][0]
    assert alpha["versions"][0]["artifact"] == url  # relative to the index
    assert alpha["versions"][0]["signed"] is False
    assert fake_store.headers[-1] == env.cfg.decky_version

    desc = await env.ws.routes["remora/store/list"]("name", "desc", "ZET")
    assert [p["name"] for p in desc["plugins"]] == ["Zeta"]


# -- install ---------------------------------------------------------------------------------

async def test_install_sets_modes_order_and_source(env: Env, fake_store: FakeStore) -> None:
    files = plugin_files("Fresh", "1.0.0", folder="decky-fresh",
                         extra={"decky-fresh/run.sh": b"#!/bin/sh\n"})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, data in files.items():
            info = zipfile.ZipInfo(path)
            info.external_attr = ((0o100755 if path.endswith("run.sh") else 0o100666) << 16)
            zf.writestr(info, data)
    h = fake_store.publish("Fresh", "1.0.0", buf.getvalue())
    await env.install("Fresh", "1.0.0", h)
    assert env.failures() == []
    plugin = env.cfg.plugins_dir / "decky-fresh"
    assert stat.S_IMODE(plugin.stat().st_mode) == 0o755
    assert stat.S_IMODE((plugin / "plugin.json").stat().st_mode) == 0o644
    assert stat.S_IMODE((plugin / "bin" / "tool").stat().st_mode) == 0o755
    assert stat.S_IMODE((plugin / "run.sh").stat().st_mode) == 0o755
    assert "Fresh" in env.manager.plugins
    assert env.settings.get("pluginOrder")[-1] == "Fresh"
    assert env.settings.get("remora.installSource") == {"Fresh": "local"}
    assert env.ws.named("loader/plugin_download_start") == [("Fresh",)]
    assert env.ws.named("loader/plugin_download_finish") == [("Fresh",)]
    assert ("Fresh", "1.0.0", 1, True, 15000) in env.ws.named("loader/import_plugin")
    assert env.leftovers() == []
    assert fake_store.increments == []  # telemetry is off by default


async def test_install_plugins_multiple_prompt_and_cancel(env: Env, fake_store: FakeStore) -> None:
    h1 = fake_store.publish("One", "1.0.0", make_zip(plugin_files("One", "1.0.0", folder="one")))
    h2 = fake_store.publish("Two", "1.0.0", make_zip(plugin_files("Two", "1.0.0", folder="two")))
    reqs = [{"artifact": fake_store.url(f"/zips/{h}.zip"), "name": n, "version": "1.0.0", "hash": h,
             "install_type": 0} for n, h in (("One", h1), ("Two", h2))]
    await env.ws.routes["utilities/install_plugins"](reqs)
    request_id, items = env.ws.named("loader/add_multiple_plugins_install_prompt")[-1]
    assert [i["name"] for i in items] == ["One", "Two"]
    details = env.ws.named("remora/install_request")[-1]
    assert details[0] == request_id and details[1][0]["source"] == "local"
    await env.ws.routes["utilities/cancel_plugin_install"](request_id)
    with pytest.raises(RouteError):
        await env.ws.routes["utilities/confirm_plugin_install"](request_id)
    await env.ws.routes["utilities/install_plugins"](reqs)
    request_id = env.ws.named("loader/add_multiple_plugins_install_prompt")[-1][0]
    await env.ws.routes["utilities/confirm_plugin_install"](request_id)
    assert {"One", "Two"} <= set(env.manager.plugins)


@pytest.mark.parametrize("files,symlinks,message", [
    ({"p/plugin.json": "{}", "p/../../evil": "x"}, None, "traversal"),
    ({"/abs/plugin.json": "{}"}, None, "absolute"),
    (plugin_files("Bad", "1.0.0", folder="p"), {"p/link": "/etc/passwd"}, "symlink"),
    ({**plugin_files("Bad", "1.0.0", folder="p"), "q/x": "y"}, None, "exactly one top-level"),
    ({**plugin_files("Bad", "1.0.0", folder="p"), "stray.txt": "y"}, None, "outside the plugin folder"),
    (plugin_files("Other", "1.0.0", folder="p"), None, "not 'Bad'"),
    ({"p/package.json": "{}"}, None, "no plugin.json"),
])
async def test_unsafe_zips_are_refused(env: Env, fake_store: FakeStore, files: dict[str, Any],
                                       symlinks: dict[str, str] | None, message: str) -> None:
    h = fake_store.publish("Bad", "1.0.0", make_zip(files, symlinks))
    await env.install("Bad", "1.0.0", h)
    failures = env.failures()
    assert len(failures) == 1 and failures[0][0] == "Bad"
    assert message in failures[0][1]
    assert "Bad" not in env.manager.plugins
    assert os.listdir(env.cfg.plugins_dir) == []
    assert not (env.cfg.homebrew.parent / "evil").exists()


async def test_hash_mismatch_is_refused(env: Env, fake_store: FakeStore) -> None:
    data = make_zip(plugin_files("Hashy", "1.0.0"))
    h, url = fake_store.add_zip(data)
    await env.install("Hashy", "1.0.0", "0" * 64, artifact=url)
    assert "hash mismatch" in env.failures()[0][1]
    assert os.listdir(env.cfg.plugins_dir) == []


async def test_no_hash_needs_developer_mode(env: Env, fake_store: FakeStore) -> None:
    data = make_zip(plugin_files("Dev", "0.1.0", folder="dev"))
    _, url = fake_store.add_zip(data)
    await env.install("dev", "dev", "", artifact=url)
    assert "no hash" in env.failures()[0][1]
    zip_path = env.cfg.user_home / "dev.zip"
    zip_path.write_bytes(data)
    await env.install("dev", "dev", "", artifact=f"file://{zip_path}")
    assert "developer mode" in env.failures()[-1][1]
    env.settings.set("developer.enabled", True)
    await env.install("dev", "dev", "", artifact=f"file://{zip_path}")
    assert len(env.failures()) == 2
    assert "Dev" in env.manager.plugins  # the name comes from the zip for developer installs


async def test_update_keeps_settings_and_skips_uninstall(env: Env, fake_store: FakeStore) -> None:
    h1 = fake_store.publish("Keeper", "1.0.0", make_zip(plugin_files("Keeper", "1.0.0", main=True)))
    await env.install("Keeper", "1.0.0", h1)
    assert env.failures() == []
    data_dir = env.cfg.homebrew / "data" / "my-plugin"
    (data_dir / "state.json").write_text("{}")
    env.settings.update({"hiddenPlugins": ["Keeper"], "pluginOrder": ["Keeper", "Other"],
                         "disabled_plugins": []})
    h2 = fake_store.publish("Keeper", "1.1.0", make_zip(plugin_files("Keeper", "1.1.0", main=True,
                                                                     folder="renamed-in-zip")))
    updates = await env.ws.routes["remora/store/updates"]()
    assert updates["Keeper"] == {"version": "1.1.0", "hash": h2,
                                 "artifact": fake_store.url(f"/zips/{h2}.zip"), "source": "local"}
    env.settings.set("frozenPlugins", ["Keeper"])
    assert "Keeper" not in await env.ws.routes["remora/store/updates"]()
    env.settings.set("frozenPlugins", [])

    await env.install("Keeper", "1.1.0", h2)
    assert env.failures() == []
    assert env.manager.plugins["Keeper"].info.version == "1.1.0"
    assert env.manager.plugins["Keeper"].info.dir_name == "my-plugin"  # the folder name stays
    assert not (env.cfg.plugins_dir / "renamed-in-zip").exists()
    assert not (data_dir / "uninstalled").exists()  # _uninstall did not run
    assert (data_dir / "state.json").exists()
    assert env.settings.get("hiddenPlugins") == ["Keeper"]
    assert env.settings.get("pluginOrder") == ["Keeper", "Other"]
    assert env.leftovers() == []
    assert "Keeper" not in await env.ws.routes["remora/store/updates"]()


async def test_failed_update_rolls_back(env: Env, fake_store: FakeStore, monkeypatch) -> None:
    h1 = fake_store.publish("Roll", "1.0.0", make_zip(plugin_files("Roll", "1.0.0", main=True)))
    await env.install("Roll", "1.0.0", h1)
    h2 = fake_store.publish("Roll", "2.0.0", make_zip(plugin_files("Roll", "2.0.0", main=True)))
    real = env.manager.load_dir
    calls = []

    async def flaky(path: Path):
        calls.append(path)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return await real(path)

    monkeypatch.setattr(env.manager, "load_dir", flaky)
    await env.install("Roll", "2.0.0", h2)
    failure = env.failures()[-1]
    assert failure[0] == "Roll" and "old version is back" in failure[1]
    assert env.manager.plugins["Roll"].info.version == "1.0.0"
    assert json.loads((env.cfg.plugins_dir / "my-plugin" / "package.json").read_text())["version"] == "1.0.0"
    assert env.manager.status("Roll")["state"] in ("running", "starting")
    assert not (env.cfg.homebrew / "data" / "my-plugin" / "uninstalled").exists()
    assert env.leftovers() == []


@pytest.mark.parametrize("allow", [False, True, None])
async def test_root_prompt(env: Env, fake_store: FakeStore, allow: bool | None) -> None:
    h = fake_store.publish("Rooty", "1.0.0", make_zip(plugin_files("Rooty", "1.0.0", root=True)))
    env.ws.root_answer = allow
    env.store.installer.root_prompt_seconds = 0.2  # None: nobody answers, it times out
    await env.install("Rooty", "1.0.0", h)
    prompts = env.ws.named("remora/root_prompt")
    assert len(prompts) == 1 and prompts[0][1:] == ("Rooty", "1.0.0")
    if allow:
        assert env.failures() == []
        assert env.manager.root_approved("Rooty")
        assert "Rooty" in env.manager.plugins
    else:
        assert "root" in env.failures()[0][1]
        assert "Rooty" not in env.manager.plugins
        assert os.listdir(env.cfg.plugins_dir) == []
        assert not env.manager.root_approved("Rooty")


async def test_uninstall_runs_hook_and_keeps_homebrew_data(env: Env, fake_store: FakeStore) -> None:
    h = fake_store.publish("Gone", "1.0.0", make_zip(plugin_files("Gone", "1.0.0", main=True)))
    await env.install("Gone", "1.0.0", h)
    env.settings.update({"hiddenPlugins": ["Gone"], "frozenPlugins": ["Gone", "X"],
                         "disabled_plugins": []})
    settings_dir = env.cfg.homebrew / "settings" / "my-plugin"
    (settings_dir / "config.json").write_text("{}")
    await env.ws.routes["utilities/uninstall_plugin"]("Gone")
    assert not (env.cfg.plugins_dir / "my-plugin").exists()
    assert "Gone" not in env.manager.plugins
    assert (settings_dir / "config.json").exists()
    assert (env.cfg.homebrew / "data" / "my-plugin" / "uninstalled").exists()
    assert "Gone" not in env.settings.get("pluginOrder")
    assert env.settings.get("hiddenPlugins") == []
    assert env.settings.get("frozenPlugins") == ["X"]
    assert "Gone" not in (env.settings.get("remora.installSource") or {})


async def test_system_plugins_refuse_update_and_uninstall(tmp_path: Path, fake_store: FakeStore) -> None:
    system = tmp_path / "usr-share-decky" / "sys-plugin"
    system.mkdir(parents=True)
    (system / "plugin.json").write_text(json.dumps({"name": "Sys", "author": "k", "flags": []}))
    (system / "package.json").write_text(json.dumps({"version": "1.0.0"}))
    e = Env(tmp_path, fake_store)
    (e.cfg.plugins_dir / "sys-plugin").symlink_to(system)
    await e.start()
    try:
        assert "Sys" in e.manager.plugins
        with pytest.raises(RouteError, match="system image"):
            await e.ws.routes["utilities/uninstall_plugin"]("Sys")
        assert system.exists()
        h = fake_store.publish("Sys", "2.0.0", make_zip(plugin_files("Sys", "2.0.0", folder="sys-plugin")))
        assert "Sys" not in await e.ws.routes["remora/store/updates"]()
        await e.install("Sys", "2.0.0", h)
        assert "system image" in e.failures()[0][1]
        assert json.loads((system / "package.json").read_text())["version"] == "1.0.0"
    finally:
        await e.stop()


async def test_new_install_refuses_taken_folder(env: Env, fake_store: FakeStore) -> None:
    h1 = fake_store.publish("First", "1.0.0", make_zip(plugin_files("First", "1.0.0", folder="shared")))
    await env.install("First", "1.0.0", h1)
    h2 = fake_store.publish("Second", "1.0.0", make_zip(plugin_files("Second", "1.0.0", folder="shared")))
    await env.install("Second", "1.0.0", h2)
    assert "already taken by plugin 'First'" in env.failures()[0][1]
    assert env.manager.plugins["First"].info.dir_name == "shared"


async def test_remote_binary(env: Env, fake_store: FakeStore) -> None:
    blob = b"\x7fELF fake binary"
    bh, burl = fake_store.add_zip(blob)
    good = {"my-plugin/package.json": json.dumps({"version": "1.0.0", "remote_binary": [
        {"name": "helper", "url": burl, "sha256hash": bh}]})}
    h = fake_store.publish("Bins", "1.0.0", make_zip(plugin_files("Bins", "1.0.0", extra=good)))
    await env.install("Bins", "1.0.0", h)
    assert env.failures() == []
    helper = env.cfg.plugins_dir / "my-plugin" / "bin" / "helper"
    assert helper.read_bytes() == blob and stat.S_IMODE(helper.stat().st_mode) == 0o755

    bad = {"my-plugin/package.json": json.dumps({"version": "1.0.0", "remote_binary": [
        {"name": "helper", "url": burl, "sha256hash": "0" * 64}]})}
    h = fake_store.publish("BadBins", "1.0.0", make_zip(plugin_files("BadBins", "1.0.0", folder="bb",
                                                                     extra={f"bb/{k.split('/', 1)[1]}": v
                                                                            for k, v in bad.items()})))
    await env.install("BadBins", "1.0.0", h)
    assert "remote_binary helper: hash mismatch" in env.failures()[0][1]
    assert not (env.cfg.plugins_dir / "bb").exists()


async def test_telemetry_only_when_enabled(env: Env, fake_store: FakeStore) -> None:
    env.settings.set("remora.storeTelemetry", True)
    h = fake_store.publish("Count Me", "1.0.0", make_zip(plugin_files("Count Me", "1.0.0")))
    await env.install("Count Me", "1.0.0", h)
    for task in list(env.store._tasks):
        await task
    assert fake_store.increments == ["Count Me/1.0.0?isUpdate=False"]


# -- signed index ----------------------------------------------------------------------------

@pytest.mark.skipif(not store_index.HAVE_CRYPTO, reason="cryptography is not installed")
async def test_signed_index(env: Env, fake_store: FakeStore) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = Ed25519PrivateKey.generate()
    pub = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()

    def version(name: str, ver: str, data: bytes, good: bool = True) -> dict[str, Any]:
        h, url = fake_store.add_zip(data)
        msg = store_index.signed_message(name, ver, h if good else "f" * 64)
        return {"name": ver, "hash": h, "artifact": url, "size": len(data),
                "sig": {"key": "k1", "alg": "ed25519", "value": base64.b64encode(key.sign(msg)).decode()}}

    fake_store.index = {"format": "decky-index/1",
                        "source": {"id": "t", "keys": [{"id": "k1", "alg": "ed25519", "pub": pub}]},
                        "plugins": [
                            {"name": "Good", "versions": [version("Good", "1.0.0", make_zip(
                                plugin_files("Good", "1.0.0", folder="good")))]},
                            {"name": "Forged", "versions": [version("Forged", "1.0.0", make_zip(
                                plugin_files("Forged", "1.0.0", folder="forged")), good=False)]},
                            {"name": "Unsigned", "versions": [{"name": "1.0.0", "hash": "a" * 64,
                                                               "artifact": "x.zip"}]},
                            {"name": "Arm", "versions": [{"name": "1.0.0", "hash": "b" * 64,
                                                          "artifact": "y.zip", "arch": ["nonexistent"]}]},
                        ]}
    await env.ws.routes["remora/store/set_sources"]([{"id": "decky", "enabled": False},
                                                     {"id": "idx", "kind": "index",
                                                      "url": fake_store.url("/idx/index.json")}])
    plugins = {p["name"]: p for p in (await env.ws.routes["remora/store/list"]())["plugins"]}
    assert plugins["Good"]["versions"][0]["signed"] is True
    assert plugins["Forged"]["versions"][0]["signed"] == "invalid"
    assert plugins["Unsigned"]["versions"][0]["signed"] == "invalid"  # a signing source must sign all
    assert plugins["Arm"]["versions"][0]["compatible"] is False

    good = plugins["Good"]["versions"][0]
    await env.install("Good", "1.0.0", good["hash"], artifact=good["artifact"])
    assert env.failures() == []
    assert env.settings.get("remora.installSource") == {"Good": "idx"}
    assert env.ws.named("remora/install_request")[-1][1][0]["signed"] is True

    forged = plugins["Forged"]["versions"][0]
    await env.install("Forged", "1.0.0", forged["hash"], artifact=forged["artifact"])
    assert "signature" in env.failures()[0][1]
    assert "Forged" not in env.manager.plugins


def test_semver_rules() -> None:
    assert store_index.is_newer("1.2.0", "1.1.9")
    assert store_index.is_newer("v1.10.0", "1.9.0")
    assert store_index.is_newer("1.0.0", None)
    assert not store_index.is_newer("1.2.3-abc1234", "1.2.3")
    assert store_index.is_newer("1.2.3", "1.2.3-abc1234")
    assert not store_index.is_newer("nightly", "1.0.0")
    assert not store_index.is_newer("1.0.0", "1.0.0")
