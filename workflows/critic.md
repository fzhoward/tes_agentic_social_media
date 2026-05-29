# Critic Agent — Workflow SOP

*V1 — independent QA pass on every draft before Slack approval*

## Role

The Critic is an independent quality gate. It evaluates every drafted post against the Critic Checklist before the post advances to owner approval. The Critic uses a different LLM provider than the Drafter (OpenAI vs. Anthropic) to break correlated errors — when the same model writes and reviews, both can share blind spots. The Critic does not write content. It evaluates the draft and returns a structured verdict with specific, actionable fix instructions when issues are found.

## Trigger

| Trigger | Source | Frequency |
|---------|--------|-----------|
| Queue-driven | Make.com scenario | Fires when a Content Queue row has `status = drafted` |
| Re-evaluation | Make.com scenario | Fires when the Drafter resubmits after applying Critic fix instructions (`revision_round` incremented) |
| Post-edit evaluation | Make.com scenario | Fires when the owner edits the caption via the Slack approval card (re-runs Critic only, skips Drafter) |

## Inputs

| Input | Source | Purpose |
|-------|--------|---------|
| `row_id` | Make.com scenario | Content Queue row to evaluate |
| `revision_round` | Make.com scenario | Which evaluation round this is (1 on first pass, 2 or 3 on revisions) |
| `previous_critic_output` | Make.com scenario (if `revision_round > 1`) | The previous `failed_checks` array so the Critic can verify whether previous issues were addressed |
| Content Queue row (drafted) | Google Sheets | Full draft: caption, creative_hook_text, first_comment, cta_text, hook_text, image_overlay_text, media_url, media_format_used, platform, objective, content_type, focus_equipment_id, draft_rationale |
| Critic Checklist | `skills/critic_checklist.md` | The full evaluation criteria with check IDs, categories, and verdict levels |
| Brand voice file | `skills/brand_voice.md` | Voice rules, formatting rules, banned language, post objective rules, pricing policy |
| Catalog item record | Google Sheets (if `focus_equipment_id` is set) | Spec fields for verifying factual claims (checks G1, G2, G3) |
| Review record | Google Sheets (if `content_type = Social Proof` and `review_id` is set) | Original review text for verifying the caption does not fabricate beyond the review |
| CTA Skill | `skills/cta.md` | CTA type rules for objective compliance checks |
| Content Type Definitions | `skills/content_types.md` | Content type definitions for alignment checks |
| Platform Style Skill | `skills/platform_style.md` | Character limits, platform constraints |

## Outputs

| Output | Destination | Description |
|--------|-------------|-------------|
| Critic evaluation JSON | Returned by agent (stdout) | Structured verdict, failed checks with fix instructions, warnings, passed checks |
| Updated Content Queue row | Google Sheets | `critic_score` (verdict), `critic_notes` (full JSON), `status` |

### Status transitions

- **pass** → `status = awaiting_approval`. Make.com posts the Slack approval card.
- **soft_fail** → `status` remains `drafted`. Make.com increments `revision_round` and routes back to the Drafter with `failed_checks` as fix instructions.
- **hard_fail** → `status = hard_fail`. Make.com posts an escalation card to `{{SLACK_ERROR_CHANNEL}}`.

## Processing Steps

### 1. Load Context

Load the drafted Content Queue row by `row_id`. Verify `status == "drafted"` — if not, return early with a no-op result so Make.com doesn't double-evaluate.

Load the catalog item record if `focus_equipment_id` is set. Load the review record if `content_type` is Social Proof and `review_id` is set. Load all skills referenced by the checklist via `tools/skill_loader.py`.

### 2. Deterministic Pre-Checks

Run the mechanical checks in Python before calling the LLM. These are faster, cheaper, and more reliable than asking the LLM to do regex or arithmetic:

