"""The per-install daily cap.

The cap bounds cost as much as abuse (decision 10), so the interesting cases are
the boundary — the twentieth check must succeed and the twenty-first must not —
and the isolation properties: one install ID's usage must never spend another's,
and the counter must roll over with the Singapore day rather than UTC's.

Every test drives :func:`app.limits.check_daily_cap` against fakeredis directly;
the HTTP surface of the cap (the 429) is covered in :mod:`tests.test_check_flow`.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fakeredis.aioredis import FakeRedis

from app.limits import CAP_TTL_SECONDS, DailyCapExceeded, check_daily_cap, singapore_today

CAP = 20
"""The production default, asserted here rather than the small cap the app
fixtures use, because 20 is the number in the brief."""

DAY = "20260831"
"""A fixed day, so nothing in this module depends on the wall clock."""

INSTALL_ID = "11111111-2222-3333-4444-555555555555"


def cap_key(install_id: str, day: str) -> str:
    """The documented Redis key for one install ID on one day."""
    return f"cap:{install_id}:{day}"


async def test_the_first_twenty_checks_are_allowed_and_counted(fake_redis: FakeRedis) -> None:
    """Calls 1 … 20 succeed and hand back their own position in the day."""
    counts = [
        await check_daily_cap(fake_redis, INSTALL_ID, CAP, day=DAY) for _ in range(CAP)
    ]
    assert counts == list(range(1, CAP + 1))


async def test_the_twenty_first_check_is_refused(fake_redis: FakeRedis) -> None:
    """The call past the cap raises, and says what it counted and against what."""
    for _ in range(CAP):
        await check_daily_cap(fake_redis, INSTALL_ID, CAP, day=DAY)

    with pytest.raises(DailyCapExceeded) as excinfo:
        await check_daily_cap(fake_redis, INSTALL_ID, CAP, day=DAY)

    assert excinfo.value.count == CAP + 1
    assert excinfo.value.cap == CAP
    assert str(CAP) in str(excinfo.value)


async def test_the_cap_stays_exceeded_on_later_attempts(fake_redis: FakeRedis) -> None:
    """A refused install ID does not get a free check on the next request."""
    for _ in range(CAP):
        await check_daily_cap(fake_redis, INSTALL_ID, CAP, day=DAY)
    for expected in (CAP + 1, CAP + 2):
        with pytest.raises(DailyCapExceeded) as excinfo:
            await check_daily_cap(fake_redis, INSTALL_ID, CAP, day=DAY)
        assert excinfo.value.count == expected


async def test_a_different_install_id_has_its_own_allowance(fake_redis: FakeRedis) -> None:
    """One reader burning through the cap must not lock anyone else out."""
    for _ in range(CAP):
        await check_daily_cap(fake_redis, INSTALL_ID, CAP, day=DAY)
    with pytest.raises(DailyCapExceeded):
        await check_daily_cap(fake_redis, INSTALL_ID, CAP, day=DAY)

    assert await check_daily_cap(fake_redis, "another-install", CAP, day=DAY) == 1


async def test_a_new_day_resets_the_counter(fake_redis: FakeRedis) -> None:
    """The count is per day: tomorrow starts again at one."""
    for _ in range(CAP):
        await check_daily_cap(fake_redis, INSTALL_ID, CAP, day=DAY)
    with pytest.raises(DailyCapExceeded):
        await check_daily_cap(fake_redis, INSTALL_ID, CAP, day=DAY)

    assert await check_daily_cap(fake_redis, INSTALL_ID, CAP, day="20260901") == 1


async def test_the_counter_key_gets_a_ttl(fake_redis: FakeRedis) -> None:
    """A day's counter expires on its own — nothing sweeps these keys.

    The expiry is anchored to the first check of the day, not refreshed by later
    ones, so the window can never be extended indefinitely by a busy reader.
    """
    key = cap_key(INSTALL_ID, DAY)

    await check_daily_cap(fake_redis, INSTALL_ID, CAP, day=DAY)
    first_ttl = await fake_redis.ttl(key)
    assert 0 < first_ttl <= CAP_TTL_SECONDS
    assert CAP_TTL_SECONDS - first_ttl < 5, "the TTL should be set to the full window"

    for _ in range(5):
        await check_daily_cap(fake_redis, INSTALL_ID, CAP, day=DAY)
    assert await fake_redis.ttl(key) <= first_ttl


async def test_the_key_always_has_a_ttl_from_the_very_first_increment(
    fake_redis: FakeRedis,
) -> None:
    """A cap key without a TTL never expires, and locks that install ID out for good.

    The counter used to be ``INCR`` followed by a conditional ``EXPIRE`` — two
    round trips — so a crash, restart or dropped connection between them left a
    key at 1 with no expiry, and that reader was capped forever. Seeding and
    incrementing now happen in one transaction, so the TTL exists from the first
    call onwards and never has a window in which it does not.
    """
    key = cap_key(INSTALL_ID, DAY)

    for expected in range(1, 6):
        assert await check_daily_cap(fake_redis, INSTALL_ID, CAP, day=DAY) == expected
        ttl = await fake_redis.ttl(key)
        assert ttl > 0, f"no TTL after increment {expected} (ttl={ttl})"
        assert ttl <= CAP_TTL_SECONDS


async def test_the_ttl_survives_a_broken_standalone_expire(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The expiry is not a separate ``EXPIRE`` call that can fail on its own.

    Sabotaging ``redis.expire`` proves the point: if the cap still depended on a
    second round trip, this would either raise or leave the key TTL-less.
    """

    async def unreachable(*args: object, **kwargs: object) -> int:
        raise AssertionError("check_daily_cap must not issue a standalone EXPIRE")

    monkeypatch.setattr(fake_redis, "expire", unreachable)

    assert await check_daily_cap(fake_redis, INSTALL_ID, CAP, day=DAY) == 1
    assert await fake_redis.ttl(cap_key(INSTALL_ID, DAY)) > 0


