"""Tests for the per-domain async rate limiter (Task 4.1).

The limiter enforces a minimum interval (``1 / max_rps``) between successive
``acquire`` calls for the SAME domain, while letting DIFFERENT domains proceed
independently. Timing is measured on the asyncio event-loop clock
(``loop.time()``) rather than wall clock, and the sleep function is injectable so
these tests stay fast and deterministic.
"""

import asyncio

from crawloop.access import RateLimiter


async def test_same_domain_back_to_back_calls_are_spaced():
    """Two consecutive acquires for one domain are spaced >= ~0.09s.

    Uses the real ``asyncio.sleep`` but a fast rate (10 rps -> 0.1s interval) so
    the test costs ~0.1s. Elapsed is measured on the event-loop clock.
    """
    rl = RateLimiter(max_rps=10)
    loop = asyncio.get_running_loop()

    await rl.acquire("d")  # first call: no prior timestamp, returns immediately
    start = loop.time()
    await rl.acquire("d")  # second call: must wait out the min interval
    elapsed = loop.time() - start

    assert elapsed >= 0.09


async def test_different_domains_do_not_block_each_other():
    """Acquires for two distinct domains interleave with no inter-domain gap."""
    rl = RateLimiter(max_rps=10)
    loop = asyncio.get_running_loop()

    await rl.acquire("a")
    await rl.acquire("b")  # different domain: must NOT wait on "a"'s interval

    start = loop.time()
    await rl.acquire("c")  # yet another fresh domain: also immediate
    elapsed = loop.time() - start

    assert elapsed < 0.05


async def test_min_interval_uses_injected_sleep_with_fake_clock():
    """With a fake clock + injected sleep, the limiter requests exactly the
    remaining interval and never calls the real ``asyncio.sleep``.

    The fake clock only advances when the limiter sleeps, so we can assert the
    precise wait amounts deterministically and instantly.
    """
    now = 0.0
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        nonlocal now
        slept.append(seconds)
        now += seconds

    rl = RateLimiter(max_rps=5, sleep=fake_sleep, time=lambda: now)  # interval 0.2s

    await rl.acquire("d")  # first: no wait
    await rl.acquire("d")  # second: full interval, clock was frozen
    await rl.acquire("d")  # third: full interval again

    assert slept == [0.2, 0.2]


async def test_partial_elapsed_time_only_waits_the_remainder():
    """If some of the interval already elapsed, only the remainder is slept."""
    now = 0.0
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        nonlocal now
        slept.append(seconds)
        now += seconds

    rl = RateLimiter(max_rps=10, sleep=fake_sleep, time=lambda: now)  # interval 0.1s

    await rl.acquire("d")
    now += 0.04  # 0.04s passes "naturally" between calls
    await rl.acquire("d")

    assert len(slept) == 1
    assert abs(slept[0] - 0.06) < 1e-9  # only the remaining 0.06s
