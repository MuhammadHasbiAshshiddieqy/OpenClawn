"""Event-Driven Runtime (TODO.md § Prioritas 4) — event bus in-process murni
Python (`asyncio.Queue` + callback registry), TANPA broker eksternal.

Menjawab "agent jadi producer/consumer event, bukan saling panggil langsung"
(TREND.md) untuk komunikasi antar-role di `core/conversation.py`. Versi ini
SENGAJA single-proses/in-memory — jalur upgrade opsional ke NATS atau
PostgreSQL LISTEN/NOTIFY untuk deployment yang butuh skala lintas-proses
dicatat sebagai evaluasi terpisah di TODO.md, bukan dikerjakan di sini
(prinsip token-first §1.4: jangan bangun kompleksitas yang belum dibutuhkan
pilot nyata).

Extractable (CLAUDE.md §1.6): tanpa dependency ke web/DB/config — modul
generik yang bisa dipakai ulang di luar OpenCLAWN.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from infra.logging import log

Handler = Callable[[Any], Awaitable[None]]


@dataclass
class Event:
    """Satu event yang tersimpan di `EventBus.events` untuk replay/audit —
    terpisah dari mekanisme subscribe/callback langsung di bawah."""

    topic: str
    payload: Any


class EventBus:
    """Publish/subscribe in-process. `publish()` memanggil SEMUA handler yang
    subscribe ke topic itu (fail-safe: satu handler gagal tidak menghentikan
    yang lain atau membuat publish() melempar), LALU menaruh event ke
    `self.events` (asyncio.Queue) untuk siapa pun yang mau replay/audit
    seluruh riwayat event tanpa perlu subscribe di awal.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Handler]] = {}
        self.events: asyncio.Queue[Event] = asyncio.Queue()

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._subscribers.setdefault(topic, []).append(handler)

    def unsubscribe(self, topic: str, handler: Handler) -> None:
        handlers = self._subscribers.get(topic)
        if handlers and handler in handlers:
            handlers.remove(handler)

    async def publish(self, topic: str, payload: Any) -> None:
        for handler in list(self._subscribers.get(topic, [])):
            try:
                await handler(payload)
            except Exception as exc:  # noqa: BLE001 — satu handler gagal, jangan jatuhkan yang lain
                log.error("event_bus_handler_failed", topic=topic, error=str(exc))
        await self.events.put(Event(topic=topic, payload=payload))