async def test_a_second_check_never_resets_the_counter(fake_redis: FakeRedis) -> None:
    """The seeding write is ``SET NX``: it must not zero a counter already running.

    A plain ``SET`` here would hand every reader an unlimited allowance.
    """
    for _ in range(CAP):
        await check_daily_cap(fake_redis, INSTALL_ID, CAP, day=DAY)

    assert await fake_redis.get(cap_key(INSTALL_ID, DAY)) == str(CAP)
    with pytest.raises(DailyCapExceeded):
        await check_daily_cap(fake_redis, INSTALL_ID, CAP, day=DAY)


async def test_the_counter_lives_under_the_documented_key(fake_redis: FakeRedis) -> None:
    """``cap:{install_id}:{day}`` — the key shape the brief documents."""
    await check_daily_cap(fake_redis, INSTALL_ID, CAP, day=DAY)
    await check_daily_cap(fake_redis, INSTALL_ID, CAP, day=DAY)

    assert await fake_redis.get(cap_key(INSTALL_ID, DAY)) == "2"
    assert await fake_redis.keys("cap:*") == [cap_key(INSTALL_ID, DAY)]


async def test_the_day_defaults_to_today_in_singapore(fake_redis: FakeRedis) -> None:
    """Omitting ``day`` counts against the Singapore calendar day.

    Sampling the expected day on both sides of the call keeps this race-free
    across a midnight boundary.
    """
    before = singapore_today()
    await check_daily_cap(fake_redis, INSTALL_ID, CAP)
    after = singapore_today()

    keys = await fake_redis.keys("cap:*")
    assert keys == [cap_key(INSTALL_ID, before)] or keys == [cap_key(INSTALL_ID, after)]


def test_singapore_today_is_an_eight_digit_singapore_date() -> None:
    """``YYYYMMDD`` in Asia/Singapore — readers' days roll over at their midnight."""
    before = datetime.now(ZoneInfo("Asia/Singapore")).strftime("%Y%m%d")
    value = singapore_today()
    after = datetime.now(ZoneInfo("Asia/Singapore")).strftime("%Y%m%d")

    assert len(value) == 8
    assert value.isdigit()
    assert value in {before, after}


def test_the_ttl_outlives_the_day_it_counts() -> None:
    """48 hours: comfortably longer than any day the counter can belong to, so a
    counter is never expired out from under the day it is counting."""
    assert CAP_TTL_SECONDS == 48 * 3600
    assert CAP_TTL_SECONDS > 24 * 3600
