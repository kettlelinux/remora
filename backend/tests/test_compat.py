"""Plugin compatibility: device facts, compat lists, the store's `compat` answers, and the
install-time binary check."""

from __future__ import annotations

import io
import json
import os
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import pytest

from remora import device as device_mod
from remora.device import Device, detect
from remora.store import compat as compat_mod
from remora.store import elf
from remora.store.compat import DATA_FILE, CompatList, evaluate
from remora.wsrouter import RouteError

from test_store import Env, FakeStore, fake_store, make_zip, plugin_files, refetch_fresh_lists  # noqa: F401


def elf_head(machine: int, size: int = 64) -> bytes:
    """The start of a 64-bit little-endian ELF file for this e_machine."""
    head = b"\x7fELF" + bytes([2, 1, 1, 0]) + bytes(8) + (2).to_bytes(2, "little") \
        + machine.to_bytes(2, "little")
    return head + bytes(size - len(head))


X86_ELF = elf_head(0x3E)
ARM_ELF = elf_head(0xB7)
PORTAL_FACTS = {f: False for f in device_mod.FACTS} | {
    "aarch64": True, "qualcomm-cpu": True, "adreno-gpu": True, "flatpak": True, "steamos": True,
    "systemd": True}
PORTAL = Device(arch="aarch64", facts=PORTAL_FACTS, device_id="ayn,odin2portal",
                model="AYN Odin 2 Portal", cpu_vendor="qualcomm", gpu_drivers=["msm_dpu"],
                ids=["ayn,odin2portal", "qcom,qcs8550", "qcom,sm8550"])


def write(root: Path, rel: str, data: str | bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data)
    return path


def drm_card(root: Path, card: str, driver: str) -> None:
    drivers = root / "sys/bus/platform/drivers" / driver
    drivers.mkdir(parents=True, exist_ok=True)
    dev = root / "sys/class/drm" / card / "device"
    dev.mkdir(parents=True)
    os.symlink(drivers, dev / "driver")


# -- device facts ------------------------------------------------------------------------------

def test_device_portal(tmp_path: Path) -> None:
    write(tmp_path, "etc/os-release", 'NAME="Kettle"\nID=steamos\nID_LIKE=arch\n')
    write(tmp_path, "proc/device-tree/model", "AYN Odin 2 Portal\0")
    write(tmp_path, "proc/device-tree/compatible", "ayn,odin2portal\0qcom,qcs8550\0qcom,sm8550\0")
    write(tmp_path, "proc/cpuinfo", "processor\t: 0\nCPU implementer\t: 0x41\n")
    drm_card(tmp_path, "card0", "msm_dpu")
    (tmp_path / "sys/class/drm/card0-DSI-1").mkdir()
    write(tmp_path, "usr/bin/flatpak", "")
    (tmp_path / "run/systemd/system").mkdir(parents=True)
    d = detect(tmp_path, machine="aarch64")
    on = {k for k, v in d.facts.items() if v}
    assert on == {"aarch64", "qualcomm-cpu", "adreno-gpu", "flatpak", "steamos", "systemd"}
    assert set(d.facts) == set(device_mod.FACTS)
    assert d.device_id == "ayn,odin2portal" and d.model == "AYN Odin 2 Portal"
    assert d.cpu_vendor == "qualcomm" and d.gpu_drivers == ["msm_dpu"]
    assert d.to_json()["arch"] == "aarch64"


def test_device_deck_user_and_home(tmp_path: Path) -> None:
    write(tmp_path, "etc/passwd", "root:x:0:0::/root:/bin/bash\nkettle:x:1000:1000::/home/kettle:/bin/bash\n")
    d = detect(tmp_path, machine="aarch64")
    assert not d.facts["deck-user"] and not d.facts["deck-home"]
    write(tmp_path, "etc/passwd", "deck:x:1000:1000::/home/deck:/bin/bash\n")
    (tmp_path / "home/deck").mkdir(parents=True)
    d = detect(tmp_path, machine="aarch64")
    assert d.facts["deck-user"] and d.facts["deck-home"]


