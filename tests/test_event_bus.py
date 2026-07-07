"""Test untuk core/event_bus.py — Event-Driven Runtime (TODO.md § Prioritas 4).

Event log in-process murni Python asyncio.Queue + callback registry — TANPA
broker eksternal (NATS/Kafka/Postgres LISTEN-NOTIFY dicatat sebagai jalur
upgrade OPSIONAL di TODO.md, bukan dikerjakan sekarang). Agent jadi
producer/consumer event, bukan saling panggil langsung.
"""

import asyncio

import pytest

from core.event_bus import EventBus


@pytest.mark.asyncio
async def test_publish_without_subscriber_does_not_error():
    bus = EventBus()
    await bus.publish("topic.x", {"data": 1})  # tidak ada subscriber, tidak crash


@pytest.mark.asyncio
async def test_subscriber_receives_published_event():
    bus = EventBus()
    received = []

    async def handler(payload):
        received.append(payload)

    bus.subscribe("topic.x", handler)
    await bus.publish("topic.x", {"data": 1})

    assert received == [{"data": 1}]


@pytest.mark.asyncio
async def test_multiple_subscribers_same_topic_all_receive():
    bus = EventBus()
    received_a, received_b = [], []

    async def handler_a(payload):
        received_a.append(payload)

    async def handler_b(payload):
        received_b.append(payload)

    bus.subscribe("topic.x", handler_a)
    bus.subscribe("topic.x", handler_b)
    await bus.publish("topic.x", "hello")

    assert received_a == ["hello"]
    assert received_b == ["hello"]


@pytest.mark.asyncio
async def test_subscriber_on_different_topic_does_not_receive():
    bus = EventBus()
    received = []

    async def handler(payload):
        received.append(payload)

    bus.subscribe("topic.a", handler)
    await bus.publish("topic.b", "should not arrive")

    assert received == []


@pytest.mark.asyncio
async def test_handler_exception_does_not_crash_publish():
    """Fail-safe (§1.3): satu handler yang melempar exception tidak boleh
    mencegah handler lain menerima event, atau membuat publish() gagal."""
    bus = EventBus()
    received = []

    async def bad_handler(payload):
        raise ValueError("boom")

    async def good_handler(payload):
        received.append(payload)

    bus.subscribe("topic.x", bad_handler)
    bus.subscribe("topic.x", good_handler)
    await bus.publish("topic.x", "ok")  # tidak boleh raise

    assert received == ["ok"]


@pytest.mark.asyncio
async def test_events_queue_preserves_order():
    """EventBus.events (asyncio.Queue) menyimpan semua event terpublikasi
    secara berurutan — dipakai untuk replay/audit trail, terpisah dari
    mekanisme subscribe/callback langsung."""
    bus = EventBus()
    await bus.publish("topic.a", 1)
    await bus.publish("topic.b", 2)
    await bus.publish("topic.a", 3)

    collected = []
    while not bus.events.empty():
        collected.append(await bus.events.get())

    assert [e.topic for e in collected] == ["topic.a", "topic.b", "topic.a"]
    assert [e.payload for e in collected] == [1, 2, 3]


@pytest.mark.asyncio
async def test_unsubscribe_stops_receiving():
    bus = EventBus()
    received = []

    async def handler(payload):
        received.append(payload)

    bus.subscribe("topic.x", handler)
    await bus.publish("topic.x", "first")
    bus.unsubscribe("topic.x", handler)
    await bus.publish("topic.x", "second")

    assert received == ["first"]


@pytest.mark.asyncio
async def test_concurrent_publishes_all_handled():
    """Sanity check concurrency: beberapa publish() paralel, semua tetap
    diterima subscriber tanpa kehilangan event (asyncio.Queue thread-safe
    dalam satu event loop)."""
    bus = EventBus()
    received = []

    async def handler(payload):
        received.append(payload)

    bus.subscribe("topic.x", handler)
    await asyncio.gather(*(bus.publish("topic.x", i) for i in range(20)))

    assert sorted(received) == list(range(20))
