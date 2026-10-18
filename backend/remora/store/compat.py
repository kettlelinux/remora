"""Plugin compatibility (docs/PROTOCOL.md, "Plugin compatibility"): the `remora-compat/1` lists
that say which store plugins need hardware this device lacks, or were tested on it, and the
`compat` answer the store listing, installs and installed plugins carry.

Lists: the package's data/compat.json, then Config.compat_paths in order (files, globs or
folders of *.json). Later lists override earlier ones per plugin, field by field; `"Name": null`
drops an entry. A missing or broken file is logged and counts as empty.
"""

from __future__ import annotations

import glob
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..device import PYTHON_PREFIX, Device
from . import elf
from .index import is_newer, newest

log = logging.getLogger("remora.store")

FORMAT = "remora-compat/1"
DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "compat.json"
STATUSES = ("works", "partial", "broken")
STATES = ("works", "partial", "untested", "unsupported", "broken")
BLOCKING = ("unsupported", "broken")
MAX_LIST_BYTES = 8 * 1024 * 1024

# for backend messages; the UI has its own (i18n compat.need.*)
FACT_TEXT = {
    "x86_64": "an x86_64 (PC) processor", "aarch64": "an ARM64 processor",
    "steamdeck": "Steam Deck hardware", "amd-cpu": "an AMD processor",
    "intel-cpu": "an Intel processor", "qualcomm-cpu": "a Qualcomm processor",
    "amd-gpu": "an AMD graphics chip", "intel-gpu": "Intel graphics",
    "adreno-gpu": "Adreno graphics", "mali-gpu": "Mali graphics", "flatpak": "Flatpak",
    "steamos": "SteamOS", "pacman": "pacman", "systemd": "systemd",
    "deck-user": "SteamOS's user account 'deck'", "deck-home": "the SteamOS home folder /home/deck",
}
# what a foreign ELF needs, as a fact
ARCH_FACT = {"x86": "x86_64", "x86_64": "x86_64", "arm": "aarch64", "aarch64": "aarch64"}

_warned: set[str] = set()


def fact_text(fact: str) -> str:
    if fact.startswith(PYTHON_PREFIX):
        return f"the Python module {fact[len(PYTHON_PREFIX):]}"
    return FACT_TEXT.get(fact, fact)


def needs_text(reasons: Iterable[str]) -> str:
    return ", ".join(fact_text(r) for r in reasons)


# -- lists -------------------------------------------------------------------------------------

def _str_list(value: Any) -> list[str] | None:
    if isinstance(value, list) and all(isinstance(v, str) and v for v in value):
        return list(value)
    return None


def _clean_entry(where: str, name: str, raw: dict[str, Any]) -> dict[str, Any]:
    """The fields of one entry that are well formed; the rest is logged and dropped."""
    out: dict[str, Any] = {}
    for key, value in raw.items():
        ok = True
        if key == "requires":
            ok = _str_list(value) is not None
        elif key == "status":
            ok = value in STATUSES
        elif key == "tested":
            ok = _str_list(value) is not None
        elif key == "upto":
            ok = value is None or isinstance(value, str)
        elif key == "note":
            ok = value is None or isinstance(value, str)
        else:
            continue  # other fields are for people (and the scanner); kept out of the answers
        if ok:
            out[key] = value
        else:
            log.warning("%s: %r has a bad %s (%r), ignored", where, name, key, value)
    return out


def read_list(path: Path) -> tuple[dict[str, dict[str, Any] | None], str | None] | None:
    """({name: entry or None}, updated) from one list file, or None when it can't be used."""
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_LIST_BYTES + 1)
    except OSError as e:
        log.warning("compat list %s: %s", path, e.strerror or e)
        return None
    if len(data) > MAX_LIST_BYTES:
        log.warning("compat list %s is too large, ignored", path)
        return None
    try:
        doc = json.loads(data)
    except ValueError as e:
        log.warning("compat list %s is not valid JSON (%s), ignored", path, e)
        return None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT \
            or not isinstance(doc.get("plugins"), dict):
        log.warning("compat list %s is not a %s file, ignored", path, FORMAT)
        return None
    entries: dict[str, dict[str, Any] | None] = {}
    for name, raw in doc["plugins"].items():
        if raw is None:
            entries[name] = None
        elif isinstance(raw, dict):
            entries[name] = _clean_entry(str(path), name, raw)
        else:
            log.warning("compat list %s: %r is not an object, ignored", path, name)
    updated = doc.get("updated") if isinstance(doc.get("updated"), str) else None
    return entries, updated


def expand_paths(patterns: Iterable[str | Path]) -> list[Path]:
    out: list[Path] = []
    for pattern in patterns:
        text = str(pattern)
        if any(c in text for c in "*?["):
            out += [Path(p) for p in sorted(glob.glob(text))]
        elif os.path.isdir(text):
            out += [Path(p) for p in sorted(glob.glob(os.path.join(glob.escape(text), "*.json")))]
        else:
            out.append(Path(text))
    return out


