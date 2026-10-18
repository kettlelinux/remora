#!/usr/bin/env python3
"""Scan a Decky-format store for plugins that can't run on an ARM device, and write what it finds
as a remora-compat/1 list (docs/PROTOCOL.md, "Plugin compatibility").

For each plugin's newest version it reads the zip (downloaded by hash and verified, or taken
from --zips) in memory and records:
  - used native code built for x86 only        -> requires "x86_64"
  - remote_binary URLs that name x86 only       -> requires "x86_64" (Windows .exe/.dll don't
    count: they run inside Proton; URLs with no arch are left to Remora's install-time check)
  - Python imports nothing provides             -> requires "python:<module>"
The used-binary rule is Remora's own install-time check (backend/remora/store/elf.py, loaded
from this repo). Nothing from a zip is extracted to disk, imported or run: Python files are only
parsed with ast.

Entries get `upto` = the scanned version and a generated note; merging into an existing list
(--merge, default the output file) keeps hand-written fields, and a `scan` record per entry
lets the next run replace what the scanner itself wrote.

usage: tools/compat-scan.py -o compat.json [--zips DIR] [--offline] [--plugins plugins.json]
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures
import hashlib
import importlib.util
import io
import json
import re
import sys
import urllib.request
import warnings
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
STORE = "https://plugins.deckbrew.xyz/plugins"
CDN = "https://cdn.tzatzikiweeb.moe/file/steam-deck-homebrew/versions/{hash}.zip"
FORMAT = "remora-compat/1"
MAX_ZIP = 256 * 1024 * 1024
MAX_PY = 4 * 1024 * 1024
X86_URL = re.compile(r"x86[_-]64|amd64|x64(?![0-9])|linux64|i[3-6]86", re.I)
ARM_URL = re.compile(r"aarch64|arm64|armv\d|armhf", re.I)
# Windows programs (Proton runs them) and Windows builds of anything
PE_URL = re.compile(r"\.(?:exe|dll|msi)(?:$|[?#])|windows|win(?:32|64)", re.I)
X86 = ("x86", "x86_64")
# importable in every plugin host besides the stdlib: the packages Remora ships for plugins
# (packaging/requirements.txt), and what its packages depend on
PROVIDED_EXTRA = {"attr", "attrs", "cryptography", "_cffi_backend", "__future__"}
# import names of distributions whose import name isn't the distribution name
DIST_IMPORTS = {"typing-extensions": ["typing_extensions"], "async-timeout": ["async_timeout"]}


def load_elf() -> Any:
    path = REPO / "backend" / "remora" / "store" / "elf.py"
    spec = importlib.util.spec_from_file_location("remora_elf", path)
    if spec is None or spec.loader is None:
        sys.exit(f"compat-scan: can't load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["remora_elf"] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


elf = load_elf()


def provided_modules() -> set[str]:
    """Top-level modules a plugin can import without shipping them."""
    names = set(sys.stdlib_module_names) | PROVIDED_EXTRA
    names |= {p.stem for p in (REPO / "backend" / "remora" / "compat").glob("*.py")}
    reqs = REPO / "packaging" / "requirements.txt"
    try:
        lines = reqs.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==", line)
        if m:
            dist = m.group(1).lower()
            names |= set(DIST_IMPORTS.get(dist, [dist.replace("-", "_")]))
    return names


# -- Python imports ------------------------------------------------------------------------------

_GUARDS = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}


def _guarded(handlers: list[ast.ExceptHandler]) -> bool:
    for h in handlers:
        if h.type is None:
            return True
        types = h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]
        for t in types:
            name = t.id if isinstance(t, ast.Name) else t.attr if isinstance(t, ast.Attribute) else None
            if name in _GUARDS:
                return True
    return False


# an import: (level, module or "", names imported from it); level 0 is absolute
Imp = tuple[int, str, tuple[str, ...]]


def imports(source: str, strict: bool = False) -> list[Imp]:
    """The imports a module needs: not inside a try that catches ImportError, not under
    `if TYPE_CHECKING`. `strict` (bundled library code, where optional and Python 2 imports
    hide in functions and version checks): module-level imports only, none under an `if`."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # old escape sequences in vendored code
            tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    found: list[Imp] = []

    def walk(node: ast.AST, guarded: bool) -> None:
        if isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
            inner = guarded or _guarded(node.handlers)  # type: ignore[attr-defined]
            for child in node.body:  # type: ignore[attr-defined]
                walk(child, inner)
            for part in (node.handlers, node.orelse, node.finalbody):  # type: ignore[attr-defined]
                for child in part:
                    walk(child, guarded)
            return
        if strict and isinstance(node, (ast.If, ast.FunctionDef, ast.AsyncFunctionDef,
                                        ast.ClassDef, ast.With, ast.Match)):
            return
        if isinstance(node, ast.If):
            test = node.test
            name = test.id if isinstance(test, ast.Name) else test.attr if isinstance(test, ast.Attribute) else ""
            main = isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) \
                and test.left.id == "__name__"  # `if __name__ == "__main__":` never runs in a host
            if name == "TYPE_CHECKING" or main:
                for child in node.orelse:
                    walk(child, guarded)
                return
        if not guarded:
            if isinstance(node, ast.Import):
                found.extend((0, a.name, ()) for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                found.append((node.level, node.module or "", tuple(a.name for a in node.names)))
        for child in ast.iter_child_nodes(node):
            walk(child, guarded)

    walk(tree, False)
    return found


class Modules:
    """The zip's .py files as modules. Plugin hosts add py_modules/ to sys.path and run main.py
    from the plugin folder; vendored trees elsewhere are found by their path suffix (plugins
    add their own folders to sys.path), so `a.b` is any `.../a/b.py` or `.../a/b/__init__.py`."""

    def __init__(self, sources: dict[str, str]) -> None:
        self.sources = sources
        self.by_suffix: dict[str, str] = {}
        for rel in sorted(sources, key=lambda r: r.count("/")):  # shallowest wins
            parts = rel[:-3].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            for i in range(len(parts)):
                self.by_suffix.setdefault(".".join(parts[i:]), rel)

    def find(self, dotted: str) -> list[str]:
        """Files an absolute import runs (its packages' __init__ too); [] when not bundled."""
        parts = dotted.split(".")
        out = []
        for i in range(1, len(parts) + 1):
            rel = self.by_suffix.get(".".join(parts[:i]))
            if rel is None:
                break
            out.append(rel)
        return out

    def relative(self, rel: str, level: int, module: str, names: tuple[str, ...]) -> list[str]:
        base = rel.split("/")[:-1]
        if level > 1:
            base = base[:-(level - 1)] if level - 1 <= len(base) else []
        target = base + [p for p in module.split(".") if p]
        out = []
        for cand in ["/".join(target) + ".py", "/".join(target + ["__init__.py"])]:
            if cand.lstrip("/") in self.sources:
                out.append(cand.lstrip("/"))
        for n in names:
            for cand in ["/".join(target + [n]) + ".py", "/".join(target + [n, "__init__.py"])]:
                if cand.lstrip("/") in self.sources:
                    out.append(cand.lstrip("/"))
        return out


def missing_imports(sources: dict[str, str], provided: set[str],
                    bundled: set[str]) -> dict[str, str]:
    """{module: file that imports it} for what main.py needs, following the bundled modules it
    imports, that neither the plugin nor the host provides."""
    mods = Modules(sources)
    if "main.py" not in sources:
        return {}
    todo, seen, missing = ["main.py"], {"main.py"}, {}
    while todo:
        rel = todo.pop()
        # the plugin's own code (main.py and files next to it) counts in full; anything in a
        # folder is library code, read strictly
        for level, module, names in imports(sources[rel], strict="/" in rel):
            if level:
                files = mods.relative(rel, level, module, names)
            else:
                files = mods.find(module)
                for n in names:
                    files += [f for f in mods.find(f"{module}.{n}") if f not in files]
                top = module.split(".")[0]
                if not files and top not in provided and top not in bundled:
                    missing.setdefault(top, rel)
            for f in files:
                if f not in seen:
                    seen.add(f)
                    todo.append(f)
    return missing


def bundled_modules(names: list[str]) -> set[str]:
    """Every module name the zip could provide itself: any folder and any .py/.so file stem,
    wherever it is (py_modules/, lib/, python/, defaults/, the root, a vendored tree)."""
    out = set()
    for name in names:
        parts = [p for p in name.split("/") if p]
        out.update(p for p in parts[:-1])
        if parts:
            leaf = parts[-1]
            if leaf.endswith((".py", ".so", ".pyc", ".pyd")) or ".cpython-" in leaf:
                out.add(leaf.split(".")[0])
    return out


# -- one zip -------------------------------------------------------------------------------------

def scan_zip(data: bytes, arch: str, provided: set[str]) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        report = elf.scan(elf.zip_entries(zf))
        names = [i.filename.replace("\\", "/").split("/", 1)[-1] for i in zf.infolist() if not i.is_dir()]
        package: dict[str, Any] = {}
        sources: dict[str, str] = {}
        for info in zf.infolist():
            rel = info.filename.replace("\\", "/").split("/", 1)[-1]
            if info.is_dir() or info.file_size > MAX_PY:
                continue
            if rel == "package.json":
                try:
                    package = json.loads(zf.read(info))
                except ValueError:
                    package = {}
            elif rel.endswith(".py") and "node_modules/" not in rel:
                sources[rel] = zf.read(info).decode("utf-8", errors="replace")
    foreign = report.foreign(arch)
    x86 = [n for n in foreign if n.arch in X86]
    other = [n for n in foreign if n.arch not in X86]
    remote = package.get("remote_binary") if isinstance(package, dict) else None
    urls = [str(b.get("url") or "") + " " + str(b.get("name") or "")
            for b in (remote if isinstance(remote, list) else []) if isinstance(b, dict)]
    native_urls = [u for u in urls if not PE_URL.search(u.split(" ")[0])]
    x86_urls = [u.split(" ")[0] for u in native_urls if X86_URL.search(u) and not ARM_URL.search(u)]
    if any(ARM_URL.search(u) for u in native_urls):
        x86_urls = []  # one of several builds; the plugin picks
    needed = missing_imports(sources, provided, bundled_modules(names))
    if urls:
        # remote binaries may bring Python packages (some plugins download wheels or a
        # py_modules tree): what is missing can't be told from the zip
        needed, unsure = {}, needed
    else:
        unsure = {}
    missing = sorted(needed)
    return {"x86": [n.path for n in x86], "other": [f"{n.path} [{n.arch}]" for n in other],
            "x86_downloads": x86_urls, "imports": missing,
            "import_files": {m: needed[m] for m in missing}, "imports_maybe_downloaded": unsure,
            "unused_native": [f"{n.path} [{n.arch}]" for n in report.natives
                              if not n.used and not elf.runs_on(n.arch, arch)]}


def note_for(found: dict[str, Any]) -> str:
    parts = []
    if found["x86"]:
        paths = found["x86"]
        more = f" and {len(paths) - 3} more" if len(paths) > 3 else ""
        parts.append(f"Ships native code built for x86_64 only: {', '.join(paths[:3])}{more}.")
    if found["x86_downloads"]:
        parts.append("Downloads x86_64-only programs: "
                     + ", ".join(u.rsplit("/", 1)[-1] for u in found["x86_downloads"][:3]) + ".")
    if found["imports"]:
        parts.append("Imports Python modules it doesn't bundle: " + ", ".join(found["imports"]) + ".")
    return " ".join(parts)


def requires_for(found: dict[str, Any]) -> list[str]:
    req = ["x86_64"] if found["x86"] or found["x86_downloads"] else []
    return req + [f"python:{m}" for m in found["imports"]]


# -- fetching ------------------------------------------------------------------------------------

def http_get(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "remora-compat-scan",
                                                   "X-Decky-Version": "v3.3.0"})
    with urllib.request.urlopen(request, timeout=60) as res:
        data = res.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{url} is larger than {limit} bytes")
    return data


