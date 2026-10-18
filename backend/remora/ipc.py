"""Framing between the supervisor and a plugin host: one JSON object per line over a socketpair.

supervisor -> host
  {"t": "call", "id": int, "method": str, "args": [...], "kwargs": {...}, "legacy": bool}
  {"t": "stop", "uninstall": bool}
host -> supervisor
  {"t": "ready"}                              the plugin imported and _migration finished
  {"t": "result", "id": int, "ok": true, "value": any}
  {"t": "result", "id": int, "ok": false, "error": {"name", "message", "traceback"}}
  {"t": "event", "event": str, "args": [...]}
  {"t": "fatal", "error": {...}}             the plugin failed to start
  {"t": "stopped"}                            _unload (and _uninstall) are done
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

LINE_LIMIT = 64 * 1024 * 1024


def encode(message: dict[str, Any]) -> bytes:
    return json.dumps(message, ensure_ascii=False).encode() + b"\n"


class Channel:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self._lock = asyncio.Lock()

    @classmethod
    async def from_socket(cls, sock: Any) -> "Channel":
        reader, writer = await asyncio.open_unix_connection(sock=sock, limit=LINE_LIMIT)
        return cls(reader, writer)

    async def send(self, message: dict[str, Any]) -> None:
        await self.send_encoded(encode(message))

    async def send_encoded(self, data: bytes) -> None:
        """Send a message already encoded with encode() (big results are serialized once)."""
        async with self._lock:
            self.writer.write(data)
            await self.writer.drain()

    async def receive(self) -> dict[str, Any] | None:
        """The next message, or None at EOF."""
        while True:
            try:
                line = await self.reader.readuntil(b"\n")
            except asyncio.IncompleteReadError:
                return None
            except asyncio.LimitOverrunError as e:
                # skip a line over the limit instead of spinning on it
                await self.reader.readexactly(e.consumed)
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict):
                return message

    def close(self) -> None:
        try:
            self.writer.close()
        except RuntimeError:
            pass
