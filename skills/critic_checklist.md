# Critic Checklist — Portable

*V1 — quality gate the Critic agent runs against every draft before Slack approval*

## Purpose

Define the complete checklist the Critic agent evaluates against every drafted post before it advances to owner approval. The Critic is an independent QA pass using a different model/provider than the Drafter to break correlated errors. This is a portable skill. It contains no brand, industry, or business assumptions. The Critic loads the business-specific brand voice file and catalog data alongside this checklist at runtime.

## Verdict System

Every draft receives exactly one verdict:

| Verdict | Meaning | What Happens |
|---------|---------|-------------|
| **pass** | Draft meets all requirements. No changes needed. | Advances to Slack approval card for owner review. |
| **soft_fail** | Draft has fixable issues. The Drafter can resolve them with specific guidance. | Returns to the Drafter with specific fix instructions. The Drafter revises and resubmits to the Critic. |
| **hard_fail** | Draft has unfixable issues, or has failed 2 revision rounds, or violates a non-negotiable rule. | Escalates to Slack with the issue flagged. Does not advance to normal approval flow. |

### Revision Loop Rules

- A soft_fail returns the draft to the Drafter with specific fix instructions for every failed check.
- The Drafter revises and resubmits. The Critic re-evaluates the full checklist (not just the previously failed items).
- **Maximum 2 revision rounds.** If the draft still has soft_fail issues after 2 rounds of revision, the Critic escalates to hard_fail with all remaining issues listed.
- Hard_fail items are never sent back for revision. They escalate immediately on the first evaluation.

### Fix Instruction Requirements

When the Critic flags a violation, it must provide:

1. The specific checklist item that failed (by ID).
2. The exact location of the violation in the draft (e.g., "sentence 3 of the caption," "the CTA line," "the first comment field").
3. A specific fix instruction the Drafter can act on (e.g., "Remove the dollar amount '$250/day' from sentence 3" or "Replace the exclamation point at the end of line 5 with a period").

Vague feedback like "fix the tone" or "revise the CTA" is not acceptable. The Drafter should be able to make the fix without guessing what the Critic meant.

---

## Checklist Categories

Checks are organized into categories. Each check has an ID, a description, a verdict level (hard_fail or soft_fail), and the reference document that defines the rule.

### A. Non-Negotiable Rules (Hard Fail)

These checks trigger an immediate hard_fail on the first violation. They are never sent back for revision because they indicate a fundamental problem with the draft.

| ID | Check | Reference |
|----|-------|-----------|
| A1 | **No pricing language.** The draft must not contain dollar signs, dollar amounts, price ranges, or relative pricing language ("affordable," "competitive," "budget-friendly," "cheap," "cheapest"). | Brand voice: Pricing Mention Rules |
| A2 | **No emoji.** The draft must not contain any emoji characters. | Brand voice: Emoji Policy |
| A3 | **No fabricated claims.** The draft must not contain fabricated customer stories, invented anecdotes, made-up statistics, or factual claims about equipment that is not present in the catalog at all. **Scope boundary:** A3 does NOT cover spec claims about a catalog item — those are judged under G1/G3. If a claim concerns a catalog item's specs, dimensions, capacity, weight, or features, evaluate it as G1 (does it match the catalog row?), never as A3. A claim that matches the catalog row (including a qualitative spec such as a "reduced tail swing" machine where the catalog records that attribute) is supported and must not be failed under A3 or G1. | Brand voice: Default Content Rules |
| A4 | **No unsafe simplification.** The draft must not make any product, service, or equipment sound safer, easier, or more risk-free than it actually is. | Brand voice: Trust and Safety Rules |
| A5 | **CTA matches objective.** A brand awareness post must not contain a conversion CTA (call, DM, click, visit, book, directions). A lead generation post must contain a call or DM CTA. | CTA Skill: CTA Type by Post Objective |
| A6 | **No banned language.** The draft must not contain any phrase from the banned language list in the brand voice file. | Brand voice: Banned and Restricted Language |

### B. Formatting Rules (Soft Fail)

