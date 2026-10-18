"""loader.json (compat/backend.md §7, R6) and the legacy settings.SettingsManager."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

from remora import settings as settings_mod
from remora.settings import Settings, write_json_atomic

from conftest import COMPAT_DIR


def leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.name.startswith("."))


def test_missing_file_is_empty(tmp_path: Path) -> None:
    s = Settings(tmp_path / "settings" / "loader.json")
    assert s.data == {} and s.get("pluginOrder") is None and s.get("x", 5) == 5
    assert not s.path.exists()  # reading doesn't create it


def test_set_writes_atomically(tmp_path: Path) -> None:
    path = tmp_path / "settings" / "loader.json"
    s = Settings(path)
    assert s.set("pluginOrder", ["A", "Ünïcode"]) == ["A", "Ünïcode"]
    text = path.read_text(encoding="utf-8")
    assert json.loads(text) == {"pluginOrder": ["A", "Ünïcode"]}
    assert "Ünïcode" in text and "\n    " in text  # ensure_ascii=False, indent 4, like Decky
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert leftovers(path.parent) == []


def test_unknown_keys_are_kept(tmp_path: Path) -> None:
    path = tmp_path / "loader.json"
    original = {"pluginOrder": ["A"], "developer.rdt.ip": "10.0.0.2", "notificationSettings": {"x": [1]},
                "branch": 0, "user_info.user_name": "deck"}
    path.write_text(json.dumps(original))
    s = Settings(path)
    s.set("hiddenPlugins", ["B"])
    assert json.loads(path.read_text()) == {**original, "hiddenPlugins": ["B"]}
    s.update({"branch": 1})
    assert Settings(path).data == {**original, "hiddenPlugins": ["B"], "branch": 1}


def test_failed_write_leaves_the_old_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "loader.json"
    s = Settings(path)
    s.set("a", 1)

    def broken_dump(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(settings_mod.json, "dump", broken_dump)
    with pytest.raises(OSError):
        s.set("b", 2)
    assert json.loads(path.read_text()) == {"a": 1}
    assert leftovers(tmp_path) == []


def test_unserializable_value_does_not_truncate(tmp_path: Path) -> None:
    path = tmp_path / "loader.json"
    s = Settings(path)
    s.set("a", 1)
    with pytest.raises(TypeError):
        s.set("bad", object())
    assert json.loads(path.read_text()) == {"a": 1}
    assert leftovers(tmp_path) == []


def test_broken_file_is_moved_aside(tmp_path: Path) -> None:
    path = tmp_path / "loader.json"
    path.write_text('{"pluginOrder": ["A"')
    s = Settings(path)
    assert s.data == {}
    assert not path.exists()
    assert (tmp_path / "loader.json.broken").read_text() == '{"pluginOrder": ["A"'
    s.set("x", 1)
    assert json.loads(path.read_text()) == {"x": 1}
    assert (tmp_path / "loader.json.broken").exists()


@pytest.mark.parametrize("content", ["", "   \n", "[1, 2]", "null", '"text"'])
def test_empty_or_non_object_is_empty(tmp_path: Path, content: str) -> None:
    path = tmp_path / "loader.json"
    path.write_text(content)
    assert Settings(path).data == {}


def test_list_helpers(tmp_path: Path) -> None:
    s = Settings(tmp_path / "loader.json")
    s.list_add("disabled_plugins", "A")
    s.list_add("disabled_plugins", "B")
    s.list_add("disabled_plugins", "A")
    assert s.get("disabled_plugins") == ["A", "B"]
    s.list_remove("disabled_plugins", "A")
    s.list_remove("disabled_plugins", "Z")
    assert Settings(s.path).get("disabled_plugins") == ["B"]


def test_write_json_atomic_makes_the_folder(tmp_path: Path) -> None:
    write_json_atomic(tmp_path / "a" / "b" / "c.json", {"k": "v"})
    assert json.loads((tmp_path / "a" / "b" / "c.json").read_text()) == {"k": "v"}


# -- the legacy settings module plugins import (compat/settings.py) ---------------------------

@pytest.fixture
def legacy_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("DECKY_HOME", str(tmp_path / "homebrew"))
    spec = importlib.util.spec_from_file_location("remora_test_compat_settings", COMPAT_DIR / "settings.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_settings_manager_roundtrip(legacy_settings, tmp_path: Path) -> None:
    folder = tmp_path / "plugin-settings"
    sm = legacy_settings.SettingsManager("config", settings_directory=str(folder))
    assert (folder / "config.json").exists()  # created empty, as Decky does
    assert sm.getSetting("missing", "default") == "default"
    assert sm.setSetting("volume", 7) == 7
    assert json.loads((folder / "config.json").read_text()) == {"volume": 7}
    again = legacy_settings.SettingsManager("config", str(folder))
    assert again.getSetting("volume") == 7
    sm.settings["direct"] = True
    sm.commit()
    again.read()
    assert again.settings == {"volume": 7, "direct": True}
    assert leftovers(folder) == []


def test_settings_manager_default_folder(legacy_settings, tmp_path: Path) -> None:
    sm = legacy_settings.SettingsManager("thing")
    assert sm.path == os.path.join(str(tmp_path / "homebrew"), "settings", "thing.json")


def test_settings_manager_broken_file(legacy_settings, tmp_path: Path) -> None:
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "x.json").write_text("{broken")
    sm = legacy_settings.SettingsManager("x", str(tmp_path / "s"))
    assert sm.settings == {}
    sm.setSetting("ok", 1)
    assert json.loads((tmp_path / "s" / "x.json").read_text()) == {"ok": 1}
