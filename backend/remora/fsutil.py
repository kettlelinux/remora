"""File access in directories the user controls, without following links they could plant.

The loader runs as root but reads and creates things under the user's home (~/homebrew). Every
walk below a trusted base opens one component at a time with O_NOFOLLOW, so a symlink the user
put there can't point root at /etc (compat/backend.md W3, W9).
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class UnsafePath(OSError):
    pass


def split_relative(rel: str) -> list[str]:
    """Split a URL or relative path into components, refusing anything that could leave the base."""
    parts = []
    for part in rel.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == ".." or "\0" in part:
            raise UnsafePath(f"refused path {rel!r}")
        parts.append(part)
    return parts


def _trusted_base_and_rest(path: Path, user_home: Path) -> tuple[str, list[str]]:
    """Split path into a base resolved with realpath and the components to walk without following.

    The user's home directory is trusted: its parent belongs to root, so the user can't swap the
    home directory itself, only what is inside it. A path outside home trusts only its parent.
    """
    path = Path(os.path.abspath(path))
    home = Path(os.path.abspath(user_home))
    try:
        rest = path.relative_to(home)
        return os.path.realpath(home), list(rest.parts)
    except ValueError:
        return os.path.realpath(path.parent), [path.name]


def makedirs_as_user(path: Path, user_home: Path, uid: int, gid: int) -> None:
    """mkdir -p, owning the directories it creates to uid:gid. Nothing that exists is changed."""
    if os.geteuid() != 0:
        os.makedirs(path, exist_ok=True)
        return
    base, parts = _trusted_base_and_rest(path, user_home)
    fd = os.open(base, _DIR)
    try:
        for part in parts:
            created = False
            try:
                os.mkdir(part, 0o755, dir_fd=fd)
                created = True
            except FileExistsError:
                pass
            try:
                nfd = os.open(part, _DIR, dir_fd=fd)
            except OSError as e:
                raise UnsafePath(f"{path}: {part} is not a plain directory ({e.strerror})") from e
            os.close(fd)
            fd = nfd
            if created:
                os.fchown(fd, uid, gid)
    finally:
        os.close(fd)


def open_beneath(base: str, rel_parts: list[str]) -> int:
    """Open a regular file below base (resolved once), following no link on the way. Returns a fd."""
    fd = os.open(os.path.realpath(base), _DIR)
    try:
        for part in rel_parts[:-1]:
            nfd = os.open(part, _DIR, dir_fd=fd)
            os.close(fd)
            fd = nfd
        if not rel_parts:
            raise IsADirectoryError(base)
        # O_NONBLOCK: a fifo planted there would block open() (and the loader's event loop)
        ffd = os.open(rel_parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                      dir_fd=fd)
    finally:
        os.close(fd)
    st = os.fstat(ffd)
    if not stat.S_ISREG(st.st_mode):
        os.close(ffd)
        raise IsADirectoryError(rel_parts[-1])
    return ffd


def open_user_file_beneath(base: Path, rel_parts: list[str], user_home: Path) -> int:
    """open_beneath for a base inside the user's home, where the base itself could be a link."""
    trusted, parts = _trusted_base_and_rest(base, user_home)
    return open_beneath(trusted, parts + rel_parts)


def list_dir(path: str, include_files: bool = True, include_folders: bool = True,
             include_ext: list[str] | None = None, include_hidden: bool = False,
             order_by: str = "name_asc", filter_for: str | None = None, page: int = 1,
             max_items: int = 1000) -> dict[str, Any]:
    """utilities/filepicker_ls. As root the loader runs this as the Game Mode user, in
    `python -m remora.fsutil` (below), so it lists only what that user may list."""
    real = os.path.realpath(path)
    exts = [str(e).lower().lstrip(".") for e in (include_ext or ["all_files"])]
    all_files = "all_files" in exts
    matcher = None
    if filter_for:
        try:
            rx = re.compile(filter_for)
            matcher = lambda n: rx.search(n) is not None  # noqa: E731
        except re.error:
            matcher = lambda n: filter_for in n  # noqa: E731
    files: list[dict[str, Any]] = []
    with os.scandir(real) as it:
        for entry in it:
            if not include_hidden and entry.name.startswith("."):
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            is_dir = stat.S_ISDIR(st.st_mode)
            if is_dir and not include_folders:
                continue
            if not is_dir:
                if not include_files:
                    continue
                if not all_files and entry.name.rsplit(".", 1)[-1].lower() not in exts:
                    continue
            if matcher is not None and not matcher(entry.name):
                continue
            files.append({"isdir": is_dir, "name": entry.name,
                          "realpath": os.path.realpath(entry.path), "size": st.st_size,
                          "modified": st.st_mtime, "created": getattr(st, "st_birthtime", st.st_ctime)})
    field, _, direction = (order_by or "name_asc").partition("_")
    key = {"name": lambda f: f["name"].lower(), "modified": lambda f: f["modified"],
           "created": lambda f: f["created"], "size": lambda f: f["size"]}.get(field,
                                                                               lambda f: f["name"].lower())
    files.sort(key=key, reverse=direction == "desc")
    files.sort(key=lambda f: not f["isdir"])  # folders first, stable
    page = max(1, int(page or 1))
    max_items = max(1, int(max_items or 1000))
    start = (page - 1) * max_items
    return {"realpath": real, "files": files[start:start + max_items], "total": len(files)}


def _main() -> None:
    """The file picker's worker: JSON arguments of list_dir on stdin, the answer on stdout."""
    try:
        args = json.loads(sys.stdin.read())
        value = list_dir(**args)
        answer: dict[str, Any] = {"ok": True, "value": value}
    except Exception as e:
        answer = {"ok": False, "name": type(e).__name__, "message": str(e)}
    sys.stdout.write(json.dumps(answer, ensure_ascii=False))


if __name__ == "__main__":
    _main()
