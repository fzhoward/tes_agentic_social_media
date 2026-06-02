"""Draft-cycle agent — front-half orchestrator for the revision loop.

Runs the stateful Drafter -> Critic -> revision loop for one or more `planned`
Content Queue rows, driving each row to a terminal state
(`awaiting_approval` via a `pass`, or `hard_fail`) within a single invocation,
then exits. This is the Python half of the hybrid (Option C) architecture:
Make schedules the poke, this module owns the loop.

Design constraints (Session 35, decided with the owner — do NOT relitigate):
    - The executor stays a dumb runner. The revision loop lives here, not in
      tools/executor.py. The executor shells out to this module like any other
      endpoint.
    - Sub-agents are invoked through their PROVEN CLIs (`-m agents.drafter`,
      `-m agents.critic`) — never by calling their internals — so the contract
      that has been tested in isolation is preserved.
    - Tempfile handoff (Option A): the captured Critic output is written to a
      temp JSON file and passed to the re-Drafter (and the re-Critic) on their
      existing `--previous-output` flag.
    - Fresh-only (Option A): one invocation processes `planned` rows only and
      runs each to a terminal state. Resuming interrupted rows is out of scope.

The revision loop is naturally bounded by the Critic's existing escalation
(`determine_verdict` escalates a round>=3 soft_fail to hard_fail). MAX_REVISION
_ROUNDS is a belt-and-suspenders cap so the loop can never spin forever even if
that escalation contract changes.

Testability:
    `run_row_cycle` is a pure function whose only dependency on the outside
    world is an injectable `run_agent` callable (same signature as
    `_run_agent_cli`). Tests drive the loop with a fake `run_agent` that
    returns scripted verdict sequences — no subprocesses, no API calls. This
    mirrors how `agents.critic.evaluate_draft` takes an injectable `llm_call`.

Usage:
    python -m agents.draft_cycle [--dry-run] [--limit N]
    python -m agents.draft_cycle --row-id STR-20260527-FB-01 [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from agents import drafter
from tools.config_loader import Config, load_config


# --- Constants ---

# Repo root is the parent of this file's directory (agents/draft_cycle.py).
REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

# Hard safety cap on revision rounds. The Critic escalates a round>=3 soft_fail
# to hard_fail on its own, so this cap is a backstop, not the primary terminator.
MAX_REVISION_ROUNDS: int = 3

# Per-sub-agent subprocess timeout. The Drafter can spend minutes on media
# generation + Creatomate polling, so this is generous. The whole cycle runs in
# a background thread on the executor with its own (much larger) cap.
_AGENT_TIMEOUT: int = 60 * 10  # 10 minutes per sub-agent invocation

# Type alias for the injectable runner seam.
RunAgent = Callable[[list[str]], "tuple[int, Any]"]


# --- Sub-agent invocation ---

def _python() -> str:
    """Return the interpreter to run the sub-agent CLIs with.

    Prefers the repo venv; falls back to the current interpreter so the loop
    still works when launched from an already-activated environment. Mirrors
    tools/executor.py's `_python`.
    """
    return str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable


def _run_agent_cli(args: list[str]) -> tuple[int, Any]:
    """Run a sub-agent CLI module and return (exit_code, parsed_json_or_text).

    Uses the SAME interpreter/cwd/env discipline as tools/executor.py: runs
    `-m <module> ...` from REPO_ROOT with BUSINESS_CONFIG_PATH set so the
    sub-agents resolve config exactly as they do when invoked by hand.

    Non-JSON stdout or a non-zero exit is surfaced (not raised) so the caller
    can record the failure against the row and continue the cycle.
    """
    env = dict(os.environ)
    env["BUSINESS_CONFIG_PATH"] = os.environ.get(
        "BUSINESS_CONFIG_PATH", "business_config_tes_rentals.yaml"
    )
    try:
        proc = subprocess.run(
            [_python(), "-m", *args],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=_AGENT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return 124, {"status": "error", "error": "sub-agent timed out"}

    stdout = (proc.stdout or "").strip()
    if stdout:
        try:
            return proc.returncode, json.loads(stdout)
        except ValueError:
            # Sub-agent printed non-JSON (shouldn't happen) — don't crash.
            return proc.returncode, stdout
    # No stdout — surface stderr for debugging.
    return proc.returncode, {
        "status": "error",
        "error": (proc.stderr or "").strip() or "sub-agent produced no output",
    }


# --- CLI argument assembly (kept separate so the loop reads cleanly) ---

def _drafter_args(
    row_id: str,
    revision_round: int,
    previous_output_path: str | None,
    dry_run: bool,
) -> list[str]:
    """Build the `-m agents.drafter ...` argv for one draft round.

    Round 1 passes neither --revision-round nor --previous-output (the Drafter
    CLI forbids --previous-output on round 1). Rounds > 1 pass both, per the
    Drafter's revision contract.
    """
    args = ["agents.drafter", "--row-id", row_id]
    if revision_round > 1:
        args += ["--revision-round", str(revision_round)]
        if previous_output_path:
            args += ["--previous-output", previous_output_path]
    if dry_run:
        args.append("--dry-run")
    return args


def _critic_args(
    row_id: str,
    revision_round: int,
    previous_output_path: str | None,
    dry_run: bool,
) -> list[str]:
    """Build the `-m agents.critic ...` argv for one critique round.

    The Critic accepts --previous-output for comparison on rounds > 1; round 1
    passes neither flag.
    """
    args = ["agents.critic", "--row-id", row_id]
    if revision_round > 1:
        args += ["--revision-round", str(revision_round)]
        if previous_output_path:
            args += ["--previous-output", previous_output_path]
    if dry_run:
        args.append("--dry-run")
    return args


def _agent_ok(code: int, result: Any) -> bool:
    """True if a sub-agent CLI call succeeded (exit 0, JSON, status success)."""
    return (
        code == 0
        and isinstance(result, dict)
        and result.get("status") == "success"
    )


def _is_skipped(result: Any) -> bool:
    """True if a sub-agent returned its recognized `skipped` shape.

    Under --dry-run the Drafter does not write `status=drafted`, so the Critic
    legitimately returns `{"status": "skipped", ...}` (a distinct, recognized
    result — NOT an error). This is used to reclassify that case as
    `dry_run_skipped` instead of `error`. Outside dry-run an unexpected skip is
    still treated as an error.
    """
    return isinstance(result, dict) and result.get("status") == "skipped"


def _row_result(
    row_id: str,
    outcome: str,
    rounds_run: int,
    final_verdict: str | None,
    rounds: list[dict],
    error: str | None = None,
) -> dict:
    """Assemble the per-row result dict in a consistent shape."""
    out: dict[str, Any] = {
        "row_id": row_id,
        "outcome": outcome,
        "rounds_run": rounds_run,
        "final_verdict": final_verdict,
        "rounds": rounds,
    }
    if error is not None:
        out["error"] = error
    return out


# --- The revision loop (PURE — only touches the world through `run_agent`) ---

def run_row_cycle(
    row_id: str,
    *,
    run_agent: RunAgent,
    max_rounds: int = MAX_REVISION_ROUNDS,
    dry_run: bool = False,
) -> dict:
    """Run the Drafter -> Critic -> revision loop for a single row.

    `run_agent` is an injectable callable with the same signature as
    `_run_agent_cli` — `(argv: list[str]) -> (exit_code, parsed_json)`. In
    production it is `_run_agent_cli`; tests pass a fake that returns scripted
    `(code, json)` tuples to exercise pass / soft_fail->pass / soft_fail x3 ->
    hard_fail / error sequences without subprocesses.

    Terminal outcomes:
        passed        — Critic returned `pass` (row now awaiting_approval).
        hard_failed   — Critic returned `hard_fail` (terminal; includes the
                        round-3 soft_fail escalation).
        escalated_cap — the safety cap was hit without a terminal verdict
                        (only reachable if the Critic's escalation contract
                        changes); surfaced for visibility.
        error         — a sub-agent call failed (non-zero exit, non-JSON, or a
                        non-success status); the row is abandoned, the cycle
                        continues with the next row.
        dry_run_skipped — under --dry-run the Drafter does not write
                        `status=drafted`, so the Critic returns its recognized
                        `skipped` shape. That is correct behavior, not an error,
                        so it is recorded distinctly. (Only in dry-run; an
                        unexpected skip on a live run is still `error`.)

    Terminal outcome set: passed | hard_failed | escalated_cap | error |
    dry_run_skipped.

    Tempfiles holding each round's Critic output are removed in a `finally`
    block so they are cleaned up even on error.
    """
    rounds: list[dict] = []
    final_verdict: str | None = None
    rounds_run = 0
    previous_output_path: str | None = None
    tmp_paths: list[str] = []

    try:
        for rnd in range(1, max_rounds + 1):
            rounds_run = rnd

            # --- Draft round N ---
            d_code, d_result = run_agent(
                _drafter_args(row_id, rnd, previous_output_path, dry_run)
            )
            if not _agent_ok(d_code, d_result):
                if dry_run and _is_skipped(d_result):
                    return _row_result(
                        row_id, "dry_run_skipped", rounds_run,
                        final_verdict, rounds,
                    )
                return _row_result(
                    row_id, "error", rounds_run, final_verdict, rounds,
                    error=_describe_failure("drafter", rnd, d_code, d_result),
                )

            # --- Critique round N ---
            c_code, c_result = run_agent(
                _critic_args(row_id, rnd, previous_output_path, dry_run)
            )
            if not _agent_ok(c_code, c_result):
                if dry_run and _is_skipped(c_result):
                    return _row_result(
                        row_id, "dry_run_skipped", rounds_run,
                        final_verdict, rounds,
                    )
                return _row_result(
                    row_id, "error", rounds_run, final_verdict, rounds,
                    error=_describe_failure("critic", rnd, c_code, c_result),
                )

            verdict = c_result.get("verdict")
            final_verdict = verdict
            rounds.append({
                "round": rnd,
                "drafter_status": d_result.get("status"),
                "critic_status": c_result.get("status"),
                "verdict": verdict,
            })

            if verdict == "pass":
                return _row_result(
                    row_id, "passed", rounds_run, final_verdict, rounds,
                )
            if verdict == "hard_fail":
                return _row_result(
                    row_id, "hard_failed", rounds_run, final_verdict, rounds,
                )
            if verdict == "soft_fail":
                # Revision needed — hand the Critic output to the next round
                # via a tempfile on --previous-output. Both the re-Drafter and
                # the re-Critic read the same file.
                critic_output = c_result.get("critic_output")
                previous_output_path = _write_previous_output(critic_output)
                if previous_output_path is not None:
                    tmp_paths.append(previous_output_path)
                continue

            # Unknown / missing verdict — defensive. Treat as a row error.
            return _row_result(
                row_id, "error", rounds_run, final_verdict, rounds,
                error=f"critic returned unrecognized verdict {verdict!r} "
                      f"on round {rnd}",
            )

        # Loop exhausted without a terminal verdict — only reachable if the
        # Critic stops escalating round-3 soft_fails. Surface for visibility.
        return _row_result(
            row_id, "escalated_cap", rounds_run, final_verdict, rounds,
            error=f"hit MAX_REVISION_ROUNDS={max_rounds} without a terminal "
                  f"verdict (last verdict {final_verdict!r})",
        )
    finally:
        for path in tmp_paths:
            try:
                os.unlink(path)
            except OSError:
                pass


def _describe_failure(agent: str, rnd: int, code: int, result: Any) -> str:
    """Build a compact error string for a failed sub-agent call."""
    detail = ""
    if isinstance(result, dict):
        detail = str(result.get("error") or result.get("reason") or "")
        if not detail and result.get("status"):
            detail = f"status={result.get('status')!r}"
    elif isinstance(result, str):
        detail = result[:200]
    return (
        f"{agent} failed on round {rnd} (exit_code={code})"
        + (f": {detail}" if detail else "")
    )


def _write_previous_output(critic_output: Any) -> str | None:
    """Write the captured Critic output JSON to a tempfile; return its path.

    Returns None when there is nothing to write (defensive — a soft_fail
    without a `critic_output` payload should not happen, but the loop must not
    crash if it does; the next round simply runs without --previous-output).
    """
    if not isinstance(critic_output, dict):
        return None
    fd, path = tempfile.mkstemp(prefix="draft_cycle_prev_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(critic_output, f, ensure_ascii=False)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    return path


# --- Row selection (fresh-only; mirrors the Drafter's --all-planned set) ---

def select_target_rows(
    config: Config,
    *,
    row_id: str | None = None,
    limit: int | None = None,
) -> list[str]:
    """Return the list of row_ids the cycle should process.

    `--row-id` selects exactly that row. Otherwise the default (fresh-only)
    mode mirrors the Drafter's `--all-planned` selection EXACTLY by reusing
    `drafter.find_planned_rows_in_window` over the same lead-time window, so
    draft_cycle and the Drafter agree on the set. No window math is duplicated.
    """
    if row_id:
        return [row_id]

    context = drafter.load_drafter_context(config)
    lead_time = int(config.get("strategy.lead_time_hours", 36))
    planned = drafter.find_planned_rows_in_window(
        context["queue_rows"], lead_time,
    )
    ids = [
        str(r.get(drafter.CQ_ROW_ID, "")).strip()
        for r in planned
    ]
    ids = [i for i in ids if i]
    if limit is not None:
        ids = ids[:limit]
    return ids


# --- Cycle driver ---

def run_cycle(
    *,
    row_id: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    run_agent: RunAgent | None = None,
) -> dict:
    """Run the full draft cycle over the selected rows. Returns the cycle result.

    `run_agent` defaults to the real `_run_agent_cli`; it is exposed so the
    whole driver (not just `run_row_cycle`) can be tested with a fake runner.
    """
    runner: RunAgent = run_agent if run_agent is not None else _run_agent_cli
    config = load_config()
    target_ids = select_target_rows(config, row_id=row_id, limit=limit)

    rows: list[dict] = []
    outcomes: dict[str, int] = {
        "passed": 0,
        "hard_failed": 0,
        "escalated_cap": 0,
        "error": 0,
        "dry_run_skipped": 0,
    }
    for rid in target_ids:
        result = run_row_cycle(rid, run_agent=runner, dry_run=dry_run)
        rows.append(result)
        outcome = result.get("outcome", "error")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1

    return {
        "status": "success",
        "rows_processed": len(target_ids),
        "outcomes": outcomes,
        "rows": rows,
        "dry_run": dry_run,
    }


# --- CLI ---

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Draft-cycle orchestrator")
    parser.add_argument(
        "--row-id",
        default=None,
        help=(
            "Run the revision loop on a single Content Queue row by its "
            "row_id (for live testing). Default mode processes all planned "
            "rows in the Drafter's lead-time window."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap on rows processed in default (all-planned) mode (testing).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Thread --dry-run through to the Drafter and Critic so the cycle "
            "can be rehearsed without mutating the Content Queue."
        ),
    )
    args = parser.parse_args(argv)

    try:
        result = run_cycle(
            row_id=args.row_id,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        # The cycle itself could not run (e.g. config/sheet load failed).
        print(json.dumps({
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }, indent=2, default=str))
        return 1

    print(json.dumps(result, indent=2, default=str))
    # Exit 0 when the cycle completed — hard_fails are a normal outcome, not a
    # cycle error. Non-zero only when the cycle itself could not run (above).
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
