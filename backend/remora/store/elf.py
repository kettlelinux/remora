"""Native code in a plugin: the ELF files it ships, the CPU each is built for, and which of them
its code uses (docs/PROTOCOL.md, "Plugin compatibility").

Nothing is run or unpacked: ELF headers and the plugin's .py/.sh text are read in memory, from a
folder, a zip, or a remote binary that is itself a tar or zip. Windows .exe/.dll files are PE,
not ELF, so they never count (they run inside Proton). Standard library only and no relative
imports: tools/compat-scan.py loads this file by path.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Iterator

# e_machine values (ELF header bytes 18-19, little endian on every CPU here)
MACHINES = {0x03: "x86", 0x3E: "x86_64", 0x28: "arm", 0xB7: "aarch64", 0xF3: "riscv64",
            0x08: "mips", 0x14: "ppc", 0x15: "ppc64", 0x16: "s390x", 0x102: "loongarch64"}
HEAD = 20
MAX_TEXT = 4 * 1024 * 1024  # one .py/.sh file
MAX_TEXT_TOTAL = 64 * 1024 * 1024
MAX_MEMBERS = 20000  # in a remote binary archive
MAX_MEMBER_BYTES = 2 * 1024 * 1024 * 1024  # what a tar member walk may decompress
CODE_SUFFIXES = (".py", ".sh")
# arch names in paths: bin/x86_64/tool and bin/aarch64/tool are two builds of one program
_ARCH_TOKEN = re.compile(r"x86[_-]64|amd64|x64|i[3-6]86|aarch64|arm64|armv\dl?|armhf", re.I)

# (path, read) where read(n) returns up to n bytes from the start; -1 reads all
Entry = tuple[str, Callable[[int], bytes]]


@dataclass
class Native:
    path: str
    arch: str
    used: bool = False


@dataclass
class Report:
    natives: list[Native] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)  # archives that could not be read
    python_files: set[str] = field(default_factory=set)  # every .py path, for fallbacks

    def foreign(self, device_arch: str) -> list[Native]:
        """Used native files that can't run here and have no build for this CPU next to them.
        A Python extension with its .py source beside it is fine: Python skips an extension
        built for another CPU (its file name says so) and imports the .py instead."""
        here = {_arch_free(n.path) for n in self.natives if runs_on(n.arch, device_arch)}
        return [n for n in self.natives if n.used and not runs_on(n.arch, device_arch)
                and _arch_free(n.path) not in here and not self._has_fallback(n.path)]

    def _has_fallback(self, path: str) -> bool:
        base = path.rsplit("/", 1)[-1]
        if ".cpython-" not in base and not base.endswith(".abi3.so"):
            return False
        return f"{path.rsplit('/', 1)[0] + '/' if '/' in path else ''}{base.split('.')[0]}.py" \
            in self.python_files


def elf_arch(head: bytes) -> str | None:
    """The CPU an ELF file is built for, from its first 20 bytes; None when it isn't ELF."""
    if len(head) < HEAD or head[:4] != b"\x7fELF":
        return None
    order = "big" if head[5] == 2 else "little"
    em = int.from_bytes(head[18:20], order)
    return MACHINES.get(em, f"e_machine {em:#x}")


def runs_on(arch: str, device_arch: str) -> bool:
    return arch == device_arch or (device_arch == "x86_64" and arch == "x86")


def _arch_free(path: str) -> str:
    return _ARCH_TOKEN.sub("{arch}", path.lower())


def _uses(path: str, text: str) -> bool:
    """Is this native file part of what the plugin runs? Anything in py_modules/ and Python
    extensions are imported; other files only when the code names them."""
    base = path.rsplit("/", 1)[-1]
    if path.startswith("py_modules/") or ".cpython-" in base or base.endswith(".abi3.so"):
        return True
    if base in text:
        return True
    stem = base.split(".")[0]
    return len(stem) > 3 and re.search(r"(?<![\w-])" + re.escape(stem) + r"(?![\w-])", text) is not None


def _is_code(path: str) -> bool:
    return path.endswith(CODE_SUFFIXES) and "node_modules/" not in path


def scan(entries: Iterable[Entry], used: Iterable[str] = (),
         extra: Iterable[Native] = ()) -> Report:
    """ELF files among `entries` (paths relative to the plugin folder) and whether they are used.
    `used`: paths that count as used whatever the code says (remote binaries); `extra`: natives
    found elsewhere (archive members), kept as they are."""
    found: list[Native] = []
    texts: list[str] = []
    python_files = set()
    total = 0
    for path, read in entries:
        if path.endswith(".py"):
            python_files.add(path)
        head = read(HEAD)
        arch = elf_arch(head)
        if arch is not None:
            found.append(Native(path, arch))
        elif _is_code(path) and total < MAX_TEXT_TOTAL:
            data = read(MAX_TEXT)
            total += len(data)
            texts.append(data.decode("utf-8", errors="replace"))
    text = "\n".join(texts)
    always = set(used)
    for n in found:
        n.used = n.path in always or _uses(n.path, text)
    return Report(found + list(extra), python_files=python_files)