| ID | Check | Reference |
|----|-------|-----------|
| B1 | **No markdown formatting.** The draft must not contain markdown syntax (bold, italic, headers, bullet points). These render as literal characters on FB/IG. | Brand voice: Caption Formatting |
| B2 | **No exclamation points.** | Brand voice: Formatting Tone Defaults |
| B3 | **No em dashes.** | Brand voice: Formatting Tone Defaults |
| B4 | **No hashtags** (unless the brand voice explicitly allows them for the target platform). | Brand voice: Hashtag Policy |
| B5 | **Vertical stack formatting.** Every content line must be followed by a blank line. No dense paragraphs. *Deterministic pre-check.* | Brand voice: Caption Formatting |
| B6 | **Sentence length.** No sentence exceeds 18 words. Median sentence length should be 8-12 words. | Brand voice: Caption Formatting |
| B7 | **Fragment lines.** At least 2 fragment lines (6 words or fewer) per post for pacing. *Deterministic pre-check.* | Brand voice: Caption Formatting |
| B8 | **Word count in range.** Caption body (excluding hook) must fall within the target range for the post type and platform. | Brand voice: Word Count Targets |
| B9 | **No repeated hook.** The opening line (hook) must appear exactly once and must not be repeated later in the caption. *Deterministic pre-check.* | Brand voice: Hook Handling |
| B10 | **Reading level.** Content should target 6th-7th grade Flesch-Kincaid reading level. | Brand voice: Caption Formatting |
| B11 | **Creative hook text ≤ 7 words.** The `creative_hook_text` field must contain no more than 7 words. Distinct from `caption_hook` — neither string may be a case-insensitive substring of the other. | Drafter SOP: Creative Hook Text Rules |

### C. Content & Voice Rules (Soft Fail unless noted)

*C1, C2, and C6 are soft_fail. C3 and C4 are soft_fail when `focus_equipment_id` is set (a featured machine is expected) and **warning** when it is empty (specificity becomes a judgment call, not a gate). C5 and C7 are **warning** — they surface on the approval card for human review but do not block the verdict or trigger a revision round.*

| ID | Check | Reference |
|----|-------|-----------|
| C1 | **One idea per post.** The caption must develop a single main idea. Fail ONLY if the caption contains a second, distinct topic that does not support the main idea (e.g. a how-to tip that then pivots to an unrelated promotion or a different piece of equipment). Supporting detail, context, and a related CTA all belong to the one idea and do NOT count as a second idea. When in doubt about whether a second thread is "unrelated," treat it as supporting detail and PASS. | Brand voice: Caption Formatting |
| C2 | **Customer's job is the protagonist.** The customer's project, problem, or decision should be the subject of the post. Fail ONLY when the business or owner is clearly the hero — e.g. the company is the grammatical subject of the main value claim, or the caption leads with company virtues/credentials before the customer's situation. A caption that mentions T.E.S. (or the owner) while keeping the customer's job as the subject PASSES. When the framing is borderline or mentions both, PASS. | Brand voice: Default Content Rules |
| C3 | **No generic content (swap-test).** Apply the test: could a direct competitor paste their own name into this post and use it unchanged? If yes, it is too generic. Judge the substance, not a single proper noun — a post that names a place or the company but is otherwise interchangeable still fails the swap-test. On rows where `focus_equipment_id` is set this is a soft_fail (gates). On rows where `focus_equipment_id` is empty this is a **warning** only — surface it for human review but do not gate, because some genuinely good educational/behind-the-scenes posts read generally by design. | Brand voice: Brand Voice Quality Checklist |
| C4 | **Specificity standard.** The post should use concrete specifics — job types, site conditions, local decisions, or named machines — rather than vague generalities. **The form of specificity required depends on the row.** When `focus_equipment_id` is set, the post should name the specific machine/model where relevant, in addition to job/condition specificity; genericness about the featured machine is a C4 failure. When `focus_equipment_id` is empty (e.g. an Educational Tip or local-connection angle with no featured item), C4 is satisfied by specificity of job type, site condition, local scenario, or decision frame — do NOT require a named machine model, and do NOT treat the absence of a model as a C4 failure. The Drafter must not invent equipment that isn't tied to the row. Content-type expectations align with this: Equipment Spotlight and Social Proof rows will have `focus_equipment_id` set and named models are expected; Educational and Behind-the-Scenes rows often will not, and a named model should not be demanded where it would dilute a general lesson. When C4 is failed on a set-focus row, the output must tag the failure with `subreason: model_naming` (failure is only about naming the machine) or `subreason: specificity` (failure is about job-type, site-condition, or decision-frame specificity) so a model name already present in the caption can be deterministically cleared as a false negative. Tiering: when `focus_equipment_id` is set, C4 is a soft_fail (gates). When `focus_equipment_id` is empty, C4 is a **warning** only — surface a specificity concern for human review but do not gate, since specificity on a no-focus row is a judgment call. | Brand voice: Specificity Standard |
| C5 | **Geographic references earn their place.** Location references must add real meaning a local reader would recognize, not be used as filler. This is a **warning**, not a soft_fail — it surfaces on the approval card for human review and does not gate the verdict or trigger a revision round. | Brand voice: Default Content Rules |
| C6 | **No cheap-price positioning.** The post must not position the business as the cheapest, most affordable, or budget option. | Brand voice: Brand Position |
| C7 | **Voice anti-patterns (warning).** Flag ONLY when the caption contains concrete markers of off-brand voice: hype-heavy sales-page language, motivational-influencer cadence or rally-cries, marketing-agency / national-chain ad-speak, hollow superlatives, gimmicky enthusiasm, or framing that makes equipment work sound effortless or risk-free. Do NOT flag a caption merely for being improvable, for not being "more" plainspoken, or for any subjective gap when no anti-pattern marker is actually present. A plainspoken, calm, practical caption with no anti-pattern markers PASSES. This is a **warning**, not a soft_fail — it surfaces on the approval card for human review and does not block the row or trigger a revision round. | Brand voice: What T.E.S. Should Not Sound Like |