def test_device_steam_deck_and_cix(tmp_path: Path) -> None:
    deck = tmp_path / "deck"
    write(deck, "usr/lib/os-release", "ID=steamos\n")
    write(deck, "sys/class/dmi/id/sys_vendor", "Valve\n")
    write(deck, "sys/class/dmi/id/product_name", "Galileo\n")
    write(deck, "proc/cpuinfo", "processor\t: 0\nvendor_id\t: AuthenticAMD\n")
    drm_card(deck, "card0", "amdgpu")
    write(deck, "usr/bin/pacman", "")
    d = detect(deck, machine="x86_64")
    assert {k for k, v in d.facts.items() if v} == {"x86_64", "steamdeck", "amd-cpu", "amd-gpu",
                                                    "steamos", "pacman"}
    assert d.device_id == "dmi:Valve:Galileo" and d.model == "Galileo"
    assert "Valve:Galileo" in d.ids

    o6n = tmp_path / "o6n"
    write(o6n, "etc/os-release", "ID=kettle\n")
    write(o6n, "sys/firmware/devicetree/base/compatible", "radxa,orion-o6n\0cix,sky1\0")
    drm_card(o6n, "card0", "linlondp")
    drm_card(o6n, "card1", "panthor")
    d = detect(o6n, machine="arm64")
    assert d.arch == "aarch64" and d.facts["mali-gpu"] and not d.facts["adreno-gpu"]
    assert not d.facts["qualcomm-cpu"] and not d.facts["steamos"] and d.cpu_vendor is None
    assert d.gpu_drivers == ["linlondp", "panthor"] and d.model is None

    empty = detect(tmp_path / "nothing", machine="x86_64")
    assert empty.device_id is None and empty.facts["x86_64"] and not any(
        v for k, v in empty.facts.items() if k != "x86_64")


def test_python_module_facts() -> None:
    assert PORTAL.fact("python:json") is True
    assert PORTAL.fact("python:aiohttp") is True  # Remora's own dependency
    assert PORTAL.fact("python:decky") is True  # Remora's compat module
    assert PORTAL.fact("python:no_such_module_here") is False
    assert PORTAL.fact("python:not a name") is None and PORTAL.fact("no-such-fact") is None


# -- lists -------------------------------------------------------------------------------------

def compat_file(path: Path, plugins: dict[str, Any], fmt: str = "remora-compat/1") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": fmt, "updated": "2026-10-18", "plugins": plugins}))
    return path


def test_package_list_is_valid() -> None:
    doc = json.loads(DATA_FILE.read_text())
    assert doc["format"] == "remora-compat/1" and isinstance(doc["plugins"], dict)
    assert CompatList.load([]).files[0]["path"] == str(DATA_FILE)