@dataclass
class CompatList:
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    # [{path, updated, plugins}] of the files that were read
    files: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, paths: Iterable[str | Path] = (), builtin: Path | None = DATA_FILE) -> "CompatList":
        merged: dict[str, dict[str, Any]] = {}
        files = []
        for path in ([builtin] if builtin is not None else []) + expand_paths(paths):
            read = read_list(path)
            if read is None:
                continue
            entries, updated = read
            for name, entry in entries.items():
                if entry is None:
                    merged.pop(name, None)
                else:
                    merged[name] = {**merged.get(name, {}), **entry}
            files.append({"path": str(path), "updated": updated, "plugins": len(entries)})
        for name in [n for n, e in merged.items() if "requires" not in e and "status" not in e]:
            log.warning("compat lists: %r has neither requires nor status, ignored", name)
            del merged[name]
        log.info("compat lists: %d plugins from %s", len(merged),
                 ", ".join(f["path"] for f in files) or "nothing")
        return cls(merged, files)

    def get(self, name: str) -> dict[str, Any] | None:
        return self.entries.get(name)


# -- answers -----------------------------------------------------------------------------------

def answer(state: str = "untested", reasons: list[str] | None = None, note: str | None = None,
           checked: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"state": state, "reasons": reasons or [], "note": note, "checked": checked, **extra}


def evaluate(entry: dict[str, Any] | None, version: str | None, device: Device) -> dict[str, Any]:
    """The compat answer for one plugin version from its list entry (None: not listed)."""
    if entry is None:
        return answer()
    unmet = []
    for fact in entry.get("requires") or []:
        value = device.fact(fact)
        if value is None:
            if fact not in _warned:
                _warned.add(fact)
                log.warning("compat lists: unknown fact %r, ignored", fact)
            continue
        if not value:
            unmet.append(fact)
    upto = entry.get("upto")
    note = entry.get("note")
    tested_here = bool(set(entry.get("tested") or []) & set(device.ids))
    if upto is not None and version is not None and is_newer(version, upto):
        # checked against an older version only: say what it may need, enforce nothing
        return answer("untested", unmet, note, upto, tested_here=False)
    if unmet:
        state = "unsupported"
    else:
        state = {"broken": "broken", "works": "works", "partial": "partial"}.get(
            entry.get("status") or "", "untested")
    return answer(state, unmet, note, upto, tested_here=tested_here)


def blocked(compat: dict[str, Any] | None) -> bool:
    return bool(compat) and compat.get("state") in BLOCKING


def refusal(name: str, version: str, compat: dict[str, Any]) -> str:
    """Why an install is refused, for the user."""
    if compat.get("state") == "broken":
        why = "it is known not to work on these devices"
    elif compat.get("detail"):
        why = str(compat["detail"])
    else:
        why = f"it needs {needs_text(compat.get('reasons') or [])}"
    note = compat.get("note")
    tail = f" ({note})" if note and note not in why else ""
    return f"{name} {version} does not work on this device: {why}{tail}"


class Compat:
    """The device's facts and the merged lists, as the store and the plugin list use them."""

    def __init__(self, device: Device, lists: CompatList) -> None:
        self.device = device
        self.lists = lists
        self._installed: dict[tuple[Any, ...], elf.Report] = {}

    def check(self, name: str, version: str | None, arch: Any = None) -> dict[str, Any]:
        """One store version. `arch`: an index source's per-version arch list, which wins."""
        if isinstance(arch, list) and arch and "any" not in arch and self.device.arch not in arch:
            return answer("unsupported", [str(a) for a in arch], None, version)
        return evaluate(self.lists.get(name), version, self.device)

    def annotate(self, plugins: list[dict[str, Any]]) -> None:
        """Adds `compat` to each listed plugin (for the version an install picks by default)
        and to each of its versions."""
        for plugin in plugins:
            name = str(plugin.get("name"))
            versions = plugin.get("versions") or []
            for v in versions:
                v["compat"] = self.check(name, v.get("name"), v.get("arch"))
            best = newest(versions) or (versions[0] if versions else None)
            plugin["compat"] = best["compat"] if best else self.check(name, None)

    def folder(self, path: Path) -> elf.Report:
        return elf.check_folder(path)

    def with_binaries(self, compat: dict[str, Any], report: elf.Report) -> dict[str, Any]:
        """The answer once the plugin's files are known: used native code for another CPU
        makes it unsupported, whatever the lists say."""
        foreign = report.foreign(self.device.arch)
        if not foreign:
            return compat
        reasons = list(compat.get("reasons") or [])
        for n in foreign:
            fact = ARCH_FACT.get(n.arch, n.arch)
            if fact not in reasons:
                reasons.append(fact)
        return answer("unsupported", reasons, compat.get("note"), compat.get("checked"),
                      binaries=[n.path for n in foreign], detail=elf.describe(foreign))

    def installed(self, name: str, version: str | None, path: Path) -> dict[str, Any]:
        """An installed plugin: its list entry plus its binaries, cached per folder state."""
        try:
            st = os.stat(path)
            bin_st = os.stat(path / "bin") if os.path.isdir(path / "bin") else None
        except OSError:
            return self.check(name, version)
        key = (str(path), name, version, st.st_ino, st.st_mtime_ns,
               bin_st.st_mtime_ns if bin_st else None)
        cached = self._installed.get(key)
        if cached is None:
            try:
                report = self.folder(path)
            except OSError as e:
                log.info("%s: can't check its binaries: %s", name, e)
                report = elf.Report()
            for old in [k for k in self._installed if k[0] == key[0]]:
                del self._installed[old]
            cached = self._installed[key] = report
        return self.with_binaries(self.check(name, version), cached)
