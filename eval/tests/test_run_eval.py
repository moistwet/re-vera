"""Tests for the eval harness itself.

The harness is the thing that decides whether a prompt change made Re-Vera
better or worse, so a quiet bug here is worse than a quiet bug in a stage: it
would not produce a wrong verdict, it would produce a *wrong opinion about* the
verdicts, and CI would enforce it. Three things are therefore pinned hard:

* **the arithmetic**, against a hand-computed confusion matrix, including every
  zero-division case — a verdict never predicted and a verdict never present;
* **the gate**, at its exact boundary and in its non-zero exit;
* **offline mode's promise that it never networks**, which is checked by proving
  the socket guard is armed for the whole run rather than by reading the code.

Nothing here touches the network. Nothing here needs an API key.
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

EVAL_ROOT = Path(__file__).resolve().parent.parent
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

import run_eval  # noqa: E402
from run_eval import (  # noqa: E402
    DEFAULT_FIXTURES,
    DEFAULT_GOLDEN,
    GATE_MIN_PRECISION,
    VERDICTS,
    ClaimOutcome,
    EvalError,
    FixtureTransport,
    Gate,
    NetworkAccessDenied,
    abstention_rate,
    build_report,
    confusion_matrix,
    format_report,
    install_network_guard,
    load_fixtures,
    load_golden,
    main,
    stance_recording,
    verdict_score,
)

# ---------------------------------------------------------------- the arithmetic

# One hand-computed run, used by every metric test below so the numbers can be
# checked once, by eye, against the matrix rather than against the code:
#
#            predicted
#   gold      sup  con  mis  unv   | gold total
#   sup        3    1    0    1     =  5
#   con        0    4    0    2     =  6
#   mis        1    0    2    0     =  3
#   unv        0    0    0    2     =  2
#   pred tot   4    5    2    5        (16 claims)
#
#   supported        P = 3/4 = 0.75    R = 3/5 = 0.60
#   contradicted     P = 4/5 = 0.80    R = 4/6 ≈ 0.667
#   missing_context  P = 2/2 = 1.00    R = 2/3 ≈ 0.667
#   unverifiable     P = 2/5 = 0.40    R = 2/2 = 1.00
#   abstention                          5/16 = 0.3125
HAND_COMPUTED: list[tuple[str, str]] = (
    [("supported", "supported")] * 3
    + [("supported", "contradicted")]
    + [("supported", "unverifiable")]
    + [("contradicted", "contradicted")] * 4
    + [("contradicted", "unverifiable")] * 2
    + [("missing_context", "supported")]
    + [("missing_context", "missing_context")] * 2
    + [("unverifiable", "unverifiable")] * 2
)


def test_the_confusion_matrix_counts_every_pair_into_the_right_cell() -> None:
    matrix = confusion_matrix(HAND_COMPUTED)
    assert matrix["supported"] == {
        "supported": 3,
        "contradicted": 1,
        "missing_context": 0,
        "unverifiable": 1,
    }
    assert matrix["contradicted"]["contradicted"] == 4
    assert matrix["contradicted"]["unverifiable"] == 2
    assert matrix["missing_context"]["supported"] == 1
    assert matrix["unverifiable"]["unverifiable"] == 2
    assert sum(sum(row.values()) for row in matrix.values()) == len(HAND_COMPUTED)


def test_the_matrix_has_a_cell_for_every_verdict_even_when_unused() -> None:
    """An empty run still prints a full four-by-four table, all zeros."""
    matrix = confusion_matrix([])
    assert set(matrix) == set(VERDICTS)
    assert all(set(row) == set(VERDICTS) for row in matrix.values())
    assert all(value == 0 for row in matrix.values() for value in row.values())


def test_a_verdict_outside_the_four_is_rejected_rather_than_counted() -> None:
    with pytest.raises(EvalError):
        confusion_matrix([("supported", "flagged")])
    with pytest.raises(EvalError):
        confusion_matrix([("TRUE", "supported")])


@pytest.mark.parametrize(
    ("verdict", "gold", "predicted", "hit", "precision", "recall"),
    [
        ("supported", 5, 4, 3, 0.75, 0.6),
        ("contradicted", 6, 5, 4, 0.8, 4 / 6),
        ("missing_context", 3, 2, 2, 1.0, 2 / 3),
        ("unverifiable", 2, 5, 2, 0.4, 1.0),
    ],
)
def test_per_verdict_metrics_match_the_hand_computed_matrix(
    verdict: str, gold: int, predicted: int, hit: int, precision: float, recall: float
) -> None:
    score = verdict_score(confusion_matrix(HAND_COMPUTED), verdict)
    assert (score.gold, score.predicted, score.true_positive) == (gold, predicted, hit)
    assert score.precision == pytest.approx(precision)
    assert score.recall == pytest.approx(recall)
    assert score.f1 == pytest.approx(2 * precision * recall / (precision + recall))


def test_a_verdict_that_was_never_predicted_has_undefined_precision_not_perfect() -> None:
    """The zero-division case that matters most.

    A pipeline that never says `contradicted` divides by zero, and calling that
    1.000 would make "never answer" the easiest way to pass the safety gate.
    """
    score = verdict_score(confusion_matrix([("contradicted", "unverifiable")]), "contradicted")
    assert score.predicted == 0
    assert score.precision is None
    assert score.recall == 0.0
    assert score.f1 is None


def test_a_verdict_absent_from_the_golden_set_has_undefined_recall() -> None:
    score = verdict_score(confusion_matrix([("supported", "missing_context")]), "contradicted")
    assert score.gold == 0
    assert score.recall is None
    assert score.precision is None


def test_f1_is_undefined_rather_than_zero_when_both_sides_are_zero() -> None:
    """Precision 0 and recall 0 would divide by zero in the F1 formula."""
    pairs = [("contradicted", "supported"), ("supported", "contradicted")]
    score = verdict_score(confusion_matrix(pairs), "contradicted")
    assert (score.gold, score.predicted, score.true_positive) == (1, 1, 0)
    assert (score.precision, score.recall) == (0.0, 0.0)
    assert score.f1 is None


def test_the_abstention_rate_is_the_share_answered_unverifiable() -> None:
    assert abstention_rate([pair[1] for pair in HAND_COMPUTED]) == pytest.approx(5 / 16)
    assert abstention_rate(["unverifiable"] * 3) == 1.0
    assert abstention_rate(["supported"]) == 0.0
    assert abstention_rate([]) == 0.0


# ---------------------------------------------------------------- the gate


@pytest.mark.parametrize(
    ("hit", "predicted", "passes"),
    [
        (9, 10, True),  # exactly 0.90 — the boundary must pass, not round away
        (10, 10, True),
        (8, 9, False),  # 0.889
        (0, 1, False),
    ],
)
def test_the_gate_passes_at_and_above_the_threshold(
    hit: int, predicted: int, passes: bool
) -> None:
    gate = Gate(
        verdict="contradicted",
        threshold=GATE_MIN_PRECISION,
        value=hit / predicted,
        predictions=predicted,
    )
    assert gate.passed is passes


def test_the_gate_does_not_pass_on_zero_predictions() -> None:
    """An undefined precision must never clear a safety gate."""
    gate = Gate(
        verdict="contradicted", threshold=GATE_MIN_PRECISION, value=None, predictions=0
    )
    assert gate.passed is False


# ---------------------------------------------------------------- reporting


def _outcome(claim_id: str, gold: str, predicted: str | None, **kwargs: Any) -> ClaimOutcome:
    golden = run_eval.GoldenClaim(
        id=claim_id,
        article_id="fixture",
        article_url="https://example.com/fixture",
        quote="a quote long enough to be real",
        kind="general",
        gold_verdict=gold,
        gold_sources=() if gold == "unverifiable" else ("https://island-wire.example/x",),
        notes="",
        start=0,
        end=30,
    )
    if predicted is None:
        return ClaimOutcome(golden=golden, claim=None, error=kwargs.get("error", "boom"))
    claim = {
        "id": claim_id,
        "verdict": predicted,
        "sources": kwargs.get("sources", []),
    }
    return ClaimOutcome(golden=golden, claim=claim)


def test_an_errored_claim_is_excluded_from_the_metrics_not_scored_as_an_abstention() -> None:
    """A claim nobody managed to check is not evidence that the pipeline was cautious."""
    report = build_report(
        [
            _outcome("a", "contradicted", "contradicted"),
            _outcome("b", "supported", None),
        ],
        mode="offline",
        golden_path=Path("golden.jsonl"),
    )
    assert report.total == 2
    assert len(report.scored) == 1
    assert len(report.errored) == 1
    assert report.abstention == 0.0
    assert verdict_score(report.matrix, "supported").gold == 0
    assert report.gate.passed is True
    assert report.ok is False, "an unchecked claim must still fail the run"


def test_the_report_renders_undefined_metrics_as_a_dash() -> None:
    report = build_report(
        [_outcome("a", "supported", "supported")], mode="offline", golden_path=Path("g.jsonl")
    )
    text = format_report(report)
    assert "—" in text
    assert "GATE" in text
    assert "directional" in text


def test_the_json_summary_carries_the_matrix_the_gate_and_every_claim() -> None:
    report = build_report(
        [
            _outcome("a", "contradicted", "contradicted"),
            _outcome("b", "supported", "unverifiable"),
        ],
        mode="offline",
        golden_path=Path("g.jsonl"),
    )
    summary = report.to_json()
    assert summary["gate"]["threshold"] == GATE_MIN_PRECISION
    assert summary["confusion"]["supported"]["unverifiable"] == 1
    assert summary["per_verdict"]["contradicted"]["precision"] == 1.0
    assert [row["id"] for row in summary["claims_detail"]] == ["a", "b"]
    assert json.dumps(summary), "the summary must be JSON-serialisable"


# ---------------------------------------------------------------- offline is offline


def test_the_network_guard_blocks_connections_and_then_restores_them() -> None:
    undo = install_network_guard()
    try:
        with pytest.raises(NetworkAccessDenied):
            socket.create_connection(("example.com", 80), timeout=0.01)
        with pytest.raises(NetworkAccessDenied):
            socket.getaddrinfo("example.com", 80)
    finally:
        undo()
    assert socket.socket.connect is not None
    # Restored to something callable that is no longer the guard: proved by the
    # guard's own exception type no longer being raised on a lookup of localhost.
    socket.getaddrinfo("127.0.0.1", 80)


def test_an_offline_run_keeps_the_guard_armed_for_the_whole_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offline mode's promise, checked rather than assumed.

    The spy records when the guard went up and came down and, crucially, whether
    it was still up while the pipeline ran — a guard installed and immediately
    removed would satisfy a naive "was it called?" assertion.
    """
    events: list[str] = []
    real_guard = install_network_guard

    def spy() -> Any:
        events.append("armed")
        undo = real_guard()

        def wrapped_undo() -> None:
            events.append("disarmed")
            undo()

        return wrapped_undo

    armed_during_run: list[bool] = []
    real_run = run_eval.run_golden_set

    async def watched(*args: Any, **kwargs: Any) -> Any:
        try:
            socket.getaddrinfo("example.com", 80)
        except NetworkAccessDenied:
            armed_during_run.append(True)
        except OSError:
            armed_during_run.append(False)
        else:
            armed_during_run.append(False)
        return await real_run(*args, **kwargs)

    monkeypatch.setattr(run_eval, "install_network_guard", spy)
    monkeypatch.setattr(run_eval, "run_golden_set", watched)

    assert main(["--offline", "--no-json"]) == 0
    assert events == ["armed", "disarmed"]
    assert armed_during_run == [True]


