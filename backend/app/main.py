"""FastAPI application factory.

Owns the process-wide Redis client (opened for the app's lifespan), the CORS
policy that lets the extension's service worker call this API, and the mounting
of the check router. Run it with::

    uv run uvicorn app.main:app --reload
"""

from __future__ import annotations

import fnmatch
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as redis_asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.config import get_settings
from app.routes.check import get_redis
from app.routes.check import router as check_router

__all__ = ["app", "cors_origins", "create_app", "get_redis", "lifespan"]

DESCRIPTION = (
    "Checks the factual claims in a news article, only when the reader asks. "
    "Milestone 1 streams six fictional fixture claims and makes no LLM calls."
)

REDIS_CONNECT_TIMEOUT_SECONDS = 5.0
"""Ceiling on establishing a new connection to Redis.

Not a :class:`~app.config.Settings` field: it is an operational ceiling on this
process's one Redis client, in the same spirit as
:data:`app.pipeline.providers.base.PROVIDER_TIMEOUT_SECONDS` — a number this
module owns outright rather than one a deployment is expected to tune.
"""

REDIS_SOCKET_TIMEOUT_SECONDS = 10.0
"""Ceiling on any single Redis command's read/write once connected.

**Why this matters more than it looks like it should**: without it, ``redis-py``
places no bound at all on a socket read, so a single stalled Redis command —
in the request path (``POST /check``'s cache lookup, the daily-cap ``INCR``) or
in a spawned worker — can block forever. That defeats every *other* timeout in
the system, including :func:`app.routes.check.stream_deadline_seconds`'s
promise that a stream always ends: that deadline is enforced by racing the
event-relay generator against a clock in ``asyncio``, and an ``await`` on a
Redis call that never returns races against nothing.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open one Redis client for the app's lifetime and close it on shutdown.

    ``decode_responses=True`` so everything downstream works in ``str``: the
    event stream, the cache and the daily-cap counters all store JSON text.
    Connections are established lazily, so importing or starting the app never
    requires a live Redis — handy for tests, which override
    :func:`app.routes.check.get_redis` with fakeredis instead.

    ``socket_connect_timeout``/``socket_timeout`` bound how long establishing a
    connection, and every command on it, may take —
    :data:`REDIS_CONNECT_TIMEOUT_SECONDS` and
    :data:`REDIS_SOCKET_TIMEOUT_SECONDS`. ``socket_keepalive=True`` asks the OS
    to probe an idle connection so a network partition is *noticed* — surfaced
    as a broken connection on the next command — rather than left looking alive
    forever. None of this retries anything on our behalf: a timed-out command
    raises straight through to its caller, same as every other provider timeout
    in this codebase (``CLAUDE.md`` cost rule: no silent retries).
    """
    client = redis_asyncio.from_url(
        get_settings().redis_url,
        decode_responses=True,
        socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
        socket_keepalive=True,
    )
    app.state.redis = client
    try:
        yield
    finally:
        await client.aclose()


def cors_origins(raw: str) -> tuple[list[str], str | None]:
    """Split a configured origin list into exact origins and a regex for globs.

    ``ALLOWED_EXTENSION_ORIGIN`` accepts a comma-separated list, and an entry may
    contain ``*`` — the default ``chrome-extension://*`` exists because an
    unpacked extension's id is not known during development. Starlette matches
    ``allow_origins`` exactly, so glob entries are translated into
    ``allow_origin_regex`` instead. A bare ``*`` allows everything.
    """
    origins = [item.strip() for item in raw.split(",") if item.strip()]
    if "*" in origins:
        return ["*"], None
    exact = [origin for origin in origins if "*" not in origin]
    globs = [origin for origin in origins if "*" in origin]
    regex = "|".join(fnmatch.translate(glob) for glob in globs) or None
    return exact, regex


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    settings = get_settings()
    allow_origins, allow_origin_regex = cors_origins(settings.allowed_extension_origin)

    app = FastAPI(
        title="Re-Vera API",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_origin_regex=allow_origin_regex,
        allow_methods=["GET", "POST"],
        allow_headers=["content-type"],
    )
    app.include_router(check_router)
    return app


app = create_app()
