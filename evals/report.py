"""Formatters for the variance harness output.

Two outputs:

* Text report — a per-fixture human-readable table, sorted worst-first
  by instability score (tier-agnostic). This is what shows up in the
  terminal at the end of a run and what we paste into the PR description.
* JSON dump — the full structured stats, stable across runs, suitable
  for diffing two harness runs after a prompt change. Written to
  ``evals/out/`` (gitignored) so we keep history locally without
  cluttering the repo.

Both formatters operate on already-aggregated ``FixtureStats`` so they
are pure: no I/O happens here (except in ``write_json_dump``).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from evals.variance import FixtureStats, rank_checks_worst_first


# Rows with instability_score at or below this AND a unanimous fire_rate
# (0.0 or 1.0) in the text report are summarised as a single "N stable
# rows hidden" footer line rather than printed one by one. Keeps the
# worst-first table focused. Set to 0.0 to collapse only truly-stable
# rows (the default). The name is kept for back-compat with callers.
TEXT_REPORT_FLIP_THRESHOLD: float = 0.0


def format_text_report(
    all_stats: list[FixtureStats],
    *,
    flip_threshold: float = TEXT_REPORT_FLIP_THRESHOLD,
    max_rows_per_fixture: int = 30,
) -> str:
    """Format every fixture's stats into a printable table.

    Each fixture gets a header (name + verdict histogram + overall
    verdict flip score) followed by a per-check table sorted worst-first
    by ``instability_score``. A row is hidden iff
    ``instability_score <= flip_threshold`` and its ``fire_rate``
    (``(fails + warnings) / runs``) is unanimous (0.0 or 1.0) — those are
    stable checks we already have a tally for via the JSON dump, and
    printing them is noise. The fire-rate test (not fail-rate) keeps a
    check that flips pass<->warning visible even though its fail_rate is
    pinned at 0.

    Checks the LLM never observed in any run are dropped entirely
    (runs == 0) — variance is undefined on no observations.
    """
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("Critic variance harness — per-fixture report")
    lines.append("=" * 78)

    for stats in all_stats:
        lines.append("")
        lines.append(f"Fixture: {stats.fixture_name}  (K={stats.runs})")
        verdict_summary = ", ".join(
            f"{v}={c}" for v, c in sorted(stats.verdicts.items())
        )
        if not verdict_summary:
            verdict_summary = "(no verdicts recorded)"
        lines.append(
            f"  Verdicts: {verdict_summary}  "
            f"verdict_flip_score={stats.verdict_flip_score:.3f}"
        )
        lines.append("-" * 78)
        lines.append(
            f"  {'check':<6}  {'tier':<10}  {'runs':>4}  "
            f"{'fails':>5}  {'pass':>4}  {'warn':>4}  "
            f"{'fail_rate':>9}  {'flip':>5}  {'instab':>6}"
        )

        ranked = rank_checks_worst_first(stats)
        printed = 0
        for check_stats in ranked:
            if check_stats.runs == 0:
                continue
            fire_rate = (
                check_stats.fails + check_stats.warnings
            ) / check_stats.runs
            if (
                check_stats.instability_score <= flip_threshold
                and fire_rate in (0.0, 1.0)
            ):
                continue
            if printed >= max_rows_per_fixture:
                break
            lines.append(
                f"  {check_stats.check_id:<6}  "
                f"{check_stats.verdict_tier:<10}  "
                f"{check_stats.runs:>4}  {check_stats.fails:>5}  "
                f"{check_stats.passes:>4}  {check_stats.warnings:>4}  "
                f"{check_stats.fail_rate:>9.3f}  "
                f"{check_stats.flip_score:>5.3f}  "
                f"{check_stats.instability_score:>6.3f}"
            )
            printed += 1

        # Footer: count of dropped stable rows so the operator knows the
        # full picture is in the JSON dump.
        observed = [s for s in stats.checks.values() if s.runs > 0]
        stable_count = sum(
            1 for s in observed
            if s.instability_score <= flip_threshold
            and ((s.fails + s.warnings) / s.runs) in (0.0, 1.0)
        )
        unobserved = len(stats.checks) - len(observed)
        lines.append(
            f"  (stable rows hidden: {stable_count}; "
            f"unobserved check IDs: {unobserved})"
        )

    lines.append("")
    lines.append("=" * 78)
    lines.append(
        "Sort: instability_score desc, fail_rate desc, check_id asc. "
        "instab=1.0 is a perfect coin flip; 0.0 is unanimous."
    )
    lines.append(
        "instab is tier-agnostic (check fired at all: failed OR warned); "
        "flip is fails-only (gating-tier noise that can reject a draft)."
    )
    lines.append("=" * 78)
    return "\n".join(lines)


def build_json_payload(
    all_stats: list[FixtureStats],
    *,
    mode: str,
    k: int,
    seed: int | None = None,
) -> dict:
    """Build the JSON-dump payload from a list of FixtureStats.

    Kept separate from ``write_json_dump`` so tests can serialise without
    touching disk.
    """
    return {
        "schema_version": 2,
        "generated_at_iso": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(),
        ),
        "mode": mode,
        "k": k,
        "seed": seed,
        "fixtures": [stats.to_dict() for stats in all_stats],
    }


def write_json_dump(
    all_stats: list[FixtureStats],
    out_dir: Path,
    *,
    mode: str,
    k: int,
    seed: int | None = None,
) -> Path:
    """Write the JSON payload to ``out_dir`` and return the file path.

    Filename embeds mode, K, and a UTC timestamp so multiple harness
    runs accumulate side-by-side for later diffing. The output directory
    is created on demand. Callers are expected to point at
    ``evals/out/`` (gitignored).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = build_json_payload(all_stats, mode=mode, k=k, seed=seed)
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    filename = f"variance_{mode}_k{k}_{timestamp}.json"
    path = out_dir / filename
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path
