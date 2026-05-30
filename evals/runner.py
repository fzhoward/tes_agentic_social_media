"""Run the Critic K times per fixture and collect the structured outputs.

Always routes through ``agents.critic.evaluate_draft``. That function
takes an optional ``llm_call`` injection — the harness uses it to:

* Real mode: pass ``llm_call=None`` so ``evaluate_draft`` uses the real
  OpenAI path (``get_llm_evaluation``). This is what we are measuring
  variance of. K calls per fixture * len(FIXTURES) costs real money.

* Dry mode (default): pass a controlled fake ``llm_call`` that emits a
  scripted, partially-noisy result so the variance math and report
  formatting can be exercised end-to-end with zero API spend. Useful
  for unit tests and self-tests on a laptop with no network.

In both modes the deterministic pre-checks, merge logic, verdict
escalation, and validation pass through unchanged — that is the whole
point of using ``evaluate_draft`` rather than calling OpenAI directly.

The runner does NOT touch the Content Queue, the Drive, or Slack.
Critic writeback lives in ``critique_single_row``, which we deliberately
do not call. Variance lives upstream of any side effect.
"""

from __future__ import annotations

import os
import random
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from agents import critic
from tools.config_loader import Config

from evals.fixtures import FIXTURES, Fixture


# --- Result types ----------------------------------------------------------

@dataclass
class RunResult:
    """One Critic invocation against one fixture.

    ``output`` is the dict returned by ``evaluate_draft`` (the Critic
    Output JSON shape). ``error`` is non-empty only when the call raised;
    in that case ``output`` is an empty dict. ``elapsed_ms`` is recorded
    so a future cost/time report can use it.
    """

    fixture_name: str
    run_index: int
    output: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    elapsed_ms: float = 0.0


# --- Skill / config loading ------------------------------------------------

def _build_dry_config() -> Config:
    """Minimal Config sufficient to satisfy the Critic's pre-checks.

    Mirrors the shape of ``business_config_tes_rentals.yaml`` for the
    fields the Critic actually reads (``contact.booking_url`` and
    ``contact.website`` for D5/D6). Real mode replaces this with the
    on-disk config so platform-specific behaviour matches production.
    """
    return Config({
        "contact": {
            "booking_url": "https://tesrents.com",
            "website": "https://tesrents.com",
        },
    })


