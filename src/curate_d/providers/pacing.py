"""Keeping provider requests within quota, and backing off when told to.

Two separate mechanisms. The pacer spaces requests so a scan stays inside the configured
quota before the provider has to say anything. The backoff handles the provider saying it
anyway: a rate-limit answer is waited out and retried, up to a bound, rather than failing
the scan. Past that bound the limit is treated as persistent and the error propagates, so
the scan is marked incomplete with a reason instead of retrying forever.

DBOS partitions the scan queue by provider, which bounds how many scans run against one
provider at once. That bounds workflows, not the requests each one makes, which is what
the pacer is for. Each provider gets its own pacer, so one provider's quota is never
spent waiting on another's.
"""

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from curate_d.providers.errors import ProviderUnavailableError, RateLimitedError

type Clock = Callable[[], float]
type Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class Quota:
    """At most `requests` requests in any `period_seconds` window."""

    requests: int
    period_seconds: float

    def __post_init__(self) -> None:
        """Reject a quota that would admit nothing or everything.

        Raises:
            ValueError: `requests` or `period_seconds` is not positive.
        """
        if self.requests <= 0 or self.period_seconds <= 0:
            raise ValueError(f"a quota needs a positive count and period, got {self}")


class Pacer:
    """A sliding-window limiter: waits until a request fits in the quota, then admits it."""

    def __init__(
        self, quota: Quota, *, clock: Clock = time.monotonic, sleep: Sleep = asyncio.sleep
    ) -> None:
        """Bind the quota, and the clock and sleep that time is measured and spent with."""
        self._quota = quota
        self._clock = clock
        self._sleep = sleep
        self._admitted: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def admit(self) -> None:
        """Wait until one more request fits inside the quota's window, then record it."""
        async with self._lock:
            while True:
                now = self._clock()
                window_start = now - self._quota.period_seconds
                while self._admitted and self._admitted[0] <= window_start:
                    self._admitted.popleft()
                if len(self._admitted) < self._quota.requests:
                    self._admitted.append(now)
                    return
                await self._sleep(self._admitted[0] - window_start)


@dataclass(frozen=True)
class Backoff:
    """How rate-limit answers are waited out.

    Delays double from `initial_seconds` up to `max_delay_seconds`, and a provider's own
    Retry-After is honoured when it asks for longer. After `max_attempts` rate-limited
    answers in a row the limit is taken to be persistent.
    """

    initial_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    max_attempts: int = 6

    def delay(self, attempt: int, requested: float | None) -> float:
        """Seconds to wait before retry number `attempt` (1-based)."""
        exponential = min(self.initial_seconds * 2.0 ** (attempt - 1), self.max_delay_seconds)
        return max(exponential, requested or 0.0)


async def paced[T](
    pacer: Pacer,
    backoff: Backoff,
    request: Callable[[], Awaitable[T]],
    *,
    sleep: Sleep = asyncio.sleep,
) -> T:
    """Issue `request` inside the quota, retrying it through rate-limit answers.

    Raises:
        RateLimitedError: the provider kept answering with a rate limit through every
            allowed attempt.
        ProviderError: any other failure, unretried: those are for the caller to judge.
    """
    attempt = 0
    while True:
        await pacer.admit()
        try:
            return await request()
        except RateLimitedError as limited:
            attempt += 1
            if attempt >= backoff.max_attempts:
                raise
            await sleep(backoff.delay(attempt, limited.retry_after_seconds))


async def retrying_transient[T](
    request: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    initial_seconds: float = 2.0,
    sleep: Sleep = asyncio.sleep,
) -> T:
    """Retry `request` through transient provider failures, a bounded number of times.

    A rate limit is not retried here: `paced` has already waited it out to its own bound,
    and past that bound it is persistent by definition.

    Raises:
        ProviderUnavailableError: the failure outlasted every attempt.
        ProviderError: any non-transient failure, at once.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await request()
        except RateLimitedError:
            raise
        except ProviderUnavailableError:
            if attempt == attempts:
                raise
            await sleep(initial_seconds * 2.0 ** (attempt - 1))
    raise AssertionError("unreachable: the last attempt returns or raises")
