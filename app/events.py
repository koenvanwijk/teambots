"""In-process pub/sub so the web UI gets live updates over SSE."""
import asyncio
from collections import defaultdict

_subs: dict[int, set[asyncio.Queue]] = defaultdict(set)


def subscribe(conv_id: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subs[conv_id].add(q)
    return q


def unsubscribe(conv_id: int, q: asyncio.Queue) -> None:
    _subs[conv_id].discard(q)


def publish(conv_id: int, kind: str, **data) -> None:
    for q in list(_subs[conv_id]):
        try:
            q.put_nowait({"kind": kind, **data})
        except asyncio.QueueFull:
            pass
