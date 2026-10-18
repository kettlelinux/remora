"""Walking user-controlled folders without following links (compat/backend.md W3, W9)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from remora import fsutil
from remora.fsutil import (UnsafePath, makedirs_as_user, open_beneath, open_user_file_beneath,
                           split_relative)


def read_fd(fd: int) -> str:
    with os.fdopen(fd) as f:
        return f.read()


# -- split_relative -------------------------------------------------------------------------

@pytest.mark.parametrize("rel,parts", [
    ("a/b.js", ["a", "b.js"]),
    ("/a//b/./c", ["a", "b", "c"]),
    ("a\\b", ["a", "b"]),
    ("", []),
    ("...", ["..."]),
    ("..a/b..", ["..a", "b.."]),
])
def test_split_relative(rel: str, parts: list[str]) -> None:
    assert split_relative(rel) == parts


@pytest.mark.parametrize("rel", ["..", "../x", "a/../../x", "a\\..\\x", "a/\0b"])
def test_split_relative_refuses(rel: str) -> None:
    with pytest.raises(UnsafePath):
        split_relative(rel)


# -- open_beneath ---------------------------------------------------------------------------

@pytest.fixture
def tree(tmp_path: Path) -> Path:
    base = tmp_path / "base"
    (base / "sub").mkdir(parents=True)
    (base / "sub" / "file.txt").write_text("inside")
    (tmp_path / "outside.txt").write_text("outside")
    (tmp_path / "outdir").mkdir()
    (tmp_path / "outdir" / "o.txt").write_text("outside")
    os.symlink(tmp_path / "outside.txt", base / "link.txt")
    os.symlink(tmp_path / "outdir", base / "linkdir")
    os.symlink("file.txt", base / "sub" / "relative-link.txt")
    return base


def test_open_beneath_reads(tree: Path) -> None:
    assert read_fd(open_beneath(str(tree), ["sub", "file.txt"])) == "inside"


@pytest.mark.parametrize("parts", [["link.txt"], ["linkdir", "o.txt"], ["sub", "relative-link.txt"]])
def test_open_beneath_refuses_links(tree: Path, parts: list[str]) -> None:
    with pytest.raises(OSError):
        os.close(open_beneath(str(tree), parts))


@pytest.mark.parametrize("parts", [["sub"], []])
def test_open_beneath_refuses_directories(tree: Path, parts: list[str]) -> None:
    with pytest.raises(IsADirectoryError):
        os.close(open_beneath(str(tree), parts))


def test_open_beneath_missing(tree: Path) -> None:
    with pytest.raises(FileNotFoundError):
        open_beneath(str(tree), ["sub", "nope.txt"])
    with pytest.raises(NotADirectoryError):
        open_beneath(str(tree), ["sub", "file.txt", "deeper"])


def test_open_beneath_refuses_fifos_without_blocking(tree: Path) -> None:
    # a fifo the user plants in a data folder must not hang the loader on open()
    import threading

    os.mkfifo(tree / "fifo")
    outcome: list[BaseException | int] = []

    def attempt() -> None:
        try:
            outcome.append(open_beneath(str(tree), ["fifo"]))
        except BaseException as e:  # noqa: BLE001
            outcome.append(e)

    thread = threading.Thread(target=attempt, daemon=True)
    thread.start()
    thread.join(2)
    if thread.is_alive():
        os.close(os.open(tree / "fifo", os.O_WRONLY | os.O_NONBLOCK))  # unblock it
        thread.join(2)
        pytest.fail("open_beneath blocked on a fifo")
    assert isinstance(outcome[0], OSError)


def test_open_beneath_resolves_the_base_once(tree: Path, tmp_path: Path) -> None:
    # the base itself may be a link (Kettle links image plugins into plugins/)
    os.symlink(tree, tmp_path / "base-link")
    assert read_fd(open_beneath(str(tmp_path / "base-link"), ["sub", "file.txt"])) == "inside"


def test_open_user_file_beneath(tmp_path: Path) -> None:
    home = tmp_path / "home"
    data = home / "homebrew" / "data" / "plug"
    data.mkdir(parents=True)
    (data / "x.txt").write_text("x")
    assert read_fd(open_user_file_beneath(data, ["x.txt"], home)) == "x"

    # inside home the base folder itself is walked without following links: the user could
    # point ~/homebrew/data/<plugin> anywhere
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "x.txt").write_text("elsewhere")
    os.symlink(tmp_path / "elsewhere", home / "homebrew" / "data" / "linked")
    with pytest.raises(OSError):
        open_user_file_beneath(home / "homebrew" / "data" / "linked", ["x.txt"], home)

    # outside home only the parent is trusted
    other = tmp_path / "other"
    other.mkdir()
    (other / "y.txt").write_text("y")
    assert read_fd(open_user_file_beneath(other, ["y.txt"], home)) == "y"
    os.symlink(other, tmp_path / "other-link")
    with pytest.raises(OSError):
        open_user_file_beneath(tmp_path / "other-link", ["y.txt"], home)


# -- makedirs_as_user -----------------------------------------------------------------------

def test_makedirs_as_user_unprivileged(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("runs the non-root branch")
    target = tmp_path / "home" / "homebrew" / "settings" / "plug"
    makedirs_as_user(target, tmp_path / "home", os.getuid(), os.getgid())
    assert target.is_dir()
    makedirs_as_user(target, tmp_path / "home", os.getuid(), os.getgid())  # exists: fine


@pytest.fixture
def as_root(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int]]:
    """Take the root branch without being root: geteuid says 0 and fchown is recorded."""
    chowned: list[tuple[str, int, int]] = []
    monkeypatch.setattr(fsutil.os, "geteuid", lambda: 0)

    def fake_fchown(fd: int, uid: int, gid: int) -> None:
        chowned.append((os.readlink(f"/proc/self/fd/{fd}"), uid, gid))

    monkeypatch.setattr(fsutil.os, "fchown", fake_fchown)
    return chowned


def test_makedirs_as_user_root_owns_only_what_it_creates(tmp_path: Path, as_root) -> None:
    home = tmp_path / "home"
    (home / "homebrew").mkdir(parents=True)
    target = home / "homebrew" / "data" / "plug"
    makedirs_as_user(target, home, 1000, 1000)
    assert target.is_dir()
    assert as_root == [(str(home / "homebrew" / "data"), 1000, 1000), (str(target), 1000, 1000)]
    as_root.clear()
    makedirs_as_user(target, home, 1000, 1000)
    assert as_root == []  # nothing that exists is changed


@pytest.mark.parametrize("link_at", ["homebrew", "homebrew/data", "homebrew/data/plug"])
def test_makedirs_as_user_root_refuses_links(tmp_path: Path, as_root, link_at: str) -> None:
    home = tmp_path / "home"
    home.mkdir()
    victim = tmp_path / "etc"
    victim.mkdir()
    link = home / link_at
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(victim, link)
    with pytest.raises(UnsafePath):
        makedirs_as_user(home / "homebrew" / "data" / "plug", home, 1000, 1000)
    assert list(victim.iterdir()) == []
    assert all(not path.startswith(str(victim)) for path, _, _ in as_root)


def test_makedirs_as_user_root_refuses_a_file_in_the_way(tmp_path: Path, as_root) -> None:
    home = tmp_path / "home"
    (home / "homebrew").mkdir(parents=True)
    (home / "homebrew" / "data").write_text("a file")
    with pytest.raises(UnsafePath):
        makedirs_as_user(home / "homebrew" / "data" / "plug", home, 1000, 1000)


def test_makedirs_as_user_root_outside_home(tmp_path: Path, as_root) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "srv").mkdir()
    makedirs_as_user(tmp_path / "srv" / "plug", home, 1000, 1000)
    assert (tmp_path / "srv" / "plug").is_dir()
    assert as_root == [(str(tmp_path / "srv" / "plug"), 1000, 1000)]


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root")
def test_makedirs_as_user_really_chowns(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    makedirs_as_user(home / "a" / "b", home, 1234, 1234)
    assert (home / "a").stat().st_uid == 1234 and (home / "a" / "b").stat().st_gid == 1234
