"""Re-Vera backend.

FastAPI service behind the Re-Vera Chrome extension. Milestone 1 is a walking
skeleton: ``POST /check`` and ``GET /check/{job_id}/stream`` are real, backed by
a mocked pipeline that streams the six fictional fixture claims. No LLM calls.

Layout:

* ``app.config``          — :class:`~app.config.Settings` (pydantic-settings)
* ``app.schema_models``   — GENERATED from ``shared/schema.json``; do not edit
* ``app.events``          — per-job Redis event list (replay) + pub/sub (live)
* ``app.cache``           — 7-day URL cache
* ``app.limits``          — per-install daily cap
* ``app.pipeline``        — pipeline stages (milestone 1: ``mock`` only)
* ``app.routes``          — HTTP routers
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