def test_the_offline_run_never_opens_a_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Counts real connection attempts made before the guard is even installed.

    The guard would raise on any attempt anyway; this records the same fact from
    the other side, so the test still means something if the guard is ever
    weakened.
    """
    attempts: list[Any] = []
    real_connect = socket.socket.connect

    def counting_connect(self: Any, address: Any) -> Any:  # pragma: no cover - never hit
        attempts.append(address)
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", counting_connect)
    assert main(["--offline", "--no-json"]) == 0
    assert attempts == []


def test_every_model_call_in_an_offline_run_is_answered_by_a_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of "no network": the seams, not just the sockets.

    Counting sockets proves nothing was *sent*. This proves nothing live was
    even *built*: the OpenAI SDK wrapper, the one HTTP client every provider
    speaks through, and the live provider factory are all replaced with
    landmines, and every completion the run makes is recorded so the transport
    that answered it can be named. A future `offline_deps` that quietly reached
    for a real provider would fail here even if the machine had no network to
    reach it with.
    """
    import app.llm as llm
    import app.pipeline.providers.base as providers_base
    import app.pipeline.retrieve as retrieve

    touched: list[str] = []

    def landmine(name: str) -> Any:
        def explode(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - never hit
            touched.append(name)
            raise AssertionError(f"an offline run reached {name}")

        return explode

    monkeypatch.setattr(llm, "build_openai_transport", landmine("build_openai_transport"))
    monkeypatch.setattr(llm.OpenAIChatTransport, "__init__", landmine("OpenAIChatTransport"))
    monkeypatch.setattr(providers_base.HttpxClient, "__init__", landmine("HttpxClient"))
    monkeypatch.setattr(retrieve, "build_providers", landmine("build_providers"))

    # Every call the client makes, and every call a fixture answered. Comparing
    # the two counts is what makes this more than a tautology: a call served by
    # any other transport would raise the first number without the second.
    asked: list[str] = []
    served: list[str] = []
    real_structured = llm.LLMClient.structured
    real_complete = FixtureTransport.complete

    async def counted(self: Any, **kwargs: Any) -> Any:
        asked.append(str(kwargs["prompt"].name))
        return await real_structured(self, **kwargs)

    async def recorded(self: FixtureTransport, **kwargs: Any) -> Any:
        served.append(type(self).__name__)
        return await real_complete(self, **kwargs)

    monkeypatch.setattr(llm.LLMClient, "structured", counted)
    monkeypatch.setattr(FixtureTransport, "complete", recorded)

    assert main(["--offline", "--no-json"]) == 0
    assert touched == []
    assert asked, "the golden set should have made at least one model call"
    assert set(asked) <= {"stance", "judge"}, "only stages 3 and 4 run on a pre-extracted claim"
    assert served == ["FixtureTransport"] * len(asked)


# ---------------------------------------------------------------- end to end


def test_a_stray_environment_variable_cannot_move_an_offline_number(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`_env_file=None` blocks a local .env; the process environment is the hole.

    `MAX_PASSAGES_PER_CLAIM` caps what retrieval keeps, so exporting it silently
    rewrites every metric in the report — and nothing printed would say why. An
    offline run pins it to `app/config.py`'s default instead, which is what makes
    "the same fixtures score the same everywhere" true rather than nearly true.
    """
    assert run_eval.OFFLINE_PINNED == ("max_passages_per_claim",)
    monkeypatch.setenv("MAX_PASSAGES_PER_CLAIM", "1")

    assert run_eval.eval_settings(live=False).max_passages_per_claim == 6
    assert main(["--offline", "--no-json"]) == 0
    assert "abstention rate   0.344" in capsys.readouterr().out

    # ...and the variable is genuinely live otherwise, so the pin is load-bearing.
    assert run_eval.Settings().max_passages_per_claim == 1


def test_the_real_golden_set_runs_offline_and_passes_the_gate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The run CI depends on. Exit 0, gate passed, every claim scored."""
    assert main(["--offline", "--no-json"]) == 0
    printed = capsys.readouterr().out
    assert "GATE  precision on `contradicted`" in printed
    assert "PASS" in printed
    assert "ERRORS" not in printed


def test_the_run_is_deterministic(capsys: pytest.CaptureFixture[str]) -> None:
    """Two offline runs of the same fixtures must produce identical reports."""
    main(["--offline", "--no-json"])
    first = capsys.readouterr().out
    main(["--offline", "--no-json"])
    assert capsys.readouterr().out == first


def test_json_out_writes_a_machine_readable_summary(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "summary.json"
    assert main(["--offline", "--no-json", "--json-out", str(destination)]) == 0
    summary = json.loads(destination.read_text(encoding="utf-8"))
    assert summary["mode"] == "offline"
    assert summary["gate"]["passed"] is True
    assert summary["claims"]["errored"] == 0
    assert set(summary["per_verdict"]) == set(VERDICTS)


# ---------------------------------------------------------------- the gate fails loudly

_REFUTING_FIXTURE: dict[str, Any] = {
    "_fictional": "Invented for this test.",
    "claims": {
        "t1": {
            "official": [
                {
                    "outlet": "Data Portal",
                    "url": "https://data.gov.example/datasets/thing",
                    "date": "2026-01-01",
                    "stance": "refutes",
                    "quote": "the figure is four per cent, not forty",
                    "text": "The published series shows the figure is four per cent, "
                    "not forty, over the period in question.",
                }
            ],
            "judge": {
                "verdict": "contradicted",
                "confidence": "high",
                "evidence": "The Data Portal series puts the figure at four per cent.",
                "cited_spans": ["the figure is four per cent, not forty"],
            },
        }
    },
}


def _write_case(tmp_path: Path, gold_verdict: str) -> tuple[Path, Path]:
    """A one-claim golden set whose fixture always drives `contradicted`."""
    golden = tmp_path / "tiny.jsonl"
    golden.write_text(
        json.dumps({"_fictional": "Invented for this test."})
        + "\n"
        + json.dumps(
            {
                "id": "t1",
                "article_id": "tiny",
                "article_url": "https://example.com/tiny-article",
                "quote": "a claim about a figure that is stated plainly",
                "kind": "numeric",
                "gold_verdict": gold_verdict,
                "gold_sources": ["https://data.gov.example/datasets/thing"],
                "notes": "",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "tiny.json").write_text(
        json.dumps(_REFUTING_FIXTURE), encoding="utf-8"
    )
    return golden, fixtures


def test_a_false_positive_contradicted_fails_the_process(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Gold says supported, the pipeline says contradicted: precision 0.0, exit 1."""
    golden, fixtures = _write_case(tmp_path, "supported")
    exit_code = main(
        ["--offline", "--no-json", "--golden", str(golden), "--fixtures", str(fixtures)]
    )
    assert exit_code == 1
    assert "FAIL" in capsys.readouterr().out


def test_a_correct_contradicted_passes_the_process(tmp_path: Path) -> None:
    golden, fixtures = _write_case(tmp_path, "contradicted")
    assert (
        main(["--offline", "--no-json", "--golden", str(golden), "--fixtures", str(fixtures)])
        == 0
    )


# ---------------------------------------------------------------- the golden set itself


def test_the_golden_set_declares_itself_fictional_before_its_first_claim() -> None:
    """The one failure this file has that no metric would catch.

    A golden set that could be mistaken for real reporting is a liability, so the
    loader refuses to read claims until the file has said it is invented.
    """
    first = DEFAULT_GOLDEN.read_text(encoding="utf-8").splitlines()[0]
    note = json.loads(first)["_fictional"]
    assert "FICTIONAL" in note
    assert "invented" in note.lower()
    assert "never present" in note.lower()


def test_a_claim_before_the_fictional_note_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps({"id": "x"}) + "\n", encoding="utf-8")
    with pytest.raises(EvalError, match="_fictional"):
        load_golden(path)


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ({"gold_verdict": "flagged"}, "not one of"),
        ({"gold_verdict": "TRUE"}, "not one of"),
        ({"kind": "opinion"}, "ClaimKind"),
        ({"gold_verdict": "unverifiable"}, "carries no sources"),
        ({"gold_sources": []}, "at least one gold source"),
    ],
)
def test_a_malformed_golden_row_is_rejected(
    tmp_path: Path, row: dict[str, Any], message: str
) -> None:
    """The two product invariants hold for gold labels as well as for output."""
    base = {
        "id": "x1",
        "article_id": "a",
        "article_url": "https://example.com/a",
        "quote": "a quote long enough to be real",
        "kind": "general",
        "gold_verdict": "supported",
        "gold_sources": ["https://island-wire.example/x"],
        "notes": "",
    }
    path = tmp_path / "bad.jsonl"
    path.write_text(
        json.dumps({"_fictional": "Invented."}) + "\n" + json.dumps({**base, **row}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(EvalError, match=message):
        load_golden(path)


def test_a_duplicate_claim_id_is_rejected(tmp_path: Path) -> None:
    row = {
        "id": "x1",
        "article_id": "a",
        "article_url": "https://example.com/a",
        "quote": "a quote long enough to be real",
        "kind": "general",
        "gold_verdict": "unverifiable",
        "gold_sources": [],
        "notes": "",
    }
    path = tmp_path / "dup.jsonl"
    path.write_text(
        "\n".join(
            [json.dumps({"_fictional": "Invented."}), json.dumps(row), json.dumps(row)]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(EvalError, match="duplicate"):
        load_golden(path)


def test_the_golden_set_covers_all_four_verdicts_and_the_hard_cases() -> None:
    golden = load_golden(DEFAULT_GOLDEN)
    verdicts = {item.gold_verdict for item in golden}
    assert verdicts == set(VERDICTS), "every verdict must appear, or a column is untested"
    assert len(golden) >= 30
    kinds = {item.kind for item in golden}
    assert kinds == {"attribution", "numeric", "general"}
    joined = " ".join(item.notes.lower() for item in golden)
    for case in ("misquotation", "wire", "true but misleading", "known miss"):
        assert case in joined, f"the golden set should document its {case} case"


def test_every_golden_claim_has_a_recording() -> None:
    """Otherwise the offline run raises rather than quietly scoring fewer claims."""
    golden = load_golden(DEFAULT_GOLDEN)
    fixtures = load_fixtures(DEFAULT_FIXTURES)
    missing = sorted({item.id for item in golden} - set(fixtures))
    assert missing == []
    orphans = sorted(set(fixtures) - {item.id for item in golden})
    assert orphans == [], "a recording for a claim nobody evaluates is dead weight"


def test_every_url_in_the_golden_set_and_its_recordings_is_unmistakably_fake() -> None:
    """Nothing here may resolve to, or be mistaken for, a real story.

    `example.com` and the `.example` TLD are reserved by RFC 2606/6761 precisely
    so that invented data cannot point at somebody's real site.
    """
    urls: list[str] = []
    for item in load_golden(DEFAULT_GOLDEN):
        urls.append(item.article_url)
        urls.extend(item.gold_sources)
    for fixture in load_fixtures(DEFAULT_FIXTURES).values():
        urls.extend(passage.url for passage in fixture.passages())

    assert urls, "nothing was checked"
    for url in urls:
        host = url.split("/")[2]
        assert (
            host.endswith(".example")
            or host == "example.com"
            or host.endswith(".example.com")
        ), f"{url} is not obviously fictional"


# ---------------------------------------------------------------- the replay seam


def test_stance_indices_come_from_the_message_the_stage_built() -> None:
    """The fixture never predicts retrieval's ordering; it reads it back out.

    Recording a stance against a hard-coded index would mis-align silently the
    day ranking changed. Here the same two passages are numbered the other way
    round and the answer follows them.
    """
    stances = {"alpha text": (run_eval.Stance.refutes, "alpha"), "beta text": (
        run_eval.Stance.supports,
        "beta",
    )}
    user = (
        '<claim>\nq\n</claim>\n\n'
        '<passage index="1">\nbeta text\n</passage>\n\n'
        '<passage index="2">\nalpha text\n</passage>'
    )
    scores = json.loads(stance_recording(user, stances))["scores"]
    assert scores == [
        {"index": 1, "stance": "supports", "quote": "beta"},
        {"index": 2, "stance": "refutes", "quote": "alpha"},
    ]


def test_an_unrecorded_passage_is_scored_neutral_rather_than_guessed() -> None:
    user = '<passage index="1">\nsomething nobody recorded\n</passage>'
    scores = json.loads(stance_recording(user, {}))["scores"]
    assert scores == [{"index": 1, "stance": "neutral", "quote": ""}]


def test_the_transport_refuses_to_invent_a_judge_answer() -> None:
    """A hole in a fixture must stop the run, not become a number in the report."""
    fixture = run_eval.ClaimFixture(
        claim_id="t1", buckets={}, stances={}, judge=None
    )
    transport = FixtureTransport(fixture)
    with pytest.raises(EvalError, match="records no `judge` answer"):
        _run(
            transport.complete(
                model="m",
                system="s",
                user="u",
                schema_name="JudgeResponse",
                json_schema={},
                timeout=1.0,
            )
        )


def test_the_transport_refuses_an_unknown_schema() -> None:
    """A stage that starts asking something new must be recorded, not defaulted."""
    fixture = run_eval.ClaimFixture(claim_id="t1", buckets={}, stances={}, judge=None)
    with pytest.raises(EvalError, match="no recording for schema"):
        _run(
            FixtureTransport(fixture).complete(
                model="m",
                system="s",
                user="u",
                schema_name="SomethingNew",
                json_schema={},
                timeout=1.0,
            )
        )


def _run(coro: Any) -> Any:
    """Drive one coroutine to completion without depending on an asyncio plugin."""
    import asyncio

    return asyncio.run(coro)


def test_a_claimreview_hit_short_circuits_web_search() -> None:
    """The cost rule, checked end to end on a golden claim that has both.

    `otter-02` records a web-search passage as well as a fact-check one. If the
    short-circuit ever broke, the web passage's domain would appear among the
    claim's sources.
    """
    fixtures = load_fixtures(DEFAULT_FIXTURES)
    fixture = fixtures["otter-02"]
    assert fixture.buckets["factcheck"], "the fixture must have a ClaimReview hit"
    assert fixture.buckets["web"], "and a web passage that must never be reached"

    golden = {item.id: item for item in load_golden(DEFAULT_GOLDEN)}["otter-02"]
    deps = run_eval.offline_deps(fixture, run_eval.eval_settings(live=False))
    claim = _run(
        run_eval.check_claim(
            golden.extracted(),
            article_url=golden.article_url,
            settings=run_eval.eval_settings(live=False),
            deps=deps,
        )
    )
    domains = {run_eval.domain_of(source["url"]) for source in claim["sources"]}
    assert domains == {"checkpoint-sg.example"}
    assert claim["verdict"] == "contradicted"


def test_a_claim_with_no_recorded_evidence_costs_no_model_call() -> None:
    """No passages means no stance call and no judge call — the cost rule, checked."""
    fixtures = load_fixtures(DEFAULT_FIXTURES)
    fixture = fixtures["hawker-02"]
    assert fixture.passages() == ()
    transport = FixtureTransport(fixture)
    deps = run_eval.PipelineDeps(
        llm=run_eval.LLMClient(
            api_key="unused", timeout=5.0, max_retries=0, transport=transport
        ),
        providers=run_eval.offline_deps(fixture, run_eval.eval_settings(live=False)).providers,
    )
    golden = {item.id: item for item in load_golden(DEFAULT_GOLDEN)}["hawker-02"]
    claim = _run(
        run_eval.check_claim(
            golden.extracted(),
            article_url=golden.article_url,
            settings=run_eval.eval_settings(live=False),
            deps=deps,
        )
    )
    assert transport.calls == []
    assert claim["verdict"] == "unverifiable"
    assert claim["sources"] == []
    assert claim["confidence"] is None
    assert claim["evidence"].strip()


def test_every_claim_the_harness_produces_obeys_the_product_invariants() -> None:
    """The two rules that are relationships between fields, on real output.

    `aggregate` already enforces them, which is exactly why the eval should check
    them independently: a harness that only ever asserts what the code asserts
    cannot notice the code being wrong about it.
    """
    from app.invariants import validate_claim

    outcomes = _run(
        run_eval.run_golden_set(
            load_golden(DEFAULT_GOLDEN),
            live=False,
            fixtures=load_fixtures(DEFAULT_FIXTURES),
            settings=run_eval.eval_settings(live=False),
        )
    )
    assert len(outcomes) == 32
    for outcome in outcomes:
        assert outcome.claim is not None, outcome.error
        validate_claim(outcome.claim)
        assert outcome.claim["verdict"] in VERDICTS


# ---------------------------------------------------------------- cannot-run paths


def test_live_without_a_key_says_so_in_one_line_and_exits_two(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 2 means "nothing was measured", which is not the same as a failed gate."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert main(["--live", "--no-json"]) == run_eval.CANNOT_RUN
    captured = capsys.readouterr()
    assert "eval could not run" in captured.err
    assert "OPENAI_API_KEY" in captured.err
    assert "Traceback" not in captured.err


def test_a_missing_recording_stops_the_run_rather_than_scoring_fewer_claims(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    golden, _ = _write_case(tmp_path, "contradicted")
    empty = tmp_path / "empty"
    empty.mkdir()
    assert (
        main(["--offline", "--no-json", "--golden", str(golden), "--fixtures", str(empty)])
        == run_eval.CANNOT_RUN
    )
    assert "has no recording" in capsys.readouterr().err


def test_an_offline_report_states_no_cost_rather_than_a_bill_of_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A replay's token counts are zeros; printing them would read as a real bill."""
    assert main(["--offline", "--no-json"]) == 0
    assert "llm calls" not in capsys.readouterr().out
    report = build_report(
        [_outcome("a", "supported", "supported")], mode="offline", golden_path=Path("g")
    )
    assert report.cost is None
    assert report.to_json()["cost"] is None


def test_a_cost_block_is_printed_when_there_is_one() -> None:
    report = build_report(
        [_outcome("a", "supported", "supported")],
        mode="live",
        golden_path=Path("g"),
        cost={"calls": 64, "prompt_tokens": 41000, "completion_tokens": 3100},
    )
    text = format_report(report)
    assert "llm calls         64" in text
    assert "41000 prompt + 3100 completion tokens" in text
    assert report.to_json()["cost"]["calls"] == 64
