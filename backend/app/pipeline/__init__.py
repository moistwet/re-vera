"""Claim-checking pipeline stages.

Two pipelines live here, and they are interchangeable at the route
(``app/routes/check.py`` picks one with ``settings.use_mock_pipeline``) because
they take the same arguments and owe the stream the same events: a
``claims_found`` carrying every claim id in article order, one ``claim`` per
claim as it resolves, the result written to the 7-day cache, then ``done`` — or
``error``, so a reader is never left on a stream that will not end.

* :mod:`app.pipeline.run` is the real one. It orchestrates the five stages —
  :mod:`~app.pipeline.extract` → :mod:`~app.pipeline.retrieve` →
  :mod:`~app.pipeline.stance` → :mod:`~app.pipeline.judge` →
  :mod:`~app.pipeline.aggregate` — working claims concurrently so they stream in
  as each resolves. Its outbound calls all go through
  :class:`~app.pipeline.run.PipelineDeps`, which is why the whole pipeline can be
  exercised offline.
* :mod:`app.pipeline.mock` is the milestone-1 fake: six fictional fixture claims
  with the prototype's pacing and no API call at all. It is selected
  deliberately, for demos and for extension work, and is never a fallback — the
  real pipeline failing publishes an ``error`` rather than quietly streaming
  fixture verdicts for somebody's actual article.

Every stage has a typed input and output (:mod:`app.pipeline.types`), keeps its
prompt in ``app/prompts/`` rather than inline, and runs against fixtures with no
network.
"""
