"""The per-install daily cap on checks.

A cost control as much as an abuse control: each anonymous install ID gets
``DAILY_CAP`` (20) checks per calendar day, counted in Asia/Singapore because
that is where the readers are — the day should roll over overnight for them,
not mid-afternoon on UTC's schedule. The counter is the only thing an install
ID is ever used for.

The cap value and the day are passed in; nothing here reads settings or builds
a Redis client.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from redis.asyncio import Redis

CAP_KEY = "cap:{install_id}:{day}"
"""Redis counter key, one per install ID per day."""

CAP_TTL_SECONDS = 48 * 3600
"""How long a day's counter lives — comfortably past the day it counts, so a
counter is never expired out from under the day it belongs to."""

_SINGAPORE = ZoneInfo("Asia/Singapore")


class DailyCapExceeded(Exception):
    """Raised when an install ID has used up its checks for the day."""

    def __init__(self, count: int, cap: int) -> None:
        self.count = count
        """Checks attempted today, including the one that was refused."""
        self.cap = cap
        """The cap that was exceeded."""
        super().__init__(f"Daily cap of {cap} checks exceeded (this is check {count} today).")


def singapore_today() -> str:
    """Return today's date in Asia/Singapore as ``YYYYMMDD``."""
    return datetime.now(_SINGAPORE).strftime("%Y%m%d")


async def check_daily_cap(redis: Redis, install_id: str, cap: int, day: str | None = None) -> int:
    """Count one check against ``install_id`` and return the new count.

    Raises :class:`DailyCapExceeded` once the count passes ``cap``. ``day``
    defaults to :func:`singapore_today`; tests pass it explicitly to avoid
    depending on the clock.
    """
    key = CAP_KEY.format(install_id=install_id, day=day or singapore_today())
    count = int(await redis.incr(key))
    if count == 1:
        # First check of the day: give the fresh counter its expiry. Setting it
        # only here keeps the window anchored to the day, not to the last check.
        await redis.expire(key, CAP_TTL_SECONDS)
    if count > cap:
        raise DailyCapExceeded(count, cap)
    return count
