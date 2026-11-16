"""In-memory per-IP rate limiter for auth endpoints, plus client-IP extraction.

X-Forwarded-For and CF-Connecting-IP are trusted only when the immediate peer
is loopback or private (a local reverse proxy such as Caddy or cloudflared).
A public client cannot spoof those headers to bypass the limit. The original
peer is recorded in request.state.connecting_ip before ProxyHeadersMiddleware
rewrites request.client.
"""
from __future__ import annotations

import ipaddress
import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import Request


class SlidingWindow:
    def __init__(self):
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def allow(self, key: str, limit: int, window_s: float = 60.0) -> bool:
        """Record one attempt. Return True when it is within the limit."""
        if limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            cutoff = now - window_s
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def retry_after(self, key: str, window_s: float = 60.0) -> int:
        with self._lock:
            q = self._hits[key]
            if not q:
                return 1
            return max(1, int(window_s - (time.monotonic() - q[0])) + 1)


AUTH_LIMITER = SlidingWindow()


def _is_trusted_proxy(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return bool(addr.is_loopback or addr.is_private or addr.is_link_local)


def connecting_ip(request: Request) -> str:
    """Immediate TCP peer, before forwarded headers are applied."""
    ip = getattr(request.state, "connecting_ip", None)
    if ip:
        return ip
    return request.client.host if request.client else ""


def client_ip(request: Request) -> str:
    peer = connecting_ip(request)
    if _is_trusted_proxy(peer):
        cf = (request.headers.get("cf-connecting-ip") or "").strip()
        if cf:
            return cf.split(",")[0].strip()
        xff = (request.headers.get("x-forwarded-for") or "").strip()
        if xff:
            return xff.split(",")[0].strip()
    return peer or "unknown"