| Check | Logic |
|-------|-------|
| A1 (pricing) | Regex scan for `$`, dollar amounts, and the banned pricing-word list ("affordable", "competitive", "budget-friendly", "cheap", "cheapest") |
| A2 (emoji) | Regex scan for emoji unicode ranges |
| B1 (markdown) | Regex scan for `**…**`, `*…*`, leading `#`, leading `- ` |
| B2 (exclamation) | Scan for `!` characters |
| B3 (em dash) | Scan for `—` (U+2014) |
| B4 (hashtags) | Scan for `#` followed by a word character |
| B5 (vertical stack) | Detect any two non-empty content lines directly adjacent (no blank line between) |
| B6 (sentence length) | Split caption on `.` `!` `?`, count words per sentence, flag any sentence > 18 words |
| B7 (fragment lines) | Count content lines ≤ 5 words; flag if fewer than 2 |
| B8 (word count) | Count caption body words, check against platform/content-type target ranges |
| B9 (hook duplication) | Detect the opening line (normalized lowercased, no punctuation) repeating later in the caption |
| B11 (creative_hook_text) | Word count ≤ 7; distinct from caption_hook (no substring overlap either way) |
| D1 (caption length) | Character count against platform limits (FB: 63,206; IG: 2,200; GBP: 1,500) |
| D5 (GBP button type) | If platform is GBP and a button is specified, must be one of CALL/LEARN_MORE/BOOK/ORDER/GET_DIRECTIONS/SIGN_UP |
| D6 (GBP button URL) | LEARN_MORE/BOOK/ORDER/SIGN_UP require a URL; CALL/GET_DIRECTIONS do not |
| E2 (one CTA) | Count CTA patterns in caption — flag if multiple distinct CTAs are present |
| G3 (spec rounding) | If catalog data is available, extract numbers from caption and compare against catalog spec values; flag rounding (e.g., 12,000 vs. 11,800) |

Pass the deterministic results into the LLM prompt as pre-computed facts. The LLM can override only if it has strong evidence; otherwise the deterministic verdict stands.

### 3. LLM Evaluation

Build the OpenAI prompt with:

- **System message:** Role definition ("you are a QA critic"), the full critic checklist, brand voice, platform style, CTA skill, content type definitions, instruction to return JSON matching the Critic Output Format, instruction that every `failed_checks` entry must include a specific actionable `fix_instruction`. When `revision_round > 1`, include the previous `failed_checks` and instruct the Critic to verify whether each previous issue was addressed.
- **User message:** The full drafted Content Queue row (all relevant fields), the catalog item record (if applicable) including all spec fields so G1-G3 can cross-reference, the review text (if Social Proof) for verifying the caption does not fabricate beyond the review, and the deterministic pre-check results.
- **Response format:** Request a JSON object matching the Critic Output Format. Parse with one retry on failure.

The LLM focuses on judgment-heavy checks (C1-C7, F1-F4) and confirms the deterministic results. Mechanical checks are already handled by code.

### 4. Catalog Verification (G Checks)

When `focus_equipment_id` is set:

- **G1:** Cross-reference every spec claim in the caption against the catalog values. If a number, dimension, capacity, or feature is stated, it must match the catalog exactly.
- **G2:** Verify the item status is `active` or `seasonal`. Skip this check if the catalog item record was not loaded (item not found), but record a warning.
- **G3:** Check for rounded or inflated specs. Deterministic pre-check handles obvious cases; LLM confirms ambiguous cases.

### 5. Merge Results

Combine deterministic results with LLM results. Deterministic results take precedence on conflict — when code says "no pricing language detected" but the LLM flags A1, trust the code. Each `failed_checks` entry preserves `check_id`, `category`, `verdict_level`, `location`, `description`, and `fix_instruction`.

### 6. Determine Verdict

Apply the verdict hierarchy in order:

1. Any check has `verdict_level = hard_fail` → overall verdict is `hard_fail`
2. `revision_round >= 3` and any check has `verdict_level = soft_fail` → overall verdict is `hard_fail` (escalation; note added to `notes`)
3. Any check has `verdict_level = soft_fail` (and no hard_fails, and `revision_round < 3`) → overall verdict is `soft_fail`
4. All checks pass → overall verdict is `pass`

### 7. Assemble Output

Return the structured JSON defined in the Critic Checklist:

```json
{
  "queue_row_id": "{{ROW_ID}}",
  "platform": "{{PLATFORM}}",
  "revision_round": 1,
  "verdict": "pass | soft_fail | hard_fail",
  "failed_checks": [...],
  "warnings": [...],
  "passed_checks": [...],
  "notes": ""
}
```

### 8. Write to Content Queue

Update the Content Queue row:

- `critic_score` ← verdict string (`pass`, `soft_fail`, `hard_fail`)
- `critic_notes` ← JSON-serialized Critic output (failed_checks, warnings, passed_checks, notes)
- `status` ← as described in Outputs above

In `--dry-run` mode, the agent evaluates but does not write.

## Revision Loop Rules

- A `soft_fail` returns the draft to the Drafter with specific fix instructions for every failed check.
- The Drafter revises and resubmits. The Critic re-evaluates the full checklist (not just previously failed items).
- **Maximum 2 revision rounds.** If the draft still has soft_fail issues after round 2, the Critic escalates to `hard_fail` on round 3 with all remaining issues listed.
- `hard_fail` items are never sent back for revision. They escalate immediately on the first evaluation.

The Critic is stateless between rounds. It receives `revision_round` and `previous_critic_output` as input, evaluates fresh, and returns. The Critic does not call the Drafter — Make.com handles routing.

## Autonomous Decisions

- Whether each check passes or fails (applying the checklist rules)
- The specific fix instruction for each failure (must be actionable — "Remove the dollar amount '$250/day' from sentence 3", not "fix the pricing language")
- Whether a spec claim is close enough to the catalog value or a violation (G3 — no rounding allowed)
- The overall verdict based on the hierarchy

## Human-in-Loop

None. The Critic operates fully autonomously. Its output feeds into the Slack approval flow (for passes) or back to the Drafter (for soft_fails). Hard_fails escalate to the Slack error channel for owner attention.

## Error Handling

| Error | Behavior |
|-------|----------|
| Row not found | Return error result, do not write to sheet. Make.com retries. |
| Status not `drafted` | Return early no-op result. Do not write to sheet. |
| Catalog item not found (for G checks) | Skip G checks, add a warning, continue with all other checks. |
| Review record not found (for Social Proof) | Skip review-fidelity checks, add a warning, continue. |
| Brand voice / checklist file not accessible | Log error, abort evaluation. The draft stays at `drafted` and Make.com retries. |
| OpenAI call fails | Retry once with a JSON-only nudge. If still failing, return error result and leave status at `drafted`. |
| LLM JSON parse fails | Retry once. If still failing, return error and leave status at `drafted`. |

## Failure Mode

If the Critic fails to run, the draft stays at `status = drafted`. Make.com retries on the next cycle. The draft does not advance to approval without a Critic evaluation. This is a hard gate — there is no bypass.

## What the Critic Does NOT Do

- **Does not evaluate image quality.** The Critic reviews the caption, CTA, formatting, and factual claims. Image quality is the owner's judgment at the Slack approval step.
- **Does not rewrite content.** The Critic provides fix instructions. The Drafter applies them.
- **Does not evaluate hooks.** Hook quality is governed by the Hook Creation Skill's scoring rubric. The Critic checks only that a hook exists, that the opening hook line is not repeated later in the caption (B9), and that `creative_hook_text` meets its constraints (B11).
- **Does not check media format.** Whether the Strategist's media format assignment was correct is not a Critic concern.
- **Does not call the Drafter.** Routing on `soft_fail` is Make.com's responsibility.

## Config Dependencies

| Config Path | Purpose |
|-------------|---------|
| `apis.llm_provider_critic` | LLM provider (must be `openai`) |
| `approval.error_channel` | Where Make.com posts hard_fail escalations |
| `catalog.spec_sheet_id` | Catalog sheet for spec verification (G1-G3) |
| `catalog.reviews_sheet_id` | Reviews sheet for Social Proof verification |
| `drive.content_queue_sheet_id` | Content Queue sheet being evaluated |
| `strategy.pricing_in_posts` | Pricing policy reference for A1 |

All other rules come from skill files loaded at runtime, not from business_config.yaml directly.

---

*Critic Agent — Workflow SOP v1*
*Last updated: 2026-05-27*
