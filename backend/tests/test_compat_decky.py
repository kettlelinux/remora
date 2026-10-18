"""The `decky` module (compat/backend.md §5). It reads its constants and sets up the root logger at
import time, so each test imports it in a fresh interpreter."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from conftest import COMPAT_DIR


def run_decky(code: str, env: dict[str, str]) -> Any:
    """Run code with `decky` importable, as in a host; it prints one JSON value."""
    full_env = {"PATH": os.environ.get("PATH", "/usr/bin"), "PYTHONPATH": str(COMPAT_DIR), **env}
    res = subprocess.run([sys.executable, "-P", "-c", textwrap.dedent(code)], env=full_env,
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


@pytest.fixture
def plugin_env(tmp_path: Path) -> dict[str, str]:
    hb = tmp_path / "homebrew"
    env = {
        "HOME": str(tmp_path), "USER": "deck",
        "DECKY_VERSION": "v3.3.0", "DECKY_USER": "deck", "DECKY_USER_HOME": str(tmp_path),
        "DECKY_HOME": str(hb),
        "DECKY_PLUGIN_SETTINGS_DIR": str(hb / "settings" / "p"),
        "DECKY_PLUGIN_RUNTIME_DIR": str(hb / "data" / "p"),
        "DECKY_PLUGIN_LOG_DIR": str(hb / "logs" / "p"),
        "DECKY_PLUGIN_DIR": str(hb / "plugins" / "p"),
        "DECKY_PLUGIN_NAME": "P Plugin", "DECKY_PLUGIN_VERSION": "1.2.3", "DECKY_PLUGIN_AUTHOR": "me",
    }
    for key in ("DECKY_PLUGIN_SETTINGS_DIR", "DECKY_PLUGIN_RUNTIME_DIR", "DECKY_PLUGIN_LOG_DIR"):
        Path(env[key]).mkdir(parents=True)
    return env


CONSTANTS = ["HOME", "USER", "DECKY_VERSION", "DECKY_USER", "DECKY_USER_HOME", "DECKY_HOME",
             "DECKY_PLUGIN_SETTINGS_DIR", "DECKY_PLUGIN_RUNTIME_DIR", "DECKY_PLUGIN_LOG_DIR",
             "DECKY_PLUGIN_DIR", "DECKY_PLUGIN_NAME", "DECKY_PLUGIN_VERSION", "DECKY_PLUGIN_AUTHOR"]


def test_constants_from_env(plugin_env: dict[str, str]) -> None:
    got = run_decky(f"""
        import json, decky, decky_plugin
        out = {{k: getattr(decky, k) for k in {CONSTANTS!r}}}
        out["__version__"] = decky.__version__
        out["same"] = decky_plugin is decky
        out["log"] = decky.DECKY_PLUGIN_LOG
        print(json.dumps(out))
    """, plugin_env)
    for key in CONSTANTS:
        assert got[key] == plugin_env[key], key
    assert got["__version__"] == "1.0.0"
    assert got["same"] is True
    log = Path(got["log"])
    assert log.parent == Path(plugin_env["DECKY_PLUGIN_LOG_DIR"]) and log.suffix == ".log"


def test_missing_env_gives_empty_strings() -> None:
    got = run_decky(f"""
        import json, decky
        print(json.dumps({{k: getattr(decky, k) for k in {CONSTANTS!r}}}))
    """, {})
    assert got["DECKY_PLUGIN_VERSION"] == "" and got["DECKY_PLUGIN_NAME"] == ""


def test_logger_is_the_root_logger_and_writes_the_log(plugin_env: dict[str, str]) -> None:
    got = run_decky("""
        import json, logging, decky
        decky.logger.info("from the plugin")
        logging.getLogger("some.library").warning("from a library")
        logging.getLogger("some.library").debug("not at INFO")
        for h in decky.logger.handlers:
            h.flush()
        print(json.dumps({"root": decky.logger is logging.getLogger(), "level": decky.logger.level,
                          "log": open(decky.DECKY_PLUGIN_LOG).read()}))
    """, plugin_env)
    assert got["root"] is True and got["level"] == 20
    assert "[INFO]: from the plugin" in got["log"]
    assert "[WARNING]: from a library" in got["log"]
    assert "not at INFO" not in got["log"]


def test_log_rotation_keeps_the_newest_four(plugin_env: dict[str, str]) -> None:
    logs = Path(plugin_env["DECKY_PLUGIN_LOG_DIR"])
    names = [f"2026-01-0{d} 10.00.00.log" for d in range(1, 8)]
    for name in names:
        (logs / name).write_text(name)
    (logs / "notes.txt").write_text("not a log")
    got = run_decky("""
        import json, decky, os
        print(json.dumps({"log": os.path.basename(decky.DECKY_PLUGIN_LOG),
                          "files": sorted(os.listdir(decky.DECKY_PLUGIN_LOG_DIR))}))
    """, plugin_env)
    assert got["files"] == sorted(names[-4:] + [got["log"], "notes.txt"])


def test_no_log_dir_still_imports(tmp_path: Path) -> None:
    got = run_decky("""
        import json, decky
        decky.logger.info("to stderr only")
        print(json.dumps("ok"))
    """, {"DECKY_PLUGIN_LOG_DIR": str(tmp_path / "missing")})
    assert got == "ok"
    assert not (tmp_path / "missing").exists()


def test_emit_outside_a_plugin_raises(plugin_env: dict[str, str]) -> None:
    got = run_decky("""
        import asyncio, json, decky
        try:
            asyncio.run(decky.emit("x"))
            print(json.dumps("no error"))
        except RuntimeError as e:
            print(json.dumps(str(e)))
    """, plugin_env)
    assert "only available inside a plugin" in got


def test_migrate(plugin_env: dict[str, str], tmp_path: Path) -> None:
    old = tmp_path / "old"
    (old / "cfgdir" / "nested").mkdir(parents=True)
    (old / "cfgdir" / "a.json").write_text("a")
    (old / "cfgdir" / "nested" / "b.json").write_text("b")
    (old / "single.json").write_text("single")
    (old / "runtime").mkdir()
    (old / "runtime" / "cache.bin").write_text("cache")
    (old / "app.log").write_text("log")
    got = run_decky(f"""
        import json, decky
        print(json.dumps({{
            "settings": decky.migrate_settings({str(old / "cfgdir")!r}, {str(old / "single.json")!r},
                                              {str(old / "missing")!r}),
            "runtime": decky.migrate_runtime({str(old / "runtime")!r}),
            "logs": decky.migrate_logs({str(old / "app.log")!r}),
        }}))
    """, plugin_env)
    settings = Path(plugin_env["DECKY_PLUGIN_SETTINGS_DIR"])
    runtime = Path(plugin_env["DECKY_PLUGIN_RUNTIME_DIR"])
    logs = Path(plugin_env["DECKY_PLUGIN_LOG_DIR"])
    assert got["settings"] == {str(old / "cfgdir"): str(settings), str(old / "single.json"): str(settings / "single.json"),
                               str(old / "missing"): ""}
    assert got["runtime"] == {str(old / "runtime"): str(runtime)}
    assert got["logs"] == {str(old / "app.log"): str(logs / "app.log")}
    # folders: their contents land in the target, and the source goes
    assert (settings / "a.json").read_text() == "a"
    assert (settings / "nested" / "b.json").read_text() == "b"
    assert (settings / "single.json").read_text() == "single"
    assert (runtime / "cache.bin").read_text() == "cache"
    assert (logs / "app.log").read_text() == "log"
    assert sorted(p.name for p in old.iterdir()) == []


def test_migrate_any_makes_the_target_and_keeps_links(plugin_env: dict[str, str], tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (tmp_path / "target-of-link").write_text("t")
    os.symlink(tmp_path / "target-of-link", src / "link")
    target = tmp_path / "new" / "deep"
    got = run_decky(f"""
        import json, decky
        print(json.dumps(decky.migrate_any({str(target)!r}, {str(src)!r})))
    """, plugin_env)
    assert got == {str(src): str(target)}
    assert (target / "link").is_symlink()
    assert (tmp_path / "target-of-link").read_text() == "t"
