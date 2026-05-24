from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic

from meme_mcp.errors import ErrorCode, MemeMCPError


@dataclass
class RateLimiter:
    limit: int
    counts: dict[str, int]

    @classmethod
    def with_limit(cls, limit: int) -> RateLimiter:
        return cls(limit=limit, counts=defaultdict(int))

    def hit(self, key: str) -> None:
        self.counts[key] += 1
        if self.counts[key] > self.limit:
            raise MemeMCPError(ErrorCode.RATE_LIMITED, [{"field": "rate", "reason": "limit"}])


@dataclass
class WindowedRateLimiter:
    limit: int
    window_seconds: float
    clock: Callable[[], float] = monotonic

    def __post_init__(self) -> None:
        self._windows: dict[str, tuple[float, int]] = {}

    def hit(self, key: str) -> None:
        now = self.clock()
        start, count = self._windows.get(key, (now, 0))
        if now - start >= self.window_seconds:
            start, count = now, 0
        count += 1
        self._windows[key] = (start, count)
        if count > self.limit:
            raise MemeMCPError(ErrorCode.RATE_LIMITED, [{"field": "rate", "reason": "limit"}])
