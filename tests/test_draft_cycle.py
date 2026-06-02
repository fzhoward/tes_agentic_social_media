"""Tests for agents/draft_cycle.py — the front-half revision loop.

All tests are deterministic and run without subprocesses, API calls, or sheet
writes. They drive the loop through its injectable `run_agent` seam with a fake
that returns SCRIPTED (exit_code, json) tuples in sequence — the same testing
discipline `agents.critic.evaluate_draft` uses for its injectable `llm_call`.

Run from the project root:
    python -m pytest tests/test_draft_cycle.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CONFIG_FILE = PROJECT_ROOT / "business_config_tes_rentals.yaml"
os.environ.setdefault("BUSINESS_CONFIG_PATH", str(CONFIG_FILE))

from agents import draft_cycle, drafter  # noqa: E402
from tools.config_loader import Config  # noqa: E402


# --- Scripted-runner fakes (the injectable `run_agent` seam) ------------------

def _drafter_ok() -> tuple[int, dict]:
    """A successful Drafter CLI result."""
    return 0, {"status": "success", "row_id": "X", "media_format_used": "img"}


def _critic(verdict: str) -> tuple[int, dict]:
    """A successful Critic CLI result carrying `verdict` and a critic_output.

    `critic_output` is always a dict so a soft_fail produces a real tempfile
    handoff (the loop only writes a previous-output file for dict payloads).
    """
    return 0, {
        "status": "success",
        "verdict": verdict,
        "critic_output": {"verdict": verdict, "failures": [], "warnings": []},
    }


class SequenceRunner:
    """Fake `run_agent` that returns a flat list of responses in call order.

    Records every call's argv plus a snapshot of any `--previous-output` path
    and whether it existed at call time, so tests can assert both argument
    plumbing and tempfile lifecycle.
    """

    def __init__(self, responses: list[tuple[int, object]]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, argv: list[str]) -> tuple[int, object]:
        prev = None
        if "--previous-output" in argv:
            prev = argv[argv.index("--previous-output") + 1]
        self.calls.append({
            "argv": list(argv),
            "agent": argv[0],
            "previous_output": prev,
            "previous_output_exists": os.path.exists(prev) if prev else None,
        })
        if not self._responses:
            raise AssertionError(f"unexpected extra agent call: {argv}")
        return self._responses.pop(0)

    @property
    def n_calls(self) -> int:
        return len(self.calls)


class PerRowRunner:
    """Fake `run_agent` that scripts responses keyed by the row_id in argv."""

    def __init__(self, scripts: dict[str, list[tuple[int, object]]]) -> None:
        self._scripts = {k: list(v) for k, v in scripts.items()}
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, object]:
        self.calls.append(list(argv))
        rid = argv[argv.index("--row-id") + 1]
        return self._scripts[rid].pop(0)


# --- Terminal-outcome paths ---------------------------------------------------

def test_clean_pass_round_1() -> None:
    """1. Drafter ok -> Critic pass: passed in one round, no spurious revision."""
    runner = SequenceRunner([_drafter_ok(), _critic("pass")])
    result = draft_cycle.run_row_cycle("R1", run_agent=runner)

    assert result["outcome"] == "passed"
    assert result["rounds_run"] == 1
    assert result["final_verdict"] == "pass"
    # Exactly one Drafter + one Critic call — no revision round.
    assert runner.n_calls == 2
    assert runner.calls[0]["agent"] == "agents.drafter"
    assert runner.calls[1]["agent"] == "agents.critic"


def test_soft_fail_then_pass() -> None:
    """2. soft_fail -> revise -> pass; round-2 Drafter gets --previous-output."""
    runner = SequenceRunner([
        _drafter_ok(), _critic("soft_fail"),   # round 1
        _drafter_ok(), _critic("pass"),        # round 2
    ])
    result = draft_cycle.run_row_cycle("R2", run_agent=runner)

    assert result["outcome"] == "passed"
    assert result["rounds_run"] == 2
    assert runner.n_calls == 4

    # Round-1 Drafter (call 0) must NOT carry --previous-output.
    assert "--previous-output" not in runner.calls[0]["argv"]
    # Round-2 Drafter (call 2) MUST carry a --previous-output path that existed
    # at the moment of the call, plus the revision-round marker.
    round2_drafter = runner.calls[2]
    assert round2_drafter["agent"] == "agents.drafter"
    assert round2_drafter["previous_output"] is not None
    assert round2_drafter["previous_output_exists"] is True
    assert "--revision-round" in round2_drafter["argv"]


def test_hard_fail_round_1() -> None:
    """3. Critic hard_fail immediately: hard_failed, no revision attempted."""
    runner = SequenceRunner([_drafter_ok(), _critic("hard_fail")])
    result = draft_cycle.run_row_cycle("R3", run_agent=runner)

    assert result["outcome"] == "hard_failed"
    assert result["rounds_run"] == 1
    assert result["final_verdict"] == "hard_fail"
    assert runner.n_calls == 2  # no revision round


def test_soft_fail_x3_then_escalation() -> None:
    """4. soft_fail rounds 1-2, hard_fail round 3 (Critic's own escalation)."""
    runner = SequenceRunner([
        _drafter_ok(), _critic("soft_fail"),   # round 1
        _drafter_ok(), _critic("soft_fail"),   # round 2
        _drafter_ok(), _critic("hard_fail"),   # round 3 escalation
    ])
    result = draft_cycle.run_row_cycle("R4", run_agent=runner)

    assert result["outcome"] == "hard_failed"
    assert result["rounds_run"] == 3
    assert result["final_verdict"] == "hard_fail"
    assert runner.n_calls == 6


def test_safety_cap_escalated_cap() -> None:
    """5. Critic never terminal (contract violation): loop stops at cap."""
    # All three rounds soft_fail — the Critic's own round-3 escalation is
    # simulated absent, proving the belt-and-suspenders MAX_REVISION_ROUNDS cap.
    runner = SequenceRunner([
        _drafter_ok(), _critic("soft_fail"),
        _drafter_ok(), _critic("soft_fail"),
        _drafter_ok(), _critic("soft_fail"),
    ])
    result = draft_cycle.run_row_cycle("R5", run_agent=runner)

    assert result["outcome"] == "escalated_cap"
    assert result["rounds_run"] == draft_cycle.MAX_REVISION_ROUNDS
    assert runner.n_calls == 2 * draft_cycle.MAX_REVISION_ROUNDS
    assert "MAX_REVISION_ROUNDS" in result.get("error", "")


# --- Defensive paths ----------------------------------------------------------

def test_drafter_nonzero_exit_is_error() -> None:
    """6a. Non-zero Drafter exit -> outcome error; loop does not raise."""
    runner = SequenceRunner([(1, {"status": "error", "error": "boom"})])
    result = draft_cycle.run_row_cycle("R6", run_agent=runner)

    assert result["outcome"] == "error"
    assert "drafter" in result["error"]
    assert runner.n_calls == 1  # bailed before calling the Critic


def test_critic_non_json_stdout_is_error() -> None:
    """6b. Critic returns non-JSON text (a str) -> outcome error, no raise."""
    runner = SequenceRunner([_drafter_ok(), (0, "not json at all")])
    result = draft_cycle.run_row_cycle("R6b", run_agent=runner)

    assert result["outcome"] == "error"
    assert "critic" in result["error"]


def test_tempfile_cleaned_up_after_run() -> None:
    """7. The previous-output tempfile is created, passed, then removed."""
    runner = SequenceRunner([
        _drafter_ok(), _critic("soft_fail"),   # round 1 -> writes tempfile
        _drafter_ok(), _critic("pass"),        # round 2 consumes it
    ])
    result = draft_cycle.run_row_cycle("R7", run_agent=runner)
    assert result["outcome"] == "passed"

    # The path handed to round-2 sub-agents existed during the call...
    prev_path = runner.calls[2]["previous_output"]
    assert prev_path is not None
    assert runner.calls[2]["previous_output_exists"] is True
    # ...and was cleaned up by the loop's finally block.
    assert not os.path.exists(prev_path)


def test_tempfile_cleaned_up_even_on_later_error() -> None:
    """7b. Cleanup runs even when a later round errors out."""
    runner = SequenceRunner([
        _drafter_ok(), _critic("soft_fail"),       # round 1 -> writes tempfile
        (1, {"status": "error", "error": "x"}),    # round 2 Drafter errors
    ])
    result = draft_cycle.run_row_cycle("R7b", run_agent=runner)
    assert result["outcome"] == "error"

    prev_path = runner.calls[2]["previous_output"]
    assert prev_path is not None
    assert not os.path.exists(prev_path)  # finally still cleaned up


def test_dry_run_skip_reclassified() -> None:
    """8. In dry_run, a Critic `skipped` -> dry_run_skipped (NOT error)."""
    skipped = (0, {"status": "skipped", "reason": "status 'planned'"})
    runner = SequenceRunner([_drafter_ok(), skipped])
    result = draft_cycle.run_row_cycle("R8", run_agent=runner, dry_run=True)

    assert result["outcome"] == "dry_run_skipped"
    assert "error" not in result  # not recorded as an error


def test_skip_outside_dry_run_is_error() -> None:
    """8b. The SAME `skipped` result on a live run is still an error."""
    skipped = (0, {"status": "skipped", "reason": "status 'planned'"})
    runner = SequenceRunner([_drafter_ok(), skipped])
    result = draft_cycle.run_row_cycle("R8b", run_agent=runner, dry_run=False)

    assert result["outcome"] == "error"


# --- Cycle-level aggregation --------------------------------------------------

def test_mixed_batch_aggregation(monkeypatch) -> None:
    """9. run_cycle over passed/hard_failed/error rows aggregates correctly."""
    monkeypatch.setattr(
        draft_cycle, "load_config", lambda: Config({}),
    )
    monkeypatch.setattr(
        draft_cycle, "select_target_rows",
        lambda config, **kw: ["R-PASS", "R-HARD", "R-ERR"],
    )
    runner = PerRowRunner({
        "R-PASS": [_drafter_ok(), _critic("pass")],
        "R-HARD": [_drafter_ok(), _critic("hard_fail")],
        "R-ERR": [(1, {"status": "error", "error": "boom"})],
    })
    result = draft_cycle.run_cycle(run_agent=runner)

    assert result["status"] == "success"
    assert result["rows_processed"] == 3
    assert result["dry_run"] is False
    assert len(result["rows"]) == 3
    assert result["outcomes"] == {
        "passed": 1,
        "hard_failed": 1,
        "escalated_cap": 0,
        "error": 1,
        "dry_run_skipped": 0,
    }


def test_dry_run_skipped_lands_in_aggregate_bucket(monkeypatch) -> None:
    """9b. A dry-run skip is counted in the dry_run_skipped bucket."""
    monkeypatch.setattr(draft_cycle, "load_config", lambda: Config({}))
    monkeypatch.setattr(
        draft_cycle, "select_target_rows",
        lambda config, **kw: ["R-SKIP"],
    )
    runner = PerRowRunner({
        "R-SKIP": [_drafter_ok(), (0, {"status": "skipped", "reason": "x"})],
    })
    result = draft_cycle.run_cycle(run_agent=runner, dry_run=True)

    assert result["dry_run"] is True
    assert result["outcomes"]["dry_run_skipped"] == 1
    assert result["outcomes"]["error"] == 0


# --- Argument plumbing --------------------------------------------------------

def test_dry_run_threads_to_both_subagents() -> None:
    """10a. --dry-run reaches BOTH the Drafter and the Critic invocations."""
    runner = SequenceRunner([_drafter_ok(), _critic("pass")])
    result = draft_cycle.run_row_cycle("R10", run_agent=runner, dry_run=True)

    assert result["outcome"] == "passed"
    assert "--dry-run" in runner.calls[0]["argv"]  # Drafter
    assert "--dry-run" in runner.calls[1]["argv"]  # Critic
    # Both carry the row id.
    for call in runner.calls:
        assert call["argv"][call["argv"].index("--row-id") + 1] == "R10"


def test_select_target_rows_row_id_short_circuits() -> None:
    """10b. --row-id selects exactly that row without touching sheets."""
    ids = draft_cycle.select_target_rows(Config({}), row_id="STR-99")
    assert ids == ["STR-99"]


def test_select_target_rows_limit_truncates(monkeypatch) -> None:
    """10c. --limit caps the all-planned set (no window math duplicated)."""
    planned = [
        {drafter.CQ_ROW_ID: "A"},
        {drafter.CQ_ROW_ID: "B"},
        {drafter.CQ_ROW_ID: "C"},
    ]
    monkeypatch.setattr(
        drafter, "load_drafter_context", lambda config: {"queue_rows": planned},
    )
    monkeypatch.setattr(
        drafter, "find_planned_rows_in_window", lambda rows, lead: planned,
    )
    ids = draft_cycle.select_target_rows(Config({}), limit=2)
    assert ids == ["A", "B"]