def test_lists_merge_field_by_field(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    base = compat_file(tmp_path / "base.json", {
        "PowerTools": {"requires": ["x86_64", "amd-cpu"], "upto": "2.0.3", "note": "ryzenadj"},
        "Gone": {"status": "works"},
        "Bad": {"requires": "x86_64", "status": "great", "note": "no facts"},
    })
    compat_file(tmp_path / "d" / "10-distro.json", {
        "PowerTools": {"note": "Distro note.", "tested": ["ayn,odin2portal"]},
        "Gone": None,
    })
    compat_file(tmp_path / "d" / "20-local.json", {"CSS Loader": {"status": "works", "upto": None}})
    (tmp_path / "d" / "30-broken.json").write_text("{not json")
    compat_file(tmp_path / "d" / "40-other.json", {"X": {"status": "works"}}, fmt="something/2")
    lists = CompatList.load([str(tmp_path / "d" / "*.json"), tmp_path / "missing.json"], builtin=base)
    assert lists.entries["PowerTools"] == {"requires": ["x86_64", "amd-cpu"], "upto": "2.0.3",
                                           "note": "Distro note.", "tested": ["ayn,odin2portal"]}
    assert "Gone" not in lists.entries and "X" not in lists.entries
    assert "Bad" not in lists.entries  # its fields were bad, nothing left that decides
    assert lists.entries["CSS Loader"] == {"status": "works", "upto": None}
    assert [Path(f["path"]).name for f in lists.files] == ["base.json", "10-distro.json", "20-local.json"]
    text = caplog.text
    assert "30-broken.json is not valid JSON" in text and "missing.json" in text
    assert "not a remora-compat/1 file" in text and "bad status" in text
    # a folder works like a glob of its *.json
    assert "CSS Loader" in CompatList.load([tmp_path / "d"], builtin=None).entries


def test_evaluate_states(caplog: pytest.LogCaptureFixture) -> None:
    pt = {"requires": ["x86_64", "amd-cpu"], "upto": "2.0.3", "note": "AMD only."}
    assert evaluate(pt, "2.0.3", PORTAL) == {"state": "unsupported", "reasons": ["x86_64", "amd-cpu"],
                                             "note": "AMD only.", "checked": "2.0.3",
                                             "tested_here": False}
    newer = evaluate(pt, "2.1.0", PORTAL)
    assert newer["state"] == "untested" and newer["reasons"] == ["x86_64", "amd-cpu"]
    assert evaluate(pt, "1.0.0", PORTAL)["state"] == "unsupported"
    assert evaluate({"requires": ["aarch64"], "status": "works", "tested": ["qcom,sm8550"]},
                    "9", PORTAL) == {"state": "works", "reasons": [], "note": None,
                                          "checked": None, "tested_here": True}
    assert evaluate({"requires": [], "status": "partial", "note": "BAT1 only"}, "1", PORTAL)["state"] == "partial"
    assert evaluate({"status": "broken"}, "1", PORTAL)["state"] == "broken"
    assert evaluate({"requires": ["flatpak"]}, "1", PORTAL)["state"] == "untested"
    assert evaluate(None, "1", PORTAL) == {"state": "untested", "reasons": [], "note": None, "checked": None}
    # an unknown fact is ignored (and logged once); python:<module> is looked up
    compat_mod._warned.discard("quantum-cpu")
    assert evaluate({"requires": ["quantum-cpu"], "status": "works"}, "1", PORTAL)["state"] == "works"
    assert evaluate({"requires": ["quantum-cpu"], "status": "works"}, "1", PORTAL)["state"] == "works"
    assert caplog.text.count("unknown fact 'quantum-cpu'") == 1
    py = evaluate({"requires": ["python:no_such_module_here", "python:json"]}, "1", PORTAL)
    assert py["state"] == "unsupported" and py["reasons"] == ["python:no_such_module_here"]
    assert "the Python module no_such_module_here" in compat_mod.refusal("Spy", "1", py)


# -- the binary check --------------------------------------------------------------------------

def tree(root: Path, files: dict[str, str | bytes]) -> Path:
    for rel, data in files.items():
        write(root, rel, data)
    return root


def test_elf_headers() -> None:
    assert elf.elf_arch(X86_ELF) == "x86_64" and elf.elf_arch(ARM_ELF) == "aarch64"
    assert elf.elf_arch(b"\x7fELF short") is None and elf.elf_arch(b"MZ" + bytes(62)) is None
    assert elf.runs_on("x86", "x86_64") and not elf.runs_on("x86_64", "aarch64")


def test_used_and_unused_binaries(tmp_path: Path) -> None:
    root = tree(tmp_path / "p", {
        "main.py": "import subprocess\nsubprocess.run([BIN / 'backend'])\n",
        "bin/backend": X86_ELF,
        "bin/leftover-tool": X86_ELF,  # named nowhere
        "py_modules/fast.cpython-311-x86_64-linux-gnu.so": X86_ELF,
        "py_modules/lib/helper.so": X86_ELF,  # in py_modules: imported
        "tools/run.sh": "#!/bin/sh\nexec ./converter \"$@\"\n",
        "tools/converter": X86_ELF,
        "bin/win.exe": b"MZ" + bytes(100),  # PE: runs in Proton, not native code
        "dist/index.js": "backend leftover-tool",  # frontend text doesn't count
    })
    report = elf.check_folder(root)
    used = {n.path for n in report.natives if n.used}
    assert used == {"bin/backend", "py_modules/fast.cpython-311-x86_64-linux-gnu.so",
                    "py_modules/lib/helper.so", "tools/converter"}
    foreign = report.foreign("aarch64")
    assert {n.path for n in foreign} == used
    assert report.foreign("x86_64") == []
    assert elf.describe(foreign).startswith("built for x86_64 only: ")


def test_builds_for_both_cpus_and_python_fallbacks(tmp_path: Path) -> None:
    root = tree(tmp_path / "p", {
        "main.py": "run('bin/x86_64/tool'); run('bin/aarch64/tool')\n",
        "bin/x86_64/tool": X86_ELF,
        "bin/aarch64/tool": ARM_ELF,
        "py_modules/yaml/_speed.cpython-311-x86_64-linux-gnu.so": X86_ELF,
        "py_modules/yaml/_speed.cpython-311-aarch64-linux-gnu.so": ARM_ELF,
        "py_modules/zeroconf/_history.cpython-313-x86_64-linux-gnu.so": X86_ELF,
        "py_modules/zeroconf/_history.py": "# the pure Python version",
    })
    assert elf.check_folder(root).foreign("aarch64") == []


def test_remote_binary_archive_members(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in (("pkg/usr/bin/daemon", X86_ELF), ("pkg/README", b"hello"),
                           ("pkg/arm/daemon", X86_ELF)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("tool/aarch64-tool", ARM_ELF)
    root = tree(tmp_path / "p", {
        "package.json": json.dumps({"remote_binary": [
            {"name": "daemon.tar.gz", "url": "https://x/daemon.tar.gz", "sha256hash": "0"},
            {"name": "arm.zip", "url": "https://x/arm.zip", "sha256hash": "0"},
            {"name": "bare", "url": "https://x/bare", "sha256hash": "0"},
            {"name": "not-there", "url": "https://x/nt", "sha256hash": "0"}]}),
        "main.py": "pass\n",
        "bin/daemon.tar.gz": buf.getvalue(),
        "bin/arm.zip": zbuf.getvalue(),
        "bin/bare": X86_ELF,  # a remote binary is used whatever the code says
    })
    report = elf.check_folder(root)
    assert sorted(n.path for n in report.foreign("aarch64")) == [
        "bin/bare", "bin/daemon.tar.gz:pkg/arm/daemon", "bin/daemon.tar.gz:pkg/usr/bin/daemon"]
    assert [n.path for n in report.natives if n.arch == "aarch64"] == ["bin/arm.zip:tool/aarch64-tool"]
    write(root, "bin/daemon.tar.gz", b"\x1f\x8b broken gzip")
    report = elf.check_folder(root)
    assert report.unchecked == ["bin/daemon.tar.gz"]


# -- the store ---------------------------------------------------------------------------------

@pytest.fixture
async def portal(tmp_path: Path, fake_store: FakeStore):  # noqa: F811
    e = await Env(tmp_path, fake_store).start()
    e.store.compat.device = PORTAL
    e.store.compat.lists = CompatList({
        "PowerTools": {"requires": ["x86_64", "amd-cpu"], "upto": "2.0.3", "note": "AMD only."},
        "Broken Thing": {"status": "broken", "note": "Needs the Deck's EC."},
        "CSS Loader": {"status": "works", "tested": ["ayn,odin2portal"]},
        "KDE Connect": {"requires": ["deck-user"], "upto": "0.1.0-1"},
    })
    yield e
    await e.stop()


async def test_listing_carries_compat(portal: Env, fake_store: FakeStore) -> None:
    for name in ("PowerTools", "Broken Thing", "CSS Loader", "Unknown"):
        fake_store.publish(name, "2.0.3", make_zip(plugin_files(name, "2.0.3")))
    fake_store.publish("PowerTools", "2.1.0", make_zip(plugin_files("PowerTools", "2.1.0")))
    h, _ = fake_store.add_zip(make_zip(plugin_files("Arm Only", "1.0.0")))
    fake_store.index["plugins"] = [{"name": "X86 Index", "versions": [
        {"name": "1.0.0", "hash": h, "artifact": f"../zips/{h}.zip", "arch": ["x86_64"]}]}]
    await portal.ws.routes["remora/store/set_sources"]([
        {"id": "decky", "enabled": False},
        {"id": "local", "name": "Local", "kind": "decky", "url": fake_store.url("/")},
        {"id": "idx", "name": "Idx", "kind": "index", "url": fake_store.url("/idx")}])
    plugins = {p["name"]: p for p in (await portal.ws.routes["remora/store/list"]())["plugins"]}
    states = {n: p["compat"]["state"] for n, p in plugins.items()}
    # PowerTools' newest version is past `upto`: untested, but it says what it may need
    assert states == {"PowerTools": "untested", "Broken Thing": "broken", "CSS Loader": "works",
                      "Unknown": "untested", "X86 Index": "unsupported"}
    pt = plugins["PowerTools"]
    assert pt["compat"]["reasons"] == ["x86_64", "amd-cpu"]
    assert [v["compat"]["state"] for v in pt["versions"]] == ["untested", "unsupported"]
    assert plugins["X86 Index"]["compat"]["reasons"] == ["x86_64"]
    assert plugins["X86 Index"]["versions"][0]["compatible"] is False  # the device's arch, not the host's
    assert plugins["CSS Loader"]["compat"]["tested_here"] is True
    device = await portal.ws.routes["remora/device"]()
    assert device["model"] == "AYN Odin 2 Portal" and device["facts"]["adreno-gpu"]
    assert "compat_lists" in device


async def test_listing_follows_release_order(portal: Env, fake_store: FakeStore) -> None:
    # authors' "-1" is a later release, though semver ranks it below 0.1.0; the card offers
    # the first listed, and the plugin's answer (what hides it) is that version's
    fake_store.publish("KDE Connect", "0.1.0", make_zip(plugin_files("KDE Connect", "0.1.0")))
    fake_store.publish("KDE Connect", "0.1.0-1", make_zip(plugin_files("KDE Connect", "0.1.0-1")))
    await portal.ws.routes["remora/store/set_sources"]([
        {"id": "decky", "enabled": False},
        {"id": "local", "name": "Local", "kind": "decky", "url": fake_store.url("/")}])
    kde = next(p for p in (await portal.ws.routes["remora/store/list"]())["plugins"]
               if p["name"] == "KDE Connect")
    assert [v["name"] for v in kde["versions"]] == ["0.1.0-1", "0.1.0"]
    assert kde["compat"]["state"] == "unsupported" and kde["compat"]["reasons"] == ["deck-user"]


async def test_install_refused_unless_developer(portal: Env, fake_store: FakeStore) -> None:
    h = fake_store.publish("PowerTools", "2.0.3", make_zip(plugin_files("PowerTools", "2.0.3", folder="pt")))
    await portal.install("PowerTools", "2.0.3", h)
    message = portal.failures()[-1][1]
    assert "does not work on this device" in message and "an x86_64 (PC) processor" in message
    assert "AMD only." in message and "PowerTools" not in portal.manager.plugins
    details = portal.ws.named("remora/install_request")[-1][1][0]
    assert details["compat"]["state"] == "unsupported"

    hb = fake_store.publish("Broken Thing", "1.0.0", make_zip(plugin_files("Broken Thing", "1.0.0", folder="bt")))
    await portal.install("Broken Thing", "1.0.0", hb)
    assert "known not to work" in portal.failures()[-1][1]

    portal.settings.set("developer.enabled", True)
    await portal.install("PowerTools", "2.0.3", h)
    assert "PowerTools" in portal.manager.plugins and len(portal.failures()) == 2
    warning = portal.ws.named("remora/install_warning")
    assert warning and warning[-1][0] == "PowerTools" and "x86_64" in warning[-1][1]
    # installed: the plugin list says so too
    listed = {p["name"]: p for p in portal.manager.get_plugins()}
    assert listed["PowerTools"]["compat"]["state"] == "unsupported"
    assert portal.manager.status("PowerTools")["compat"]["state"] == "unsupported"


async def test_binary_check_at_install(portal: Env, fake_store: FakeStore) -> None:
    def plugin(name: str, folder: str, binary: bytes) -> bytes:
        return make_zip(plugin_files(name, "1.0.0", folder=folder, extra={
            f"{folder}/main.py": "import os\nos.system(os.path.dirname(__file__) + '/bin/backend')\n",
            f"{folder}/bin/backend": binary, f"{folder}/bin/unused-x86": X86_ELF}))

    h = fake_store.publish("Native X86", "1.0.0", plugin("Native X86", "nx", X86_ELF))
    await portal.install("Native X86", "1.0.0", h)
    assert "built for x86_64 only: bin/backend" in portal.failures()[-1][1]
    assert not (portal.cfg.plugins_dir / "nx").exists() and portal.leftovers() == []

    h = fake_store.publish("Native Arm", "1.0.0", plugin("Native Arm", "na", ARM_ELF))
    await portal.install("Native Arm", "1.0.0", h)
    assert len(portal.failures()) == 1 and "Native Arm" in portal.manager.plugins
    assert {p["name"]: p for p in portal.manager.get_plugins()}["Native Arm"]["compat"]["state"] == "untested"

    # a remote binary that is a tar.gz with an x86_64 program inside
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("sing-box/sing-box")
        info.size = len(X86_ELF)
        tf.addfile(info, io.BytesIO(X86_ELF))
    bh, burl = fake_store.add_zip(buf.getvalue())
    extra = {"rb/package.json": json.dumps({"version": "1.0.0", "remote_binary": [
        {"name": "sing-box.tar.gz", "url": burl, "sha256hash": bh}]})}
    h = fake_store.publish("Remote", "1.0.0", make_zip(plugin_files("Remote", "1.0.0", folder="rb", extra=extra)))
    await portal.install("Remote", "1.0.0", h)
    assert "bin/sing-box.tar.gz:sing-box/sing-box" in portal.failures()[-1][1]

    portal.settings.set("developer.enabled", True)
    await portal.install("Remote", "1.0.0", h)
    assert "Remote" in portal.manager.plugins
    assert "bin/sing-box.tar.gz:sing-box/sing-box" in portal.ws.named("remora/install_warning")[-1][1]
    status = portal.manager.status("Remote")["compat"]
    assert status["state"] == "unsupported" and status["binaries"] == ["bin/sing-box.tar.gz:sing-box/sing-box"]


async def test_rollback_and_updates_respect_compat(portal: Env, fake_store: FakeStore) -> None:
    h1 = fake_store.publish("PowerTools", "2.0.3", make_zip(plugin_files("PowerTools", "2.0.3", main=True)))
    portal.settings.set("developer.enabled", True)
    await portal.install("PowerTools", "2.0.3", h1)
    h2 = fake_store.publish("PowerTools", "2.1.0", make_zip(plugin_files("PowerTools", "2.1.0", main=True)))
    await portal.install("PowerTools", "2.1.0", h2)
    portal.settings.set("developer.enabled", False)
    with pytest.raises(RouteError) as e:
        await portal.ws.routes["remora/store/rollback"]("PowerTools")
    assert e.value.name == "PermissionError" and "does not work on this device" in e.value.message
    assert portal.manager.plugins["PowerTools"].info.version == "2.1.0"

    # an update to a version the list rules out is not offered
    portal.store.compat.lists.entries["PowerTools"]["upto"] = "9.0.0"
    fake_store.publish("PowerTools", "3.0.0", make_zip(plugin_files("PowerTools", "3.0.0", main=True)))
    portal.store.fetcher.clear()
    assert "PowerTools" not in await portal.ws.routes["remora/store/updates"]()
    portal.settings.set("developer.enabled", True)
    assert (await portal.ws.routes["remora/store/updates"]())["PowerTools"]["version"] == "3.0.0"
