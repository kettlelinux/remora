"""What this device is: CPU, GPU, OS and tools, as the facts compat lists name (docs/PROTOCOL.md,
"Plugin compatibility"). Read once from sysfs/procfs; `root` points the reads elsewhere (tests)."""

from __future__ import annotations

import functools
import importlib.machinery
import logging
import os
import platform
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("remora.device")

# every fact a compat list's `requires` may name; each is always known (true or false)
FACTS = ("x86_64", "aarch64", "steamdeck", "amd-cpu", "intel-cpu", "qualcomm-cpu",
         "amd-gpu", "intel-gpu", "adreno-gpu", "mali-gpu", "flatpak", "steamos", "pacman",
         "systemd", "deck-user", "deck-home")

# /sys/class/drm/card*/device/driver names; a device may have several cards (a display
# controller and a separate GPU, as on CIX boards)
GPU_DRIVERS = {"amdgpu": "amd-gpu", "i915": "intel-gpu", "xe": "intel-gpu",
               "msm": "adreno-gpu", "msm_dpu": "adreno-gpu", "msm_drm": "adreno-gpu",
               "panfrost": "mali-gpu", "panthor": "mali-gpu", "mali": "mali-gpu",
               "mali_kbase": "mali-gpu"}
STEAM_DECKS = ("Jupiter", "Galileo")
# `python:<module>`: importable in plugin hosts; checked on first use (python_module())
PYTHON_PREFIX = "python:"
# plugin hosts put Remora's Decky modules (decky, settings, helpers, ...) first on sys.path
COMPAT_DIR = str(Path(__file__).resolve().parent / "compat")
_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_CARD = re.compile(r"^card\d+$")


def normalize_machine(machine: str) -> str:
    m = machine.strip().lower()
    return {"arm64": "aarch64", "amd64": "x86_64", "x64": "x86_64"}.get(m, m)


@dataclass(frozen=True)
class Device:
    arch: str
    facts: dict[str, bool]
    device_id: str | None = None
    model: str | None = None
    cpu_vendor: str | None = None  # "amd" | "intel" | "qualcomm" | None
    gpu_drivers: list[str] = field(default_factory=list)
    # every devicetree compatible (or the DMI id), for matching a list's `tested`
    ids: list[str] = field(default_factory=list)

    def fact(self, name: str) -> bool | None:
        """A fact's value; None for a name that isn't one."""
        if name in self.facts:
            return self.facts[name]
        if name.startswith(PYTHON_PREFIX):
            return python_module(name[len(PYTHON_PREFIX):])
        return None

    def to_json(self) -> dict[str, Any]:
        return {"arch": self.arch, "facts": dict(self.facts), "device_id": self.device_id,
                "model": self.model, "cpu_vendor": self.cpu_vendor,
                "gpu_drivers": list(self.gpu_drivers)}


def _read(path: Path, limit: int = 1024 * 1024) -> str | None:
    try:
        with open(path, "rb") as f:
            return f.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return None


def _os_release(root: Path) -> dict[str, str]:
    text = _read(root / "etc/os-release")
    if text is None:
        text = _read(root / "usr/lib/os-release") or ""
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() and not key.startswith("#"):
            out[key.strip()] = value.strip().strip("\"'")
    return out


def _devicetree(root: Path) -> tuple[list[str], str | None]:
    for base in ("proc/device-tree", "sys/firmware/devicetree/base"):
        compatible = _read(root / base / "compatible")
        if compatible is not None:
            model = (_read(root / base / "model") or "").strip("\0").strip() or None
            return [c for c in compatible.split("\0") if c], model
    return [], None


def _cpu_vendor_id(root: Path) -> str | None:
    text = _read(root / "proc/cpuinfo") or ""
    m = re.search(r"^vendor_id\s*:\s*(\S+)", text, re.M)
    return m.group(1) if m else None