def get_zip(plugin: dict[str, Any], version: dict[str, Any], zips: Path | None,
            offline: bool) -> tuple[bytes | None, str | None]:
    want = str(version.get("hash", "")).lower()
    if zips is not None:
        for name in (f"{want}.zip", f"{plugin.get('id')}.zip"):
            path = zips / name
            if path.is_file():
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() == want:
                    return data, None
    if offline:
        return None, "no verified zip in --zips"
    try:
        data = http_get(CDN.format(hash=want), MAX_ZIP)
    except Exception as e:
        return None, f"download failed: {e}"
    if hashlib.sha256(data).hexdigest() != want:
        return None, "the download does not match its hash"
    if zips is not None:
        (zips / f"{want}.zip").write_bytes(data)
    return data, None


# -- merging -------------------------------------------------------------------------------------

def merge(doc: dict[str, Any], name: str, version: str, found: dict[str, Any],
          conflicts: list[str]) -> None:
    plugins = doc["plugins"]
    old = plugins.get(name)
    requires = requires_for(found)
    if old is None and not requires:
        return
    entry = dict(old or {})
    prev = entry.get("scan") if isinstance(entry.get("scan"), dict) else {}
    if not requires and not prev:
        return  # nothing found, nothing written before: a hand-written entry stays as it is
    hand = [r for r in entry.get("requires") or [] if r not in (prev.get("requires") or [])]
    if requires and entry.get("status") in ("works", "partial") and entry.get("upto") is None:
        conflicts.append(f"{name}: marked {entry['status']} for all versions, the scan says it "
                         f"needs {', '.join(requires)}; left as it is")
        requires = []
    entry["requires"] = hand + [r for r in requires if r not in hand]
    note = note_for(found)
    if "note" not in entry or entry.get("note") == prev.get("note"):
        if note:
            entry["note"] = note
        else:
            entry.pop("note", None)
    if requires and not (old is not None and old.get("upto") is None and "status" in old):
        entry["upto"] = version
    entry["scan"] = {"version": version, "requires": requires, "note": note}
    if not entry["requires"] and "status" not in entry:
        plugins.pop(name, None)  # nothing left that the scanner didn't write itself
        return
    plugins[name] = entry


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-o", "--output", required=True, type=Path)
    ap.add_argument("--merge", type=Path, help="existing list to merge into (default: --output)")
    ap.add_argument("--store", default=STORE, help="Decky-format store list URL")
    ap.add_argument("--plugins", type=Path, help="the store list as a file, instead of --store")
    ap.add_argument("--zips", type=Path, help="zips named <hash>.zip or <store id>.zip; downloads land here too")
    ap.add_argument("--offline", action="store_true", help="never download")
    ap.add_argument("--arch", default="aarch64", help="the CPU of the devices (default aarch64)")
    ap.add_argument("--only", action="append", help="scan only this plugin (repeatable)")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--report", type=Path, help="write per-plugin findings (JSON) here")
    args = ap.parse_args()

    if args.plugins:
        store = json.loads(args.plugins.read_text(encoding="utf-8"))
    elif args.offline:
        sys.exit("compat-scan: --offline needs --plugins")
    else:
        store = json.loads(http_get(args.store, 64 * 1024 * 1024))
    if not isinstance(store, list):
        sys.exit("compat-scan: the store list is not a JSON array")
    plugins = [p for p in store if isinstance(p, dict) and isinstance(p.get("name"), str)
               and isinstance(p.get("versions"), list) and p["versions"]]
    if args.only:
        plugins = [p for p in plugins if p["name"] in args.only]

    base = args.merge or args.output
    doc: dict[str, Any] = {"format": FORMAT, "updated": None, "plugins": {}}
    if base.is_file():
        doc = json.loads(base.read_text(encoding="utf-8"))
        if doc.get("format") != FORMAT or not isinstance(doc.get("plugins"), dict):
            sys.exit(f"compat-scan: {base} is not a {FORMAT} file")
    provided = provided_modules()

    def one(p: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
        version = p["versions"][0]  # Decky lists the newest first
        data, error = get_zip(p, version, args.zips, args.offline)
        if data is None:
            return p, None, error
        try:
            return p, scan_zip(data, args.arch, provided), None
        except (zipfile.BadZipFile, OSError) as e:
            return p, None, f"bad zip: {e}"

    report, conflicts = [], []
    with concurrent.futures.ThreadPoolExecutor(max(1, args.jobs)) as pool:
        results = list(pool.map(one, plugins))
    for p, found, error in sorted(results, key=lambda r: r[0]["name"].lower()):
        version = str(p["versions"][0].get("name"))
        if found is None:
            print(f"skipped {p['name']} {version}: {error}", file=sys.stderr)
            report.append({"name": p["name"], "version": version, "error": error})
            continue
        merge(doc, p["name"], version, found, conflicts)
        report.append({"name": p["name"], "version": version, **found})
        for line in found["other"]:
            print(f"note {p['name']}: native code for another CPU: {line}", file=sys.stderr)
    doc["updated"] = date.today().isoformat()
    doc["plugins"] = dict(sorted(doc["plugins"].items(), key=lambda kv: kv[0].lower()))
    args.output.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.report:
        args.report.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    for c in conflicts:
        print(f"conflict {c}", file=sys.stderr)
    x86 = sum(1 for e in doc["plugins"].values() if "x86_64" in (e.get("requires") or []))
    py = sum(1 for e in doc["plugins"].values() if any(r.startswith("python:") for r in e.get("requires") or []))
    print(f"compat-scan: {len(plugins)} plugins, {sum(1 for r in results if r[1] is None)} skipped; "
          f"{x86} need x86_64, {py} need Python modules; wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
