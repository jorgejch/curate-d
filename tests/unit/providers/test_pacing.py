"""Requests stay inside the quota, and rate-limit answers are waited out rather than fatal."""

import pytest

from curate_d.domain import Provider
from curate_d.providers.errors import ProviderRequestError, RateLimitedError
from curate_d.providers.pacing import Backoff, Pacer, Quota, paced


class _Time:
    """A clock that only moves when something sleeps on it."""

    def __init__(self) -> None:
        """Start at zero with no sleeps recorded."""
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        """Return the current time."""
        return self.now

    async def sleep(self, seconds: float) -> None:
        """Advance time by `seconds` and record the sleep."""
        self.sleeps.append(seconds)
        self.now += seconds


def _limited(retry_after: float | None = None) -> RateLimitedError:
    """A rate-limit answer."""
    return RateLimitedError(
        Provider.PCLOUD, "a", "list folder", "HTTP 429", retry_after_seconds=retry_after
    )


async def test_requests_never_exceed_the_quota_in_any_window() -> None:
    """Twenty requests against five per ten seconds spread over at least thirty seconds."""
    time = _Time()
    pacer = Pacer(Quota(requests=5, period_seconds=10.0), clock=time.clock, sleep=time.sleep)

    admitted: list[float] = []
    for _ in range(20):
        await pacer.admit()
        admitted.append(time.now)

    for start in admitted:
        in_window = [t for t in admitted if start <= t < start + 10.0]
        assert len(in_window) <= 5
    assert admitted[-1] >= 30.0


async def test_requests_inside_the_quota_are_not_delayed() -> None:
    """A burst within quota goes straight through."""
    time = _Time()
    pacer = Pacer(Quota(requests=5, period_seconds=10.0), clock=time.clock, sleep=time.sleep)
    for _ in range(5):
        await pacer.admit()
    assert time.sleeps == []


async def test_a_rate_limit_answer_backs_off_and_then_succeeds() -> None:
    """The request is retried after waiting, and its result returned."""
    time = _Time()
    pacer = Pacer(Quota(requests=100, period_seconds=1.0), clock=time.clock, sleep=time.sleep)
    answers: list[RateLimitedError | str] = [_limited(), _limited(), "listing"]

    async def request() -> str:
        """Answer from the scripted sequence."""
        answer = answers.pop(0)
        if isinstance(answer, RateLimitedError):
            raise answer
        return answer

    result = await paced(pacer, Backoff(initial_seconds=1.0), request, sleep=time.sleep)

    assert result == "listing"
    assert time.sleeps == [1.0, 2.0]


async def test_the_providers_retry_after_is_honoured_when_longer() -> None:
    """A provider asking for thirty seconds gets thirty, not the backoff's one."""
    time = _Time()
    pacer = Pacer(Quota(requests=100, period_seconds=1.0), clock=time.clock, sleep=time.sleep)
    answers: list[RateLimitedError | str] = [_limited(retry_after=30.0), "ok"]

    async def request() -> str:
        """Answer from the scripted sequence."""
        answer = answers.pop(0)
        if isinstance(answer, RateLimitedError):
            raise answer
        return answer

    await paced(pacer, Backoff(), request, sleep=time.sleep)
    assert time.sleeps == [30.0]


async def test_a_persistent_limit_propagates_after_the_bound() -> None:
    """Past the configured attempts the limit is taken as persistent and raised."""
    time = _Time()
    pacer = Pacer(Quota(requests=100, period_seconds=1.0), clock=time.clock, sleep=time.sleep)
    calls = 0

    async def request() -> str:
        """Always rate-limited."""
        nonlocal calls
        calls += 1
        raise _limited()

    with pytest.raises(RateLimitedError):
        await paced(pacer, Backoff(max_attempts=3), request, sleep=time.sleep)
    assert calls == 3


async def test_other_failures_are_not_retried() -> None:
    """Only rate limits are waited out; a rejected request fails at once."""
    time = _Time()
    pacer = Pacer(Quota(requests=100, period_seconds=1.0), clock=time.clock, sleep=time.sleep)
    calls = 0

    async def request() -> str:
        """Always rejected."""
        nonlocal calls
        calls += 1
        raise ProviderRequestError(Provider.PCLOUD, "a", "op", "result 2009")

    with pytest.raises(ProviderRequestError):
        await paced(pacer, Backoff(), request, sleep=time.sleep)
    assert calls == 1


def test_the_backoff_doubles_up_to_its_ceiling() -> None:
    """Delays grow exponentially and stop growing at the ceiling."""
    backoff = Backoff(initial_seconds=1.0, max_delay_seconds=5.0)
    assert [backoff.delay(n, None) for n in range(1, 6)] == [1.0, 2.0, 4.0, 5.0, 5.0]


@pytest.mark.parametrize(("requests", "period"), [(0, 1.0), (1, 0.0), (-1, 1.0)])
def test_an_empty_quota_is_rejected(requests: int, period: float) -> None:
    """A quota admitting nothing is a configuration error, not a hang."""
    with pytest.raises(ValueError, match="positive"):
        Quota(requests=requests, period_seconds=period)
