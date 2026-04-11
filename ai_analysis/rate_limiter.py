"""Concurrency and rate limit management for Claude API calls."""

import asyncio
import logging
import time
from collections import deque

log = logging.getLogger(__name__)


class RateLimiter:
    """Controls concurrency and requests-per-minute for API calls.

    The Anthropic SDK handles 429 retries internally. This module prevents
    flooding the API in the first place.
    """

    def __init__(self, max_concurrent: int = 5, rpm_limit: int = 50):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._rpm_limit = rpm_limit
        self._request_times: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until we can make a request (respecting both concurrency and RPM)."""
        await self._semaphore.acquire()

        async with self._lock:
            now = time.monotonic()
            # Remove timestamps older than 60 seconds
            while self._request_times and self._request_times[0] < now - 60:
                self._request_times.popleft()

            # If at RPM limit, wait until the oldest request expires
            if len(self._request_times) >= self._rpm_limit:
                wait_time = 60 - (now - self._request_times[0])
                if wait_time > 0:
                    log.info(f"RPM limit reached, waiting {wait_time:.1f}s")
                    await asyncio.sleep(wait_time)

            self._request_times.append(time.monotonic())

    def release(self) -> None:
        """Release the concurrency slot."""
        self._semaphore.release()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.release()