### D. Platform Compliance (Soft Fail)

| ID | Check | Reference |
|----|-------|-----------|
| D1 | **Caption length within platform limit.** FB: 63,206 chars. IG: 2,200 chars. GBP: 1,500 chars. | Platform Style Skill: Character Limits |
| D2 | **No URL in caption body** (for FB/IG link posts). The link must go in the first comment field. | Platform Style Skill: Link Behavior |
| D3 | **Link post CTA directs to first comment.** For FB/IG link posts, the CTA must point the reader to the first comment (not embed a URL). | Brand voice: Link Post Rules |
| D4 | **First comment field populated** (for link posts). The first_comment output field must contain the URL with a short prefix. | FB/IG Post Framework: Output Format |
| D5 | **GBP button type valid.** If a GBP CTA button is specified, it must be one of: CALL, LEARN_MORE, BOOK, ORDER, GET_DIRECTIONS, SIGN_UP. | Platform Style Skill: CTA Button Types |
| D6 | **GBP button URL provided** (when required). LEARN_MORE, BOOK, ORDER, and SIGN_UP require a URL. CALL and GET_DIRECTIONS do not. | Platform Style Skill: CTA Button Types |
| D7 | **No phone number or street address in the GBP post body** (Google "phone stuffing" content policy). Deterministic, **hard_fail**, GBP-only. The phone reaches customers via the CALL button (verified listing number, no digits in text); the address is shown on the listing itself. | Platform Style Skill: CTA Button Types |

### E. CTA Rules (Soft Fail unless noted)

| ID | Check | Verdict | Reference |
|----|-------|---------|-----------|
| E1 | **CTA is last element.** Nothing follows the CTA in the caption. | Soft fail | CTA Skill: General Rules |
| E2 | **One CTA only.** The post must not stack multiple actions. | Soft fail | CTA Skill: General Rules |
| E3 | **CTA names the action specifically.** "Contact us" is too vague. Must name the specific action (call, DM, visit, comment, etc.) with the destination. Bad examples: "Feel free to reach out", "Click link" Good example: "Drop a comments and tell us" "See full line up at*"| Soft fail | CTA Skill: General Rules |
| E4 | **Lead gen post names situation before the ask.** The post must describe the customer situation or problem before presenting the CTA. | Soft fail | Brand voice: Lead Generation Posts, CTA Skill |
| E5 | **No urgency language in CTA** unless the post is about a genuinely time-limited situation. | Soft fail | CTA Skill: General Rules |

### F. Objective Alignment (Soft Fail)

| ID | Check | Reference |
|----|-------|-----------|
| F1 | **Content type matches assignment.** The draft should match the content type the Strategist assigned (equipment spotlight, use-case scenario, educational tip, etc.). | Content Type Definitions |
| F2 | **Objective alignment.** The overall tone, framing, and CTA (or absence of CTA) must align with the assigned objective (brand awareness or lead generation). | Brand voice: Post Objective Rules |
| F3 | **Advisory post delivers standalone value.** If the post is an advisory/educational type, the reader should walk away with one useful takeaway without needing to click anything. | Brand voice: Advisory Post Rules |

### G. Catalog Verification (Soft Fail)

| ID | Check | Reference |
|----|-------|-----------|
| G1 | **Spec claims match catalog data.** Any specific claim about a catalog item (dimensions, capacity, weight, features, tail swing, etc.) must match the value in the catalog sheet. This check — not A3 — owns ALL spec claims about a catalog item: a claim that matches the catalog passes; a claim that is wrong, inflated, or refers to a field that is blank in the catalog row is flagged here as G1 (soft_fail, revisable). Qualitative attributes count: if the caption calls a machine "reduced tail swing" and the catalog `tail_swing` value is "Reduced," that is a match and passes. Do not escalate a catalog spec problem to A3. | Catalog Schema Template |
| G2 | **Item exists and is active.** The featured catalog item must have status "active" or "seasonal" in the catalog. Do not feature inactive or coming_soon items unless the Strategist explicitly assigned them. | Catalog Schema Template |
| G3 | **No inflated or rounded specs.** Spec values must match the catalog exactly. Do not round "11,800 lbs" to "12,000 lbs" or "14.2 ft" to "15 ft." | Catalog Schema Template |

### W. Warnings (Do Not Fail)

