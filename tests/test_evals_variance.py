"""Tests for the Critic eval harness (evals/).

Dry-mode only — no OpenAI calls, no network, no sheets. Covers:

* flip_score boundary values (0/1 → 0.0, 0.5 → 1.0, symmetry).
* per-check tally arithmetic on hand-constructed outputs.
* aggregate_fixture + rank_checks_worst_first ordering rules.
* JSON-payload serialisation round-trips and disk-write to a tmp_path.
* The dry-mode runner end-to-end: every fixture, K runs, the noise
  profile shows up in the per-check stats with the expected shape.

Run from the project root:
    python -m pytest tests/test_evals_variance.py -v
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CONFIG_FILE = PROJECT_ROOT / "business_config_tes_rentals.yaml"
os.environ.setdefault("BUSINESS_CONFIG_PATH", str(CONFIG_FILE))

from agents import critic  # noqa: E402
from evals import fixtures, report, runner, variance  # noqa: E402


# --- flip_score boundary values --------------------------------------------

def test_flip_score_unanimous_pass_is_zero() -> None:
    """0/10 fails should be perfectly stable."""
    assert variance.flip_score(0.0) == 0.0


def test_flip_score_unanimous_fail_is_zero() -> None:
    """10/10 fails should be perfectly stable too."""
    assert variance.flip_score(1.0) == 0.0


def test_flip_score_fifty_fifty_is_maximal() -> None:
    """5/10 fails is pure coin flip — flip_score must be 1.0."""
    assert variance.flip_score(0.5) == 1.0


def test_flip_score_symmetric_around_half() -> None:
    """flip_score(p) must equal flip_score(1-p)."""
    for p in (0.1, 0.25, 0.3, 0.4):
        assert variance.flip_score(p) == pytest.approx(
            variance.flip_score(1.0 - p),
        )


def test_flip_score_linear_in_each_half() -> None:
    """0.1 -> 0.2, 0.2 -> 0.4, 0.3 -> 0.6, 0.4 -> 0.8."""
    assert variance.flip_score(0.1) == pytest.approx(0.2)
    assert variance.flip_score(0.2) == pytest.approx(0.4)
    assert variance.flip_score(0.3) == pytest.approx(0.6)
    assert variance.flip_score(0.4) == pytest.approx(0.8)


def test_flip_score_rejects_out_of_range() -> None:
    """Negative or >1 inputs should raise — defends against tally bugs."""
    with pytest.raises(ValueError):
        variance.flip_score(-0.01)
    with pytest.raises(ValueError):
        variance.flip_score(1.01)


# --- verdict_flip_score ----------------------------------------------------

def test_verdict_flip_score_unanimous_is_zero() -> None:
    """All 10 runs returning pass -> stable verdict."""
    assert variance.verdict_flip_score({"pass": 10}) == 0.0


def test_verdict_flip_score_split_is_nonzero() -> None:
    """5 pass / 5 hard_fail -> 0.5 (the worst case for a binary split)."""
    assert variance.verdict_flip_score(
        {"pass": 5, "hard_fail": 5},
    ) == pytest.approx(0.5)


def test_verdict_flip_score_empty_is_zero() -> None:
    """No verdicts recorded -> degenerate, but must not blow up."""
    assert variance.verdict_flip_score({}) == 0.0


# --- tally_check -----------------------------------------------------------

def _output(failed: list[str], passed: list[str], warned=None) -> dict:
    """Hand-build a minimal Critic-output dict for tally tests."""
    return {
        "verdict": "soft_fail" if failed else "pass",
        "failed_checks": [
            {"check_id": cid, "verdict_level": "soft_fail"} for cid in failed
        ],
        "passed_checks": list(passed),
        "warnings": [
            {"check_id": cid, "description": "..."} for cid in (warned or [])
        ],
    }


def test_tally_check_counts_fails_and_passes() -> None:
    """5 fails + 5 passes on identical input — the canonical noisy case."""
    outputs = (
        [_output(["C4"], ["C1"])] * 5
        + [_output([], ["C4", "C1"])] * 5
    )
    stats = variance.tally_check("C4", outputs)
    assert stats.runs == 10
    assert stats.fails == 5
    assert stats.passes == 5
    assert stats.warnings == 0
    assert stats.fail_rate == 0.5
    assert stats.flip_score == 1.0
    assert stats.verdict_tier == critic.VERDICT_LEVEL_BY_CHECK["C4"]


def test_tally_check_unanimous_pass_has_zero_flip() -> None:
    outputs = [_output([], ["C4"])] * 10
    stats = variance.tally_check("C4", outputs)
    assert stats.runs == 10
    assert stats.fails == 0
    assert stats.passes == 10
    assert stats.fail_rate == 0.0
    assert stats.flip_score == 0.0


def test_tally_check_unanimous_fail_has_zero_flip() -> None:
    outputs = [_output(["A3"], [])] * 8
    stats = variance.tally_check("A3", outputs)
    assert stats.runs == 8
    assert stats.fails == 8
    assert stats.fail_rate == 1.0
    assert stats.flip_score == 0.0


def test_tally_check_ignores_runs_that_did_not_observe() -> None:
    """A check that the LLM never mentions in a given run does not count.

    If C7 is sometimes routed to warnings and sometimes ignored entirely,
    we want runs to only reflect actual observations.
    """
    outputs = [
        _output([], ["C4"]),         # run sees C4 (pass)
        _output(["C4"], []),         # run sees C4 (fail)
        _output([], []),             # run never mentions C4 — drop it
    ]
    stats = variance.tally_check("C4", outputs)
    assert stats.runs == 2
    assert stats.fails == 1
    assert stats.passes == 1
    assert stats.fail_rate == 0.5
    assert stats.flip_score == 1.0


def test_tally_check_counts_warnings() -> None:
    """C7 is warning-tier — warnings should be tallied separately."""
    outputs = [
        _output([], [], warned=["C7"]),
        _output([], [], warned=["C7"]),
        _output([], ["C7"]),
    ]
    stats = variance.tally_check("C7", outputs)
    assert stats.runs == 3
    assert stats.warnings == 2
    assert stats.passes == 1
    assert stats.fails == 0
    assert stats.verdict_tier == "warning"


# --- aggregate_fixture + ranking ------------------------------------------

def test_aggregate_fixture_records_verdicts() -> None:
    outputs = [
        _output([], ["C1"]),
        _output([], ["C1"]),
        _output(["A1"], []),
    ]
    # Override the second output's verdict so we exercise the verdict
    # histogram explicitly.
    outputs[2]["verdict"] = "hard_fail"

    stats = variance.aggregate_fixture("demo", outputs)
    assert stats.runs == 3
    assert stats.verdicts.get("pass") == 2
    assert stats.verdicts.get("hard_fail") == 1
    # 1/3 of runs disagreed with the modal verdict.
    assert stats.verdict_flip_score == pytest.approx(1.0 - 2.0 / 3.0)


def test_rank_checks_worst_first_sorts_by_flip_then_rate() -> None:
    """Higher flip_score sorts first; ties break on fail_rate then ID."""
    # Build outputs where C4 is 5/5 (flip=1.0), C2 is 7/3 fails (flip=0.6),
    # C1 is unanimous pass (flip=0.0).
    outputs = []
    for i in range(10):
        failed = []
        passed = ["C1"]
        if i < 5:
            failed.append("C4")
        else:
            passed.append("C4")
        if i < 7:
            failed.append("C2")
        else:
            passed.append("C2")
        outputs.append(_output(failed, passed))

    stats = variance.aggregate_fixture(
        "demo", outputs, check_ids=("C1", "C2", "C4"),
    )
    ranked = variance.rank_checks_worst_first(stats)
    assert [s.check_id for s in ranked] == ["C4", "C2", "C1"]


# --- JSON payload ----------------------------------------------------------

def test_build_json_payload_serializes_round_trip() -> None:
    """Payload must serialise to JSON without losing structure."""
    outputs = [_output([], ["C4"])] * 3 + [_output(["C4"], [])] * 7
    stats = variance.aggregate_fixture(
        "noisy", outputs, check_ids=("C4",),
    )
    payload = report.build_json_payload(
        [stats], mode="dry", k=10, seed=42,
    )
    raw = json.dumps(payload)
    restored = json.loads(raw)
    assert restored["mode"] == "dry"
    assert restored["k"] == 10
    assert restored["seed"] == 42
    fixture_blob = restored["fixtures"][0]
    assert fixture_blob["fixture_name"] == "noisy"
    c4 = fixture_blob["checks"]["C4"]
    assert c4["fails"] == 7
    assert c4["passes"] == 3
    assert c4["fail_rate"] == pytest.approx(0.7, rel=1e-3)


def test_write_json_dump_creates_file(tmp_path) -> None:
    """Dump path lands inside the requested directory and parses as JSON."""
    outputs = [_output([], ["C4"])] * 5
    stats = variance.aggregate_fixture(
        "demo", outputs, check_ids=("C4",),
    )
    out_dir = tmp_path / "dump"
    path = report.write_json_dump([stats], out_dir, mode="dry", k=5)
    assert path.parent == out_dir
    assert path.exists()
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert parsed["mode"] == "dry"
    assert parsed["k"] == 5
    assert parsed["fixtures"][0]["fixture_name"] == "demo"


# --- Text report formatting ------------------------------------------------

def test_format_text_report_shows_noisy_checks() -> None:
    """Noisy checks must appear in the table; the header must show K."""
    # 50/50 fail on C4.
    outputs = (
        [_output(["C4"], ["C1"])] * 5
        + [_output([], ["C4", "C1"])] * 5
    )
    stats = variance.aggregate_fixture(
        "demo", outputs, check_ids=("C1", "C4"),
    )
    text = report.format_text_report([stats])
    assert "demo" in text
    assert "K=10" in text
    assert "C4" in text
    # C1 is unanimous pass — should be collapsed into the stable footer.
    assert "stable rows hidden" in text


# --- Dry-mode runner end-to-end --------------------------------------------

def test_dry_runner_uses_evaluate_draft_and_never_raises() -> None:
    """Run all fixtures with K=2 in dry mode; nothing should error."""
    results = runner.run_all(k=2, real=False, seed=0)
    assert set(results.keys()) == {fx.name for fx in fixtures.FIXTURES}
    for fixture_name, run_list in results.items():
        assert len(run_list) == 2, fixture_name
        for r in run_list:
            assert r.error == "", (fixture_name, r.error)
            assert r.output, fixture_name
            assert r.output["verdict"] in (
                "pass", "soft_fail", "hard_fail",
            )


def test_dry_runner_reproduces_with_seed() -> None:
    """Same seed -> identical verdict sequence per fixture."""
    a = runner.run_all(k=4, real=False, seed=123)
    b = runner.run_all(k=4, real=False, seed=123)
    for fixture_name in a:
        seq_a = [r.output["verdict"] for r in a[fixture_name]]
        seq_b = [r.output["verdict"] for r in b[fixture_name]]
        assert seq_a == seq_b, fixture_name


def test_dry_runner_noise_profile_shows_up_for_known_flip_fixture() -> None:
    """The Session-27 fixture is scripted to flip C4; with K=20 it should
    show fail_rate strictly between 0 and 1 — exercising the harness's
    core purpose end-to-end."""
    target = "str_20260602_fb_01"
    results = runner.run_all(
        k=20, real=False,
        fixtures=(fixtures.get_fixture(target),),
        seed=7,
    )
    outputs = [r.output for r in results[target]]
    stats = variance.aggregate_fixture(
        target, outputs, check_ids=("C4",),
    )
    c4 = stats.checks["C4"]
    assert c4.runs == 20
    # With p=0.5 over K=20, fail_rate must be strictly between 0 and 1
    # in the overwhelming majority of seeds; the seed is fixed so this
    # is a deterministic assertion.
    assert 0.0 < c4.fail_rate < 1.0, c4
    assert c4.flip_score > 0.5, c4


def test_dry_runner_clean_fixture_stable_pass() -> None:
    """The clean fixture has an empty noise profile -> every run passes."""
    target = "clean_high_quality"
    results = runner.run_all(
        k=5, real=False,
        fixtures=(fixtures.get_fixture(target),),
        seed=0,
    )
    verdicts = {r.output["verdict"] for r in results[target]}
    # The fake LLM never injects failures for this fixture; deterministic
    # pre-checks on the fixture are clean by construction.
    assert verdicts == {"pass"}, verdicts


def test_dry_runner_does_not_require_openai_key(monkeypatch) -> None:
    """Even with the env var blanked out, dry mode must work."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    results = runner.run_all(k=1, real=False, seed=0)
    assert all(
        all(not r.error for r in run_list)
        for run_list in results.values()
    )


def test_real_mode_requires_openai_key(monkeypatch) -> None:
    """Real mode without a key must raise — guards against accidental spend."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        runner.run_all(k=1, real=True)


# --- Fixture sanity --------------------------------------------------------

def test_fixtures_contain_seed_cases() -> None:
    """The two named cases from the task brief must be present verbatim."""
    names = {fx.name for fx in fixtures.FIXTURES}
    assert "str_20260531_ig_01" in names
    assert "str_20260602_fb_01" in names

    fx1 = fixtures.get_fixture("str_20260531_ig_01")
    assert "John Deere 325G" in fx1.row[critic.CQ_CAPTION]
    assert fx1.row[critic.CQ_FOCUS_EQUIPMENT] == "TES-004"
    assert fx1.catalog_item is not None
    assert fx1.catalog_item[critic.CAT_MODEL] == "John Deere 325G"

    fx2 = fixtures.get_fixture("str_20260602_fb_01")
    assert fx2.row[critic.CQ_FOCUS_EQUIPMENT] == ""
    assert fx2.catalog_item is None
    assert "Zero tail swing" in fx2.row[critic.CQ_CAPTION]
