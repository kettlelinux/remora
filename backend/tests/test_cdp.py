"""The injector's loop survives anything devtools sends, and counts crashes sensibly."""

from __future__ import annotations

import time

import pytest

from remora import cdp as cdp_mod
from remora.cdp import CDP, Injector, Target

UI = Target("1", "SharedJSContext", "https://steamloopback.host/routes/", "ws://x")


async def test_find_target_survives_bad_answers(cfg, monkeypatch) -> None:
    answers = [TypeError("not a list"), AttributeError("x"), ValueError("bad json"), [UI]]

    async def targets():
        item = answers.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def no_sleep(_):
        return None

    injector = Injector(cfg, CDP(cfg), "tok")
    monkeypatch.setattr(injector.cdp, "targets", targets)
    monkeypatch.setattr(cdp_mod.asyncio, "sleep", no_sleep)
    assert await injector._find_target() is UI
    await injector.cdp.close()


async def test_clean_detach_is_not_a_crash_and_long_runs_reset(cfg, monkeypatch) -> None:
    injector = Injector(cfg, CDP(cfg), "tok")
    results = []

    async def find():
        return UI

    async def attach(target):
        return results.pop(0)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(injector, "_find_target", find)
    monkeypatch.setattr(injector, "_attach", attach)
    monkeypatch.setattr(cdp_mod.asyncio, "sleep", no_sleep)
    results[:] = [True, True, True]
    for _ in range(3):
        await injector._run_once()
    assert injector._crashes == []
    results[:] = [False, False]
    for _ in range(2):
        await injector._run_once()
    assert len(injector._crashes) == 2
    injector._crashes = [time.monotonic() - 120] * 4  # old ones are pruned before counting
    results[:] = [False]
    await injector._run_once()
    assert len(injector._crashes) == 1
    await injector.cdp.close()


async def test_targets_must_be_a_list(cfg, monkeypatch) -> None:
    class Res:
        async def json(self, content_type=None):
            return {"not": "a list"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    c = CDP(cfg)
    monkeypatch.setattr(c.session, "get", lambda *a, **kw: Res())
    with pytest.raises(ValueError):
        await c.targets()
    await c.close()