# -- sources of entries ------------------------------------------------------------------------

def _open_regular(path: Path) -> BinaryIO | None:
    # O_NOFOLLOW | O_NONBLOCK: never a link target or a FIFO, whatever changed since the walk
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    return os.fdopen(fd, "rb")


def _file_reader(path: Path) -> Callable[[int], bytes]:
    def read(n: int) -> bytes:
        f = _open_regular(path)
        if f is None:
            return b""
        with f:
            return f.read(n)
    return read


def tree_entries(root: Path) -> Iterator[Entry]:
    """Regular files under root, links not followed."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            try:
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    continue
            except OSError:
                continue
            yield path.relative_to(root).as_posix(), _file_reader(path)


def zip_entries(zf: zipfile.ZipFile, strip_top: bool = True) -> Iterator[Entry]:
    """A zip's files, read in memory; with strip_top, paths lose the plugin folder."""
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        if strip_top:
            if "/" not in name:
                continue
            name = name.split("/", 1)[1]

        def read(n: int, info: zipfile.ZipInfo = info) -> bytes:
            try:
                with zf.open(info) as f:
                    return f.read(n)
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError, EOFError):
                return b""
        yield name, read


def archive_natives(f: BinaryIO, label: str) -> tuple[list[Native], bool]:
    """ELF members of a remote binary that is a tar (any compression) or zip, all counted as
    used: ([natives], readable). `f` is the open, seekable file. A plain ELF file is handled by
    scan() as a file of the folder."""
    head = f.read(262)
    f.seek(0)
    out: list[Native] = []
    if head[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(f) as zf:
                for i, (name, member) in enumerate(zip_entries(zf, strip_top=False)):
                    if i >= MAX_MEMBERS:
                        return out, False
                    arch = elf_arch(member(HEAD))
                    if arch is not None:
                        out.append(Native(f"{label}:{name}", arch, True))
            return out, True
        except (zipfile.BadZipFile, OSError):
            return out, False
    is_tar = head[:2] == b"\x1f\x8b" or head[:3] == b"BZh" or head[:6] == b"\xfd7zXZ\x00" \
        or head[257:262] == b"ustar"
    if not is_tar:
        return out, True
    try:
        # streaming ("r|*"): members are read in order, nothing seeks back or lands on disk
        with tarfile.open(fileobj=f, mode="r|*") as tf:
            size = 0
            for i, member in enumerate(tf):
                if i >= MAX_MEMBERS:
                    return out, False
                size += member.size
                if size > MAX_MEMBER_BYTES:
                    return out, False
                if not member.isreg():
                    continue
                data = tf.extractfile(member)
                arch = elf_arch(data.read(HEAD)) if data is not None else None
                if arch is not None:
                    out.append(Native(f"{label}:{member.name.lstrip('./')}", arch, True))
    except Exception:  # tarfile, zlib, bz2 and lzma all have their own errors for a bad file
        return out, False
    return out, True


def remote_binary_names(package_json: bytes) -> list[str]:
    """package.json remote_binary names that are safe file names (bin/<name>)."""
    try:
        package = json.loads(package_json)
    except ValueError:
        return []
    binaries = package.get("remote_binary") if isinstance(package, dict) else None
    names = []
    for entry in binaries if isinstance(binaries, list) else []:
        name = entry.get("name") if isinstance(entry, dict) else None
        if isinstance(name, str) and name and "/" not in name and "\\" not in name \
                and name not in (".", "..") and "\0" not in name:
            names.append(name)
    return names


def check_folder(root: Path) -> Report:
    """A plugin folder after unpacking and after its remote binaries are in bin/."""
    package = _file_reader(root / "package.json")(1024 * 1024)
    remote = remote_binary_names(package)
    extra: list[Native] = []
    unchecked = []
    for name in remote:
        f = _open_regular(root / "bin" / name)
        if f is None:
            continue  # not downloaded (a developer install, or not there yet)
        with f:
            natives, readable = archive_natives(f, f"bin/{name}")
        extra += natives
        if not readable:
            unchecked.append(f"bin/{name}")
    report = scan(tree_entries(root), used=[f"bin/{n}" for n in remote], extra=extra)
    report.unchecked = unchecked
    return report


def describe(foreign: list[Native], limit: int = 3) -> str:
    """"built for x86_64 only: bin/backend, lib/x.so" for an error or a note."""
    archs = sorted({n.arch for n in foreign})
    paths = [n.path for n in foreign[:limit]]
    more = f" and {len(foreign) - limit} more" if len(foreign) > limit else ""
    return f"built for {' and '.join(archs)} only: {', '.join(paths)}{more}"