def _gpu_drivers(root: Path) -> list[str]:
    drm = root / "sys/class/drm"
    try:
        cards = sorted(n for n in os.listdir(drm) if _CARD.match(n))
    except OSError:
        return []
    drivers = []
    for card in cards:
        try:
            target = os.readlink(drm / card / "device" / "driver")
        except OSError:
            continue
        name = os.path.basename(target.rstrip("/"))
        if name and name not in drivers:
            drivers.append(name)
    return drivers


def _has_user(root: Path, name: str) -> bool:
    text = _read(root / "etc/passwd") or ""
    return any(line.split(":", 1)[0] == name for line in text.splitlines())


def detect(root: Path | str = "/", machine: str | None = None) -> Device:
    """The device's facts, read under `root` (normally /)."""
    root = Path(root)
    arch = normalize_machine(machine if machine is not None else platform.machine())
    compatible, dt_model = _devicetree(root)
    vendor = (_read(root / "sys/class/dmi/id/sys_vendor") or "").strip() or None
    product = (_read(root / "sys/class/dmi/id/product_name") or "").strip() or None
    vendor_id = _cpu_vendor_id(root)
    gpus = _gpu_drivers(root)

    cpu_vendor = {"AuthenticAMD": "amd", "GenuineIntel": "intel"}.get(vendor_id or "")
    if cpu_vendor is None and any(c.startswith("qcom,") for c in compatible):
        cpu_vendor = "qualcomm"
    gpu_facts = {GPU_DRIVERS[d] for d in gpus if d in GPU_DRIVERS}
    os_id = _os_release(root).get("ID", "")
    facts = {
        "x86_64": arch == "x86_64",
        "aarch64": arch == "aarch64",
        "steamdeck": (vendor or "").lower() == "valve" and product in STEAM_DECKS,
        "amd-cpu": cpu_vendor == "amd",
        "intel-cpu": cpu_vendor == "intel",
        "qualcomm-cpu": cpu_vendor == "qualcomm",
        "amd-gpu": "amd-gpu" in gpu_facts,
        "intel-gpu": "intel-gpu" in gpu_facts,
        "adreno-gpu": "adreno-gpu" in gpu_facts,
        "mali-gpu": "mali-gpu" in gpu_facts,
        "flatpak": os.path.exists(root / "usr/bin/flatpak"),
        "steamos": os_id == "steamos",
        "pacman": os.path.exists(root / "usr/bin/pacman"),
        "systemd": os.path.isdir(root / "run/systemd/system"),
        # SteamOS's user and home: plugins that hardcode them (sudo -u deck, /home/deck/...)
        "deck-user": _has_user(root, "deck"),
        "deck-home": os.path.isdir(root / "home/deck"),
    }
    if compatible:
        device_id, model, ids = compatible[0], dt_model, compatible
    elif vendor or product:
        device_id = f"dmi:{vendor or ''}:{product or ''}"
        model, ids = product, [device_id, f"{vendor or ''}:{product or ''}"]
    else:
        device_id, model, ids = None, None, []
    return Device(arch=arch, facts=facts, device_id=device_id, model=model, cpu_vendor=cpu_vendor,
                  gpu_drivers=gpus, ids=ids)


@functools.cache
def python_module(module: str) -> bool | None:
    """Can a plugin host import this module (top-level package) without the plugin's own
    py_modules? Hosts run this interpreter with this sys.path, Remora's compat folder first.
    Only finds it, nothing is imported. None for a name that isn't a module name."""
    if not _MODULE.match(module):
        return None
    top = module.split(".")[0]
    if top in sys.builtin_module_names:
        return True
    try:
        return importlib.machinery.PathFinder.find_spec(top, [COMPAT_DIR, *sys.path]) is not None
    except (ImportError, ValueError, OSError):
        return False


@functools.cache
def current(root: str = "/") -> Device:
    """detect(), once per process."""
    device = detect(root)
    log.info("device: %s (%s), %s, facts: %s", device.model or "unknown model",
             device.device_id or "no id", device.arch,
             ", ".join(k for k, v in device.facts.items() if v) or "none")
    return device
