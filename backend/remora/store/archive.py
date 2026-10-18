"""Checking and unpacking plugin zips (compat/store.md §2.3, §6.2 rule 3-4).

A zip must hold exactly one top-level folder, regular files and folders only, with no absolute
paths or `..`. Modes are set here, not taken from the zip: folders 0755, files 0644, and 0755 for
`bin/*` and for files the zip marks executable.
"""

from __future__ import annotations

import os
import stat
import zipfile
from pathlib import Path

MAX_ENTRIES = 20000
MAX_UNPACKED = 1024 * 1024 * 1024  # 1 GiB
CHUNK = 1024 * 1024
_OPEN_NEW = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


class ArchiveError(Exception):
    pass


def _parts(name: str) -> list[str]:
    if "\0" in name:
        raise ArchiveError(f"bad entry name {name!r}")
    clean = name.replace("\\", "/")
    if clean.startswith("/") or (len(clean) > 1 and clean[1] == ":"):
        raise ArchiveError(f"absolute path in the zip: {name!r}")
    parts = [p for p in clean.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ArchiveError(f"path traversal in the zip: {name!r}")
    return parts


def _kind(info: zipfile.ZipInfo) -> int:
    """S_IFDIR / S_IFREG / something else, from the Unix mode when the zip has one."""
    mode = info.external_attr >> 16
    fmt = stat.S_IFMT(mode)
    if fmt:
        return fmt
    return stat.S_IFDIR if info.is_dir() else stat.S_IFREG


def check(zf: zipfile.ZipFile) -> str:
    """Validate the zip and return its single top-level folder name."""
    infos = zf.infolist()
    if not infos:
        raise ArchiveError("the zip is empty")
    if len(infos) > MAX_ENTRIES:
        raise ArchiveError(f"the zip has more than {MAX_ENTRIES} entries")
    tops: set[str] = set()
    total = 0
    has_content = False
    for info in infos:
        parts = _parts(info.filename)
        if not parts:
            continue
        kind = _kind(info)
        if kind == stat.S_IFLNK:
            raise ArchiveError(f"the zip holds a symlink: {info.filename!r}")
        if kind not in (stat.S_IFDIR, stat.S_IFREG):
            raise ArchiveError(f"the zip holds a special file: {info.filename!r}")
        if kind == stat.S_IFREG and len(parts) == 1:
            raise ArchiveError(f"file {info.filename!r} is outside the plugin folder")
        tops.add(parts[0])
        if len(parts) > 1:
            has_content = True
        total += info.file_size
        if total > MAX_UNPACKED:
            raise ArchiveError("the zip unpacks to more than 1 GiB")
    if len(tops) != 1:
        raise ArchiveError("the zip must hold exactly one top-level folder, it has "
                           + (", ".join(sorted(tops)) or "none"))
    top = tops.pop()
    if top.startswith("."):
        raise ArchiveError(f"bad plugin folder name {top!r}")
    if not has_content:
        raise ArchiveError("the plugin folder in the zip is empty")
    return top


def extract(zf: zipfile.ZipFile, dest: Path) -> Path:
    """Unpack into dest (an empty folder we own) and return dest/<top>."""
    top = check(zf)
    written = 0
    root = dest / top
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)
    for info in zf.infolist():
        parts = _parts(info.filename)
        if len(parts) < 2:
            continue
        rel = parts[1:]
        target = root.joinpath(*rel)
        if _kind(info) == stat.S_IFDIR:
            _mkdirs(root, rel)
            continue
        _mkdirs(root, rel[:-1])
        executable = rel[0] == "bin" or bool((info.external_attr >> 16) & 0o111)
        try:
            fd = os.open(target, _OPEN_NEW, 0o600)
        except FileExistsError:
            raise ArchiveError(f"the zip holds {info.filename!r} twice") from None
        with os.fdopen(fd, "wb") as out, zf.open(info) as src:
            while chunk := src.read(CHUNK):
                written += len(chunk)
                if written > MAX_UNPACKED:  # the sizes in the zip headers can lie
                    raise ArchiveError("the zip unpacks to more than 1 GiB")
                out.write(chunk)
            os.fchmod(out.fileno(), 0o755 if executable else 0o644)
    return root


def _mkdirs(root: Path, rel: list[str]) -> None:
    path = root
    for part in rel:
        path = path / part
        try:
            path.mkdir(mode=0o755)
        except FileExistsError:
            if path.is_symlink() or not path.is_dir():
                raise ArchiveError(f"{'/'.join(rel)} is both a file and a folder in the zip") from None
        os.chmod(path, 0o755)