Warnings are noted in the Critic output and visible on the Slack approval card, but they do not affect the pass/soft_fail/hard_fail verdict.

| ID | Check | Notes |
|----|-------|-------|
| W1 | **Image not yet available.** The draft does not have a confirmed media asset attached. This is required before SocialBu can schedule the Instagram post. Make.com handles this gate at publishing time. | Platform Style Skill: Instagram Media Requirements |
| W2 | **Thin experience input.** The Strategist or Drafter flagged the experience input as thin. The post may be less specific than ideal. | Brand voice: Experience Integration |
| W3 | **GBP length close to truncation.** The GBP post body is within 20 characters of the ~150-200 character truncation threshold, meaning the hook may be partially cut off in the panel view. | Platform Style Skill: GBP Character Limits |

---

## Critic Output Format

The Critic returns structured JSON for every evaluation.

```json
{
  "queue_row_id": "STR-20260101-IG-01",
  "platform": "instagram",
  "revision_round": 1,
  "verdict": "pass | soft_fail | hard_fail",
  "failed_checks": [
    {
      "check_id": "A1",
      "category": "non_negotiable",
      "verdict_level": "hard_fail",
      "location": "sentence 3 of caption",
      "description": "Pricing language detected: '$250/day'",
      "fix_instruction": "Remove the dollar amount '$250/day' from sentence 3. Replace with 'Call us for rental rates.'"
    }
  ],
  "warnings": [
    {
      "check_id": "W1",
      "description": "No confirmed media asset attached. Required for Instagram scheduling."
    }
  ],
  "passed_checks": ["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9", "B10", "C1", "C2", "C3", "C4", "C5", "C6", "C7", "D1", "D2", "D3", "D4", "E1", "E2", "E3", "E4", "E5", "F1", "F2", "F3", "G1", "G2", "G3"],
  "notes": ""
}
```

### Field Definitions

| Field | Description |
|-------|-------------|
| queue_row_id | The Content Queue row ID being evaluated. |
| platform | The target platform for this draft (facebook, instagram, gbp). |
| revision_round | Which evaluation round this is (1, 2, or 3). Round 3 means the draft failed 2 revisions and this is the final escalation check. |
| verdict | The overall verdict. Determined by the highest-severity failed check: any hard_fail check → hard_fail. Any soft_fail check (no hard_fails) → soft_fail. No failures → pass. |
| failed_checks | Array of all failed checks with specific fix instructions. Empty array on pass. |
| warnings | Array of warning-level items. Do not affect the verdict. |
| passed_checks | Array of check IDs that passed. For audit trail and Slack card display. |
| notes | Free-text field for any additional context the Critic wants to surface. |

### Revision Escalation Logic

```
if revision_round >= 3 and verdict == "soft_fail":
    verdict = "hard_fail"
    notes = "Draft failed 2 revision rounds. Remaining issues escalated to hard_fail."
```

The Make.com scenario tracks the revision_round counter. The Critic receives it as input and includes it in the output.

---

## Evaluation Order

The Critic evaluates checks in this order. It does not short-circuit — all checks are evaluated on every run so the Drafter gets the complete list of issues in one pass.

1. **A (Non-Negotiable)** — checked first so hard_fails are identified immediately.
2. **B (Formatting)** — mechanical checks that are fast to evaluate.
3. **C (Content & Voice)** — requires reading comprehension and judgment.
4. **D (Platform Compliance)** — platform-specific constraint checks.
5. **E (CTA Rules)** — CTA-specific checks.
6. **F (Objective Alignment)** — higher-level alignment checks.
7. **G (Catalog Verification)** — requires cross-referencing catalog data.
8. **W (Warnings)** — noted last, do not affect verdict.

All checks run on every evaluation. The Critic does not skip categories based on previous results.

---

## Inputs the Critic Receives

| Input | Source | Purpose |
|-------|--------|---------|
| Drafted Content Queue row | Content Queue sheet | The draft caption, first_comment, CTA, media_url, platform, objective, content_type, focus_equipment_id |
| Brand voice file | Drive (per-business instance) | Voice rules, formatting rules, banned language, CTA phrasing standards, post objective rules |
| Catalog item record | Catalog sheet (if focus_equipment_id is set) | Spec fields for verifying factual claims |
| This checklist | Drive (portable) | The evaluation criteria |
| Platform Style Skill | Drive (portable) | Character limits, platform constraints |
| CTA Skill | Drive (portable) | CTA type rules, phrasing rules |
| Content Type Definitions | Drive (portable) | Content type definitions for alignment checks |
| revision_round | Make.com scenario | Which revision round this is (1, 2, or 3) |
| Previous Critic output | Make.com scenario (if revision_round > 1) | The previous failed_checks, so the Critic can verify whether previous issues were addressed |

---

*Critic Checklist — Portable v1*
*Last updated: 2026-05-20*
