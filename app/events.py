"""In-process pub/sub so the web UI gets live updates over SSE."""
import asyncio
from collections import defaultdict

_subs: dict[int, set[asyncio.Queue]] = defaultdict(set)
_bot_subs: dict[int, set[asyncio.Queue]] = defaultdict(set)


def subscribe(conv_id: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subs[conv_id].add(q)
    return q


def unsubscribe(conv_id: int, q: asyncio.Queue) -> None:
    _subs[conv_id].discard(q)


def publish(conv_id: int, kind: str, **data) -> None:
    _put(_subs[conv_id], {"kind": kind, **data})


def subscribe_bot(bot_id: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _bot_subs[bot_id].add(q)
    return q


def unsubscribe_bot(bot_id: int, q: asyncio.Queue) -> None:
    _bot_subs[bot_id].discard(q)


def publish_bot(bot_id: int, kind: str, **data) -> None:
    """Notify anyone looking at this bot's settings (MCP list, etc.)."""
    _put(_bot_subs[bot_id], {"kind": kind, **data})


def _put(qs: set[asyncio.Queue], payload: dict) -> None:
    for q in list(qs):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass
