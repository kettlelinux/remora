"""File access in directories the user controls, without following links they could plant.

The loader runs as root but reads and creates things under the user's home (~/homebrew). Every
walk below a trusted base opens one component at a time with O_NOFOLLOW, so a symlink the user
put there can't point root at /etc (compat/backend.md W3, W9).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

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
        ffd = os.open(rel_parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
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
