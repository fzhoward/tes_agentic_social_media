# Critic Agent — Workflow SOP

*V1 — independent QA pass on every draft before Slack approval*

## Role

The Critic is an independent quality gate. It evaluates every drafted post against the Critic Checklist before the post advances to owner approval. The Critic uses a different model/provider than the Drafter to break correlated errors. The Critic does not write content — it evaluates and returns a structured verdict with specific fix instructions when issues are found.

## Trigger

| Trigger | Source | Frequency |
|---------|--------|-----------|
| Queue-driven | Make.com scenario | Fires when a Content Queue row has `status = drafted` |
| Re-evaluation | Make.com scenario | Fires when the Drafter resubmits after applying Critic fix instructions |
| Post-edit evaluation | Make.com scenario | Fires when the owner edits the caption via the Slack approval card (re-runs Critic only, skips Drafter) |

## Inputs

| Input | Source | Purpose |
|-------|--------|---------|
| Content Queue row (drafted) | Google Sheets | The complete draft: caption, first_comment, cta_text, media_url, platform, objective, content_type, focus_equipment_id, media_format_used, hook_text, image_overlay_text, draft_rationale |
| Critic Checklist | Drive (portable) | The full evaluation criteria with check IDs, categories, and verdict levels |
| Brand voice file | Drive (per-business instance) | Voice rules, formatting rules, banned language, post objective rules, pricing policy |
| Catalog item record | Google Sheets (if `focus_equipment_id` is set) | Spec fields for verifying factual claims (checks G1, G2, G3) |
| CTA Skill | Drive (portable) | CTA type rules for objective compliance checks |
| Content Type Definitions | Drive (portable) | Content type definition for alignment checks |
| Platform Style Skill | Drive (portable) | Character limits, platform constraints |
| `revision_round` | Make.com scenario | Which evaluation round this is (1, 2, or 3). Passed as input by Make. |
| Previous Critic output | Make.com scenario (if revision_round > 1) | The previous `failed_checks` array for comparison |

## Outputs

| Output | Destination | Description |
|--------|-------------|-------------|
| Critic evaluation JSON | Make.com (returned to the scenario) | Structured verdict, failed checks with fix instructions, warnings, passed checks |
| Updated Content Queue row | Google Sheets | `status` updated to `critiqued` (on pass) or remains `drafted` (on soft_fail for re-drafting) |

The Make.com scenario reads the Critic's verdict and routes accordingly:
- **pass** → Post Slack approval card, update status to `awaiting_approval`
- **soft_fail** → Route back to Drafter with fix instructions, increment `revision_round`
- **hard_fail** → Post Slack escalation card to `{{SLACK_ERROR_CHANNEL}}`, update status to `hard_fail`

## Processing Steps

### 1. Load Context

Load the drafted Content Queue row, the Critic Checklist, the brand voice file, the relevant catalog item record, and all skill files referenced by the checklist.

### 2. Check Revision Round

If `revision_round >= 3`, this is the escalation check. Any remaining `soft_fail` items will be upgraded to `hard_fail` per the checklist's revision escalation logic.

### 3. Evaluate All Checks

Run every check in the Critic Checklist in order (A → B → C → D → E → F → G → W). Do not short-circuit. All checks run on every evaluation so the Drafter gets the complete picture in one pass.

For each check:
- Determine if the check passes or fails
- If it fails, record:
  - `check_id` (e.g., "A1", "B6")
  - `category` (e.g., "non_negotiable", "formatting")
  - `verdict_level` ("hard_fail" or "soft_fail")
  - `location` — the exact location in the draft (e.g., "sentence 3 of caption", "the CTA line")
  - `description` — what the violation is
  - `fix_instruction` — specific, actionable instruction the Drafter can execute

### 4. Catalog Verification (G Checks)

If `focus_equipment_id` is set, load the catalog item record and:
- **G1:** Cross-reference every spec claim in the caption against the catalog values. If a number, dimension, capacity, or feature is stated, it must match the catalog exactly.
- **G2:** Verify the item status is `active` or `seasonal`.
- **G3:** Check for rounded or inflated specs (e.g., "12,000 lbs" when catalog says "11,800 lbs").

### 5. Determine Verdict

Apply the verdict hierarchy:
1. If any check has `verdict_level = hard_fail` → overall verdict is `hard_fail`
2. If `revision_round >= 3` and any check has `verdict_level = soft_fail` → overall verdict is `hard_fail` (escalation)
3. If any check has `verdict_level = soft_fail` (and no hard_fails, and revision_round < 3) → overall verdict is `soft_fail`
4. If all checks pass → overall verdict is `pass`

### 6. Assemble Output

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

### 7. Update Content Queue Row

- On `pass`: Update status to `critiqued`. Make.com will advance to Slack approval.
- On `soft_fail`: Keep status as `drafted`. Make.com will route back to Drafter.
- On `hard_fail`: Update status to `hard_fail`. Make.com will post escalation to Slack.

## Autonomous Decisions

- Whether each check passes or fails (applying the checklist rules)
- The specific fix instruction for each failure
- Whether a spec claim is close enough to the catalog value or a violation (G3 — no rounding allowed)
- The overall verdict based on the hierarchy

## Human-in-Loop

None. The Critic operates fully autonomously. Its output feeds into the Slack approval flow (for passes) or back to the Drafter (for soft_fails). Hard_fails go to Slack for owner attention.

## Error Handling

| Error | Behavior |
|-------|----------|
| Catalog item not found (for G checks) | Skip G checks, add warning: "Catalog item not found — spec verification skipped." Pass on all other checks as normal. |
| Brand voice file not accessible | Log error, post to `{{SLACK_ERROR_CHANNEL}}`, abort evaluation. The draft stays at `drafted` and Make retries. |
| Checklist file not accessible | Same as above — abort and notify. |
| Content Queue row missing expected fields | Flag missing fields as individual soft_fail items with fix instruction "Field [X] is missing from the draft." |

## Failure Mode

If the Critic fails to run, the draft stays at `status = drafted`. Make.com retries on the next cycle. The draft does not advance to approval without a Critic evaluation. This is a hard gate — there is no bypass.

## What the Critic Does NOT Do

- **Does not evaluate image quality.** The Critic reviews the caption, CTA, formatting, and factual claims. It does not assess whether the generated image looks good. Image quality is the owner's judgment at the Slack approval step.
- **Does not rewrite content.** The Critic provides fix instructions. The Drafter applies them.
- **Does not evaluate hooks.** Hook quality is governed by the Hook Creation Skill's scoring rubric. The Critic checks only that a hook exists and that the caption body does not compete with it (check B9).
- **Does not check media format.** Whether the Strategist's media format assignment was correct is not a Critic concern.

## Config Dependencies

| Config Path | Purpose |
|-------------|---------|
| `approval.error_channel` | Where to post hard_fail escalations |
| `catalog.spec_sheet_id` | Catalog sheet for spec verification |
| `strategy.pricing_in_posts` | Pricing policy for A1 check |

All other rules come from the skill files loaded at runtime, not from business_config.yaml directly.

---

*Critic Agent — Workflow SOP v1*
*Last updated: 2026-05-20*
