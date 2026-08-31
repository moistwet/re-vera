"""Claim-checking pipeline stages.

Milestone 1 ships only ``app.pipeline.mock``, which replays the six fictional
fixture claims with realistic delays and makes zero LLM calls. The real stages
(``extract``, ``retrieve``, ``stance``, ``judge``, ``aggregate``) land in
milestone 2 and each get their own typed input/output and fixture-backed tests.
"""
