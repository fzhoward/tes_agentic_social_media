"""Variance math for the Critic eval harness.

Given K Critic outputs on byte-identical input, compute per-check tallies
and stability scores. Pure functions on already-collected data — no I/O,
no LLM calls.

Three per-check metrics matter:

* ``fail_rate`` — the rate at which a check landed in ``failed_checks``
  across K runs. 0.0 means the check never landed there; 1.0 means it
  always did.
* ``flip_score`` — ``2 * min(fail_rate, 1 - fail_rate)``. Zero when the
  fail outcome is unanimous either way (stable). One at a 50/50 split.
  This is the gating-severity signal: it measures noise on the
  ``failed_checks`` channel only, which is what can terminally reject a
  draft. A check demoted to ``warning`` tier no longer lands in
  ``failed_checks``, so its ``flip_score`` collapses to 0 even when it is
  flipping pass<->warning every other run.
* ``warning_rate`` — ``warnings / runs``. Raw rate at which a check
  landed in ``warnings``.
* ``instability_score`` — ``2 * min(fire_rate, 1 - fire_rate)`` where
  ``fire_rate = (fails + warnings) / runs`` is the rate at which the
  check FIRED at all (failed OR warned) on byte-identical input,
  regardless of tier. This is the headline, tier-agnostic noise metric:
  a pass<->warning flip and a pass<->fail flip score identically, so it
  stays nonzero for demoted (warning-tier) checks that ``flip_score``
  goes blind to. Known limitation: a three-way pass/warning/fail split
  on one check is only partially captured because ``fire_rate`` folds
  warning and fail together (e.g. 4 pass / 3 warn / 3 fail -> fire_rate
  0.6 -> instability 0.8, understating the true three-way split). This
  is acceptable — demoted checks flip two-way pass<->warning in
  practice; do not replace with entropy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agents import critic


# --- Public types -----------------------------------------------------------

@dataclass
class CheckStats:
    """Per-check_id tally across K runs on one fixture.

    Attributes:
        check_id: The Critic check ID (e.g. "C4", "A3", "F1").
        runs: Number of evaluations the check was visible in (typically K).
        fails: Times the check appeared in ``failed_checks``.
        passes: Times the check appeared in ``passed_checks``.
        warnings: Times the check appeared in ``warnings``.
        fail_rate: ``fails / runs`` (0.0 when ``runs == 0``).
        flip_score: ``2 * min(fail_rate, 1 - fail_rate)``. Zero when the
            fail outcome is unanimous, one at 50/50. Fails-only — the
            gating-severity signal.
        warning_rate: ``warnings / runs`` (0.0 when ``runs == 0``). Raw
            rate at which the check landed in ``warnings``.
        instability_score: ``2 * min(fire_rate, 1 - fire_rate)`` where
            ``fire_rate = (fails + warnings) / runs``. Tier-agnostic
            headline noise metric: nonzero whenever the check flips
            between firing (fail OR warn) and not firing, so it stays
            visible after a check is demoted to ``warning`` tier and
            ``flip_score`` goes to 0. Known limitation: a three-way
            pass/warning/fail split is only partially captured because
            ``fire_rate`` folds warning and fail together, understating a
            true three-way split.
        verdict_tier: ``"hard_fail" | "soft_fail" | "warning"`` per
            ``critic.VERDICT_LEVEL_BY_CHECK``. The tier matters because a
            noisy hard_fail check is more dangerous than a noisy warning.
    """

    check_id: str
    runs: int = 0
    fails: int = 0
    passes: int = 0
    warnings: int = 0
    fail_rate: float = 0.0
    flip_score: float = 0.0
    warning_rate: float = 0.0
    instability_score: float = 0.0
    verdict_tier: str = "soft_fail"

    def to_dict(self) -> dict:
        return {
            "check_id": self.check_id,
            "runs": self.runs,
            "fails": self.fails,
            "passes": self.passes,
            "warnings": self.warnings,
            "fail_rate": round(self.fail_rate, 4),
            "flip_score": round(self.flip_score, 4),
            "warning_rate": round(self.warning_rate, 4),
            "instability_score": round(self.instability_score, 4),
            "verdict_tier": self.verdict_tier,
        }


@dataclass
class FixtureStats:
    """Aggregated K-run stats for a single fixture."""

    fixture_name: str
    runs: int
    verdicts: dict[str, int] = field(default_factory=dict)
    verdict_flip_score: float = 0.0
    checks: dict[str, CheckStats] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "fixture_name": self.fixture_name,
            "runs": self.runs,
            "verdicts": dict(self.verdicts),
            "verdict_flip_score": round(self.verdict_flip_score, 4),
            "checks": {
                cid: stats.to_dict()
                for cid, stats in sorted(self.checks.items())
            },
        }


# --- Core math --------------------------------------------------------------

def flip_score(fail_rate: float) -> float:
    """Return a 0..1 noise score that peaks at a 50/50 split.

    Defined as ``2 * min(fail_rate, 1 - fail_rate)``. Properties:

    * unanimous pass (rate 0.0) -> 0.0
    * unanimous fail (rate 1.0) -> 0.0
    * 50/50 split   (rate 0.5) -> 1.0
    * symmetric around 0.5
    * linear in each half

    The factor of 2 normalises so the metric is read as "how noisy" on a
    0..1 scale rather than "min margin".
    """
    if fail_rate < 0.0 or fail_rate > 1.0:
        raise ValueError(
            f"fail_rate must be in [0, 1], got {fail_rate!r}"
        )
    return 2.0 * min(fail_rate, 1.0 - fail_rate)


def instability_score(fire_rate: float) -> float:
    """Return a 0..1 tier-agnostic noise score that peaks at a 50/50 split.

    Defined as ``2 * min(fire_rate, 1 - fire_rate)`` where ``fire_rate`` is
    ``(fails + warnings) / runs`` — i.e. the rate at which the check FIRED at
    all (failed OR warned) on byte-identical input, regardless of tier.

    Unlike ``flip_score`` (fails-only), this stays nonzero for a check that
    flips pass<->warning, which is what happens after a check is demoted to
    warning tier. Same shape/properties as ``flip_score``: 0 when unanimous
    (always-fired or never-fired), 1 at a 50/50 split, symmetric, linear in
    each half.

    Known limitation: a three-way pass/warning/fail split on one check is only
    partially captured (fire_rate folds warning and fail together), so a true
    three-way split is understated. This is acceptable — demoted checks flip
    two-way pass<->warning in practice. Do not replace with entropy.
    """
    if fire_rate < 0.0 or fire_rate > 1.0:
        raise ValueError(
            f"fire_rate must be in [0, 1], got {fire_rate!r}"
        )
    return 2.0 * min(fire_rate, 1.0 - fire_rate)


def verdict_flip_score(verdicts: dict[str, int]) -> float:
    """Return ``1 - (max(count) / total)`` over the verdict histogram.

    Zero when every run produced the same verdict; positive otherwise.
    Used as a fixture-level summary — separate from the per-check
    flip_score because the verdict is a tri-valued field (pass /
    soft_fail / hard_fail), not a binary outcome.
    """
    total = sum(verdicts.values())
    if total <= 0:
        return 0.0
    return 1.0 - (max(verdicts.values()) / total)


def tally_check(check_id: str, outputs: list[dict]) -> CheckStats:
    """Count how often ``check_id`` failed / passed / warned across runs.

    A run "sees" the check if its check_id appears in any of
    ``failed_checks``, ``passed_checks``, or ``warnings``. If the LLM
    never mentions the check at all in a given run, that run does not
    contribute to ``runs`` for that check_id — variance is undefined for
    an unobserved outcome.
    """
    tier = critic.VERDICT_LEVEL_BY_CHECK.get(check_id, "soft_fail")
    stats = CheckStats(check_id=check_id, verdict_tier=tier)

    for output in outputs:
        failed_ids = {
            str(f.get("check_id", "")).strip()
            for f in (output.get("failed_checks") or [])
            if isinstance(f, dict)
        }
        passed_ids = {
            str(c).strip()
            for c in (output.get("passed_checks") or [])
            if isinstance(c, str)
        }
        warning_ids = {
            str(w.get("check_id", "")).strip()
            for w in (output.get("warnings") or [])
            if isinstance(w, dict)
        }

        observed = (
            check_id in failed_ids
            or check_id in passed_ids
            or check_id in warning_ids
        )
        if not observed:
            continue

        stats.runs += 1
        if check_id in failed_ids:
            stats.fails += 1
        if check_id in passed_ids:
            stats.passes += 1
        if check_id in warning_ids:
            stats.warnings += 1

    if stats.runs > 0:
        stats.fail_rate = stats.fails / stats.runs
        stats.flip_score = flip_score(stats.fail_rate)
        stats.warning_rate = stats.warnings / stats.runs
        fire_rate = (stats.fails + stats.warnings) / stats.runs
        stats.instability_score = instability_score(fire_rate)
    return stats


def aggregate_fixture(
    fixture_name: str,
    outputs: list[dict],
    check_ids: tuple[str, ...] = critic.ALL_CHECK_IDS,
) -> FixtureStats:
    """Compute fixture-level stats from K Critic outputs.

    ``check_ids`` defaults to every gating check (everything in
    ``ALL_CHECK_IDS``). Warning-tier checks like C7 are intentionally
    included so the harness can show whether their warning rate is
    stable.
    """
    verdicts: dict[str, int] = {}
    for output in outputs:
        v = str(output.get("verdict", "") or "").strip()
        if not v:
            continue
        verdicts[v] = verdicts.get(v, 0) + 1

    checks = {cid: tally_check(cid, outputs) for cid in check_ids}

    return FixtureStats(
        fixture_name=fixture_name,
        runs=len(outputs),
        verdicts=verdicts,
        verdict_flip_score=verdict_flip_score(verdicts),
        checks=checks,
    )


def rank_checks_worst_first(stats: FixtureStats) -> list[CheckStats]:
    """Return the fixture's checks sorted worst-first.

    Primary key: ``instability_score`` descending — the noisiest checks
    first, tier-agnostic so demoted (warning-tier) flippers surface
    alongside fail-tier flippers. Tie-break: ``fail_rate`` descending —
    within an equally-noisy band, surface the gating checks (the ones
    that can actually reject a draft) ahead of warning-only noise. Final
    tie-break: check_id ascending, for stable ordering.
    """
    return sorted(
        stats.checks.values(),
        key=lambda s: (-s.instability_score, -s.fail_rate, s.check_id),
    )
