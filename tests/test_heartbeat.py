"""Test SSE heartbeat (§ user report: "Server not responding" & diam sebelum selesai).

Koneksi TIDAK putus saat agent diam — hanya tak ada frame terkirim, membuat watchdog
frontend menyangka server mati. _with_heartbeat menyisipkan komentar `: ping` selama
jeda agar client tahu koneksi hidup & me-reset watchdog. Tak ada reconnect karena tak
ada yang perlu disambung ulang — stream tetap terbuka.
"""

import asyncio

import pytest

from web.main import _with_heartbeat


@pytest.mark.asyncio
async def test_heartbeat_fires_during_quiet_gap():
    """Jeda antar-event > interval → minimal satu `: ping`, data tetap utuh & urut."""

    async def slow_source():
        yield 'event: token\ndata: "a"\n\n'
        await asyncio.sleep(0.25)  # > interval → memicu ping
        yield 'event: token\ndata: "b"\n\n'

    frames = [f async for f in _with_heartbeat(slow_source(), interval=0.1)]
    pings = [f for f in frames if f.startswith(": ping")]
    data = [f for f in frames if f.startswith("event:")]
    assert len(pings) >= 1, "heartbeat harus menyala saat jeda melebihi interval"
    assert data == ['event: token\ndata: "a"\n\n', 'event: token\ndata: "b"\n\n']


@pytest.mark.asyncio
async def test_no_heartbeat_when_source_is_fast():
    """Sumber tanpa jeda → tak ada ping (heartbeat tak mengotori stream normal)."""

    async def fast_source():
        for i in range(3):
            yield f'event: token\ndata: "{i}"\n\n'

    frames = [f async for f in _with_heartbeat(fast_source(), interval=1.0)]
    assert not any(f.startswith(": ping") for f in frames)
    assert len(frames) == 3


@pytest.mark.asyncio
async def test_source_exception_propagates():
    """Error dari sumber diteruskan (ditangani caller), tak ditelan heartbeat."""

    async def error_source():
        yield 'event: token\ndata: "x"\n\n'
        raise RuntimeError("boom")

    got = []
    with pytest.raises(RuntimeError, match="boom"):
        async for f in _with_heartbeat(error_source(), interval=1.0):
            got.append(f)
    assert got == ['event: token\ndata: "x"\n\n']


@pytest.mark.asyncio
async def test_empty_source_completes_cleanly():
    """Sumber kosong → generator selesai tanpa ping menggantung."""

    async def empty_source():
        return
        yield  # pragma: no cover — buat ini async generator

    frames = [f async for f in _with_heartbeat(empty_source(), interval=0.05)]
    assert frames == []