def _load_real_skills(config: Config) -> dict[str, str]:
    """Load the five skill files the LLM prompt needs.

    Returns empty strings on load failure rather than raising — a missing
    skill file still lets the run produce *some* output we can compare,
    and the warning is loud enough on stderr that the operator notices.
    """
    from tools import skill_loader

    wanted = (
        "critic_checklist",
        "brand_voice",
        "platform_style",
        "cta",
        "content_types",
    )
    skills: dict[str, str] = {}
    for name in wanted:
        try:
            skills[name] = skill_loader.load_skill(name, config=config)
        except Exception as exc:  # pragma: no cover — diagnostic only
            print(
                f"[evals.runner] WARN: skill {name!r} failed to load: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            skills[name] = ""
    return skills


# --- Dry-mode fake LLM -----------------------------------------------------

# Per-fixture script for the dry-mode fake. Each entry says how often a
# given LLM-judged check should fail in the synthetic output. A value of
# 0.5 produces the maximally-noisy flip the report should highlight; 0.0
# means stable pass, 1.0 means stable fail. The set of check IDs here
# does not need to match any real LLM behaviour — it just needs to
# produce a visible spread so the harness self-test is non-trivial.
DRY_NOISE_PROFILES: dict[str, dict[str, float]] = {
    # The known C4 flip case from Session 27 — model it as a true coin
    # flip so the report can demonstrate detecting it.
    "str_20260602_fb_01": {"C4": 0.5, "F1": 0.2},
    # The set-focus / model-named case — should be stable.
    "str_20260531_ig_01": {"C4": 0.0, "F1": 0.0},
    # Clean draft — fully stable across the board.
    "clean_high_quality": {},
    # Weak draft — fails C4 every time, plus a noisy C2.
    "weak_generic": {"C4": 1.0, "C2": 0.5},
}


def _make_dry_llm_call(
    fixture_name: str,
    rng: random.Random,
) -> Callable[..., dict]:
    """Return a fake ``llm_call`` driven by ``DRY_NOISE_PROFILES``.

    The fake reports every LLM-judged check (everything in
    ``ALL_CHECK_IDS`` minus ``PRE_CHECK_IDS``) and stochastically fails a
    subset according to the noise profile. Checks not in the profile
    always pass. The ``rng`` is provided externally so the dry-mode
    self-test is reproducible from a seed.
    """
    noise = DRY_NOISE_PROFILES.get(fixture_name, {})
    llm_check_ids = tuple(
        cid for cid in critic.ALL_CHECK_IDS
        if cid not in critic.PRE_CHECK_IDS
    )

    def fake(
        row, catalog_item, review_text,
        pre_failures, pre_passed, pre_warnings,
        revision_round, previous_critic_output, skills,
    ) -> dict:
        failed: list[dict] = []
        passed: list[str] = []
        for cid in llm_check_ids:
            rate = noise.get(cid, 0.0)
            if rate > 0.0 and rng.random() < rate:
                entry = {
                    "check_id": cid,
                    "category": critic.CATEGORY_BY_CHECK.get(cid, "unknown"),
                    "verdict_level": critic.VERDICT_LEVEL_BY_CHECK.get(
                        cid, "soft_fail",
                    ),
                    "location": "dry-mode synthetic",
                    "description": (
                        f"dry-mode synthetic failure (rate={rate})"
                    ),
                    "fix_instruction": "n/a — dry mode",
                }
                if cid == "C4":
                    # C4 needs a subreason or the merge layer's C4
                    # suppression logic stays inert. Pick specificity so
                    # the failure is kept (model_naming would be dropped
                    # whenever the model is named in the caption).
                    entry["subreason"] = "specificity"
                failed.append(entry)
            else:
                passed.append(cid)

        return {
            "queue_row_id": str(row.get(critic.CQ_ROW_ID, "") or ""),
            "platform": str(row.get(critic.CQ_PLATFORM, "") or ""),
            "revision_round": revision_round,
            "verdict": "soft_fail" if failed else "pass",
            "failed_checks": failed,
            "warnings": [],
            "passed_checks": passed,
            "notes": "dry-mode synthetic output",
        }

    return fake


# --- Run loop --------------------------------------------------------------

def run_one(
    fixture: Fixture,
    config: Config,
    skills: dict[str, str],
    llm_call: Callable[..., dict] | None,
) -> RunResult:
    """Invoke ``evaluate_draft`` once and capture the output."""
    start = time.perf_counter()
    try:
        output = critic.evaluate_draft(
            row=fixture.row,
            catalog_item=fixture.catalog_item,
            review_text=fixture.review_text,
            revision_round=fixture.revision_round,
            previous_critic_output=fixture.previous_critic_output,
            config=config,
            skills=skills,
            llm_call=llm_call,
        )
        return RunResult(
            fixture_name=fixture.name,
            run_index=0,
            output=output,
            elapsed_ms=(time.perf_counter() - start) * 1000.0,
        )
    except Exception as exc:
        return RunResult(
            fixture_name=fixture.name,
            run_index=0,
            output={},
            error=f"{type(exc).__name__}: {exc}",
            elapsed_ms=(time.perf_counter() - start) * 1000.0,
        )


def run_fixture(
    fixture: Fixture,
    k: int,
    config: Config,
    skills: dict[str, str],
    llm_call_factory: Callable[[str], Callable[..., dict] | None],
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> list[RunResult]:
    """Run one fixture K times and collect the K RunResults.

    ``llm_call_factory`` returns the ``llm_call`` to pass into
    ``evaluate_draft`` for a given fixture. The factory exists so dry
    mode can hand back a fixture-specific fake; in real mode it just
    returns ``None`` so ``evaluate_draft`` takes its OpenAI path.
    """
    results: list[RunResult] = []
    for i in range(k):
        llm_call = llm_call_factory(fixture.name)
        result = run_one(fixture, config, skills, llm_call)
        result.run_index = i
        results.append(result)
        if progress_callback is not None:
            progress_callback(fixture.name, i + 1, k)
    return results


def run_all(
    k: int = 10,
    real: bool = False,
    fixtures: tuple[Fixture, ...] = FIXTURES,
    seed: int = 0,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> dict[str, list[RunResult]]:
    """Drive the harness over every fixture and return per-fixture results.

    Args:
        k: Runs per fixture.
        real: When True, call OpenAI via the real path. Requires
            ``OPENAI_API_KEY``. When False (default), use the dry-mode
            fake LLM — no API calls.
        fixtures: Override the default fixture set, e.g. for tests.
        seed: PRNG seed for the dry-mode fake. Ignored in real mode (the
            real LLM's randomness is what we are measuring).
        progress_callback: Optional ``(fixture_name, completed, total)``
            callback. Useful for CLI progress output.

    Returns:
        Dict mapping fixture_name to its list of K RunResults.
    """
    if real and not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError(
            "real mode requires OPENAI_API_KEY in the environment"
        )

    if real:
        from tools.config_loader import load_config
        config = load_config()
        skills = _load_real_skills(config)
        # In real mode each fixture uses the same path — llm_call=None
        # so evaluate_draft hits OpenAI.
        def factory(_name: str) -> Callable[..., dict] | None:
            return None
    else:
        config = _build_dry_config()
        skills = {}
        rng = random.Random(seed)
        def factory(name: str) -> Callable[..., dict] | None:
            # Each fixture gets its own fake, but they share the rng so
            # the seed determines the entire run.
            return _make_dry_llm_call(name, rng)

    results: dict[str, list[RunResult]] = {}
    for fixture in fixtures:
        results[fixture.name] = run_fixture(
            fixture=fixture,
            k=k,
            config=config,
            skills=skills,
            llm_call_factory=factory,
            progress_callback=progress_callback,
        )
    return results
