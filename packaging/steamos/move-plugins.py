#!/usr/bin/python3 -I
"""Move Decky's plugins and loader.json between the user's ~/homebrew and Remora's root-owned
data folder (PRIVILEGED_PATH), as root, without following anything the user could have linked.

  move-plugins.py in  HOME DATA      ~/homebrew/{plugins/*,settings/loader.json} -> DATA
  move-plugins.py out HOME DATA      and back (uninstall.sh, or install.sh rolling back)

"in": each entry of ~/homebrew/plugins is renamed into a root-only staging folder first, where
the user can no longer reach it, and only then checked: real folders become root's (owner 0:0,
no group/other write, no set-id bits; each folder is made root's before its contents are
listed) and go to DATA/plugins. Links and other files stay in ~/homebrew/plugins, which Remora no
longer reads. Plugins Decky ran are trusted as they are now; what the user adds later isn't.
Paths in the user's home are opened by fd, component by component, with O_NOFOLLOW.
Both folders must be on one filesystem (rename); an entry that can't move stays where it was.
"""

from __future__ import annotations

import os
import stat
import sys

D = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
MAX_SETTINGS = 4 << 20
moved = failed = 0


def say(msg: str) -> None:
    print(f"remora: {msg}", file=sys.stderr)


def opendir(path: str) -> int:
    fd = os.open("/", D)
    for part in [p for p in path.split("/") if p]:
        try:
            nfd = os.open(part, D, dir_fd=fd)
        finally:
            os.close(fd)
        fd = nfd
    return fd


def subdir(parent: int, name: str, create_uid: int | None = None) -> int | None:
    try:
        return os.open(name, D, dir_fd=parent)
    except FileNotFoundError:
        if create_uid is None:
            return None
    os.mkdir(name, 0o755, dir_fd=parent)  # mkdirat: never follows a link
    fd = os.open(name, D, dir_fd=parent)
    os.fchown(fd, create_uid, -1)
    return fd


def make_root(fd: int) -> None:
    """fd is a folder in root's staging area: root's, then its entries, depth first."""
    st = os.fstat(fd)
    os.fchown(fd, 0, 0)
    os.fchmod(fd, stat.S_IMODE(st.st_mode) & 0o755)
    for name in os.listdir(fd):
        est = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(est.st_mode):
            child = os.open(name, D, dir_fd=fd)
            try:
                make_root(child)
            finally:
                os.close(child)
        elif stat.S_ISLNK(est.st_mode):
            os.chown(name, 0, 0, dir_fd=fd, follow_symlinks=False)
        else:
            os.chown(name, 0, 0, dir_fd=fd, follow_symlinks=False)
            os.chmod(name, stat.S_IMODE(est.st_mode) & 0o755, dir_fd=fd)


def read_regular(dir_fd: int, name: str) -> bytes | None:
    try:
        f = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(f).st_mode):
            return None
        data = b""
        while len(data) <= MAX_SETTINGS and (chunk := os.read(f, 1 << 20)):
            data += chunk
        return data if len(data) <= MAX_SETTINGS else None
    finally:
        os.close(f)


def write_new(dir_fd: int, name: str, data: bytes, uid: int, gid: int) -> None:
    tmp = f".{name}.remora-new"
    try:
        os.unlink(tmp, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    f = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644,
                dir_fd=dir_fd)
    try:
        os.write(f, data)
        os.fchown(f, uid, gid)
    finally:
        os.close(f)
    os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def move_in(home: str, data: str) -> None:
    global moved, failed
    try:
        hb = opendir(home + "/homebrew")
    except OSError:
        return
    dfd = opendir(data)
    pfd = subdir(dfd, "plugins", 0)
    sfd = subdir(dfd, "settings", 0)
    stage_name = f".migrate-{os.getpid()}"
    os.mkdir(stage_name, 0o700, dir_fd=dfd)
    stage = os.open(stage_name, D, dir_fd=dfd)

    settings = subdir(hb, "settings")
    if settings is not None:
        blob = read_regular(settings, "loader.json")
        try:
            os.stat("loader.json", dir_fd=sfd, follow_symlinks=False)
            have = True
        except FileNotFoundError:
            have = False
        if blob is not None and not have:
            write_new(sfd, "loader.json", blob, 0, 0)
            os.unlink("loader.json", dir_fd=settings)
            say("moved loader.json")

    plugins = subdir(hb, "plugins")
    if plugins is not None:
        for name in os.listdir(plugins):
            try:
                os.rename(name, name, src_dir_fd=plugins, dst_dir_fd=stage)
            except OSError as e:
                say(f"can't move {name}: {e.strerror}")
                failed += 1
                continue
            try:
                st = os.stat(name, dir_fd=stage, follow_symlinks=False)
                if not stat.S_ISDIR(st.st_mode):
                    raise OSError(0, "not a plugin folder")
                fd = os.open(name, D, dir_fd=stage)
                try:
                    make_root(fd)
                finally:
                    os.close(fd)
                try:
                    os.stat(name, dir_fd=pfd, follow_symlinks=False)
                    raise OSError(0, "already in " + data + "/plugins")
                except FileNotFoundError:
                    pass
                os.rename(name, name, src_dir_fd=stage, dst_dir_fd=pfd)
                moved += 1
            except OSError as e:
                if e.errno:
                    failed += 1
                say(f"left {name} in ~/homebrew/plugins: {e.strerror}")
                try:
                    os.rename(name, name, src_dir_fd=stage, dst_dir_fd=plugins)
                except OSError:
                    say(f"{name} is kept in {data}/{stage_name}")
    try:
        os.rmdir(stage_name, dir_fd=dfd)
    except OSError:
        pass


def move_out(home: str, data: str) -> None:
    global moved, failed
    try:
        hb = opendir(home + "/homebrew")
        dfd = opendir(data)
    except OSError:
        return
    st = os.fstat(hb)
    uid, gid = st.st_uid, st.st_gid
    pfd = subdir(dfd, "plugins")
    if pfd is not None:
        out = subdir(hb, "plugins", uid)
        for name in os.listdir(pfd):
            if name.startswith((".staging-", ".old-")):
                continue
            try:
                os.stat(name, dir_fd=out, follow_symlinks=False)
                say(f"~/homebrew/plugins/{name} exists; {name} stays in {data}/plugins")
                failed += 1
                continue
            except FileNotFoundError:
                pass
            try:
                # stays root's, as Decky's store installs are
                os.rename(name, name, src_dir_fd=pfd, dst_dir_fd=out)
                moved += 1
            except OSError as e:
                say(f"can't move {name} back: {e.strerror}")
                failed += 1
    sfd = subdir(dfd, "settings")
    if sfd is not None:
        blob = read_regular(sfd, "loader.json")
        user_settings = subdir(hb, "settings", uid)
        try:
            os.stat("loader.json", dir_fd=user_settings, follow_symlinks=False)
            say("~/homebrew/settings/loader.json exists; Remora's copy stays")
        except FileNotFoundError:
            if blob is not None:
                write_new(user_settings, "loader.json", blob, uid, gid)
                os.unlink("loader.json", dir_fd=sfd)


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] not in ("in", "out"):
        print(__doc__, file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        say("move-plugins.py must run as root")
        return 1
    direction, home, data = sys.argv[1:]
    (move_in if direction == "in" else move_out)(home, data)
    if moved or failed:
        say(f"{'moved' if direction == 'in' else 'moved back'} {moved} plugin folder(s)"
            + (f", {failed} left where they were" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
