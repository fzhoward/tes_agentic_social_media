# Critic eval harness

v1: **run-to-run variance only**. For each fixture, call the Critic K times
on byte-identical input and measure how often each check ID flips
pass ↔ fail across those K runs. No gold labels, no precision/recall —
that is a later layer. Variance needs no human labelling and directly
answers the question "which LLM-judged checks are coin flips on
identical input?"

The motivation is the Session 27 incident where the row
`STR-20260602-FB-01` returned `pass` on one Critic call and `hard_fail`
on the next, on the same caption, seconds apart. Because a `soft_fail`
at `revision_round >= 3` escalates to `hard_fail`, that kind of LLM
noise can terminally reject a good post. The harness exists to quantify
which checks have that problem, before deciding per check whether to
keep, demote to `warning` tier, or move to a deterministic / conditional
rule.

## Quick start

### Dry mode (default — no API calls)

```bash
python -m evals
```

Drives the harness end-to-end against a scripted fake LLM. Produces a
report and a JSON dump in `evals/out/`. Zero API spend. Useful for
proving the math + report format work and for the unit tests
(`tests/test_evals_*.py`).

### Real mode (costs money)

```bash
python -m evals --real --k 10
```

Calls the real OpenAI path via `agents.critic.evaluate_draft`. With the
seed fixture set (4 fixtures) at K=10, that is 40 chat-completion calls
per run. Requires `OPENAI_API_KEY` in the environment.

`--real` is opt-in only. The harness never calls OpenAI on import, in
tests, or as a default.

### Useful flags

| flag | default | meaning |
|------|---------|---------|
| `--k N` | 10 | Runs per fixture. K=10 is the recommended floor for stable variance estimates. |
| `--real` | off | Opt into the real OpenAI path. |
| `--fixture NAME` | all | Restrict to one fixture (repeat for several). |
| `--seed N` | 0 | Dry-mode PRNG seed (ignored under `--real`). |
| `--out PATH` | `evals/out/` | JSON dump directory. |
| `--no-json` | off | Skip the JSON dump (still prints the text report). |

## Metrics

For each check ID, across K runs on one fixture:

* **`fail_rate`** — `fails / runs`. 0.0 means the check never landed in
  `failed_checks`; 1.0 means it always did.
* **`flip_score`** — `2 * min(fail_rate, 1 - fail_rate)`. Zero when the
  fail outcome is unanimous either way (stable). One at a 50/50 split.
  This is the **gating-severity** signal — noise on the `failed_checks`
  channel only, the channel that can terminally reject a draft. A check
  demoted to `warning` tier no longer lands in `failed_checks`, so its
  `flip_score` collapses to 0 even while it is flipping pass ↔ warning.
* **`warning_rate`** — `warnings / runs`. Raw rate at which the check
  landed in `warnings`.
* **`instability_score`** — `2 * min(fire_rate, 1 - fire_rate)` where
  `fire_rate = (fails + warnings) / runs` is the rate at which the check
  **fired at all** (failed OR warned), regardless of tier. This is the
  **headline, tier-agnostic** noise metric: a pass ↔ warning flip and a
  pass ↔ fail flip score identically, so a demoted check that
  `flip_score` goes blind to still shows up here. Known limitation: a
  three-way pass/warning/fail split is only partially captured because
  `fire_rate` folds warning and fail together, understating the true
  three-way split (rare in practice — demoted checks flip two-way).
* **`verdict_tier`** — `hard_fail | soft_fail | warning` per the
  Critic's `VERDICT_LEVEL_BY_CHECK`. A noisy `hard_fail` check is more
  dangerous than a noisy `warning`.

Plus, per fixture, a **`verdict_flip_score`** for the headline `pass /
soft_fail / hard_fail` decision: `1 - max(count) / runs`. Zero means
every run returned the same verdict.

The text report sorts checks worst-first by `instability_score`,
breaking ties on `fail_rate` (so gating checks surface within an
equally-noisy band). Stable rows (`instability_score` at/below the
threshold and a unanimous `fire_rate` of 0 or 1) are collapsed into a
footer count to keep the table focused on the noisy checks. Both `flip`
(fails-only) and `instab` (tier-agnostic) columns are shown. The JSON
dump in `evals/out/` always contains the full per-check tally for
diffing two harness runs after a prompt change.

## Fixtures

Frozen drafts live in [`fixtures.py`](fixtures.py). They are checked
into the repo as Python strings so the LLM sees identical bytes across
K runs and identical bytes next week, and so a prompt change can be
A/B'd against a stable baseline. Do **not** rewire this to pull from the
live Content Queue at run time — that would make variance meaningless
because the input would be moving.

The v1 seed set is four fixtures spanning quality:

1. `str_20260531_ig_01` — Equipment Spotlight (IG), focus equipment
   set, model named in caption. Expected stable-pass on C4.
2. `str_20260602_fb_01` — Educational Tip (FB), no focus equipment.
   The Session 27 flip case — known noisy on C4.
3. `clean_high_quality` — A clean, specific, well-formed draft.
   Expected broadly stable.
4. `weak_generic` — A deliberately vague low-specificity draft.
   Expected stable-fail on C-family specificity checks.

Add fixtures sparingly. Every new fixture costs `K` API calls per real
run, and a sprawling fixture set dilutes the signal in the report.

## Design notes

* **Pure addition.** The harness only adds files. It does **not**
  modify `agents/critic.py` or any production agent. The injection
  point it uses (`evaluate_draft(..., llm_call=...)`) already existed
  for tests.
* **No side effects.** The harness never writes to the Content Queue,
  Drive, or Slack. It calls `evaluate_draft` only — the writeback path
  (`critique_single_row`) is deliberately not invoked.
* **Dry mode is always reachable offline.** No network, no
  `OPENAI_API_KEY`, no Google credentials required.
* **JSON output is stable for diffing.** Two harness runs at the same
  K should produce JSON files whose `fixtures[].checks.*` blocks can
  be diff'd directly (modulo `generated_at_iso`) to show what changed
  after a prompt edit. The payload carries `schema_version: 2` (bumped
  from 1 when `warning_rate` and `instability_score` were added). The
  bump is additive — existing keys are unchanged, so a v1 ↔ v2 diff
  shows the two new per-check keys purely as additions.
* **Out of scope (v1).** No gold labels, no precision/recall, no
  `--row-id` live-pull mode, no cost accounting. These are reasonable
  v2+ additions but each one is a meaningful build on top.
