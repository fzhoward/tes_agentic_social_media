# Drafter Agent — Workflow SOP

*V1 — produces the complete post draft: caption, CTA, hook, media asset, and first comment*

## Role

The Drafter takes a single planned Content Queue row and produces the finished draft: platform-tailored caption, CTA, media asset (image or video), and first comment (when applicable). The Drafter executes — it does not make strategic decisions. The Strategist has already decided what to post, on which platform, with what objective, content type, media format, and CTA type. The Drafter's job is to write and produce it at the quality bar defined by the brand voice and skill files.

## Trigger

| Trigger | Source | Frequency |
|---------|--------|-----------|
| Queue-driven | Make.com scenario | Fires when a Content Queue row has `status = planned` and `scheduled_datetime` is within the lead-time window (e.g., 36 hours out) |
| Re-draft (caption) | Make.com scenario | Fires when the owner selects "Edit caption" or "Regenerate all" on the Slack approval card |
| Re-draft (media only) | Make.com scenario | Fires when the owner selects "Regenerate media" on the Slack approval card |
| Revision (from Critic) | Make.com scenario | Fires when the Critic returns a `soft_fail` verdict with fix instructions |

## Inputs

| Input | Source | Purpose |
|-------|--------|---------|
| Content Queue row | Google Sheets | The planned post assignment: platform, objective, content_type, focus_equipment_id, angle, cta_type, media_format, text_overlay, source_image_id, draft_notes |
| Catalog item record | Google Sheets (if `focus_equipment_id` is set) | Item specs, description, tags, images |
| Brand voice file | Drive (per-business instance) | Voice rules, formatting rules, post objective rules, banned language, CTA phrasing |
| Hook Creation Skill | Drive (portable) | Generates scored hooks for caption and image overlay |
| Review Excerpt Selection Skill | Local (`skills/review_excerpt_selection.md`) | Selection criteria for the verbatim review excerpt used as Creatomate `Review-Text` on Social Proof posts |
| CTA Skill | Drive (portable) | CTA phrasing patterns and rules |
| Image Prompt — Social | Drive (portable) | Prompt template for Image 2 pipeline |
| Image Prompt — Universal Preamble | Drive (portable) | Prepended to all image generation prompts |
| Platform Style Skill | Drive (portable) | Character limits, platform constraints |
| Content Type Definitions | Drive (portable) | Content type definition for the assigned type |
| Business config | `business_config.yaml` | Phone number, website, DM platforms, contact info for CTA phrasing |
| Source photo | Google Drive (file ID from catalog or `source_image_id`) | Input to the image generation pipeline |
| Critic fix instructions | Make.com (if revision round) | Specific fixes to apply from the previous Critic evaluation |

## Outputs

| Output | Destination | Description |
|--------|-------------|-------------|
| Updated Content Queue row | Google Sheets | Caption, creative_hook_text, first_comment, cta_text, media_url, hook_text, image_overlay_text, draft_rationale, media_format_used. Status updated to `drafted`. |
| Generated media asset | Google Drive (`drive.generated_images_folder_id`) | The finished image or video file |

## Processing Steps

### 1. Load Context

Read all inputs: the Content Queue row, the catalog item record (if applicable), the brand voice file, and all relevant skill files. If this is a revision round, also load the Critic's fix instructions.

For Social Proof posts (`content_type = Social Proof / Customer Story`), the Strategist has set `review_id` on the Content Queue row. Read the Reviews Sheet (`catalog.reviews_sheet_id`) once at the start of the run and look up the matching row by `review_id`. Extract `reviewer_first_name`, `star_rating`, `review_text`, and `excerpt_long`. If the review isn't found, log a warning and continue — the caption can still draft from the angle, but media generation will skip.

### 2. Generate Hooks

Invoke the Hook Creation Skill to produce scored hooks for:

- **Caption hook** — the opening line of the post caption
- **Image overlay hook** — the text to overlay on the image (only when `text_overlay = true`)
- **Creative hook text** — a distinct ≤7-word hook used as the `Hook-Text` modification value on Creatomate equipment-post templates (`equipment_post_image`, `equipment_post_video`)

The Hook Creation Skill returns multiple candidates per channel with scores and a `recommended: true` flag. The Drafter uses the recommended hook. If no hook passes the minimum quality threshold, the Drafter uses the highest-scoring candidate and flags it in `draft_rationale`.

**`creative_hook_text` rules:**
- Generated using the same Hook Creation Skill that produces the caption hook
- Maximum 7 words — hard limit enforced in post-processing
- A **distinct** hook, not a truncation or substring of `caption_hook` (substring check runs in both directions)
- Optimized for bold typography on a still or motion creative — punchy, declarative, no trailing punctuation except `?`
- Always populated, regardless of `text_overlay` or `media_format` — the field is written to the Content Queue for every drafted row
- Injected as the `Hook-Text` modification value when rendering `creatomate_text_overlay` or `creatomate_video` (replacing the older behavior of truncating the caption hook). Other media formats write the value to the queue but do not consume it for image rendering.

### 3. Write Caption

Write the caption body following the brand voice rules, content type definition, and post objective rules.

**Social Proof posts:** when `review_id` is set and the review was loaded in step 1, the LLM prompt includes the full `review_text` and `reviewer_first_name`. The caption should reference the review naturally — quote, paraphrase, or frame it — and use the reviewer's first name. The caption must not fabricate content beyond what the review actually says.

**Review excerpt selection (Social Proof posts only):** the Drafter loads the `review_excerpt_selection` skill from `skills/review_excerpt_selection.md` and injects it into the LLM prompt alongside the full review text and the anti-fabrication rules. Before the LLM call, the Drafter looks up which Creatomate review template will render (deterministic rotation on `row_id`) and reads `max_review_text_chars` from that template's config entry under `creatomate.review_image.templates.<key>` or `creatomate.review_video.templates.<key>`; this value is passed into the prompt as the character budget. The LLM returns a `review_excerpt` field — a verbatim substring of the original review text, picked per the skill's criteria (pick the proof, not the pleasantry; favor specificity; self-contained readability). Two post-processing validations run before the excerpt is used:

1. **Substring check** — the excerpt must appear exactly in `review_text`. If not, fall back to `excerpt_long` from the Reviews Sheet and log a warning.
2. **Length check** — the excerpt must be ≤ `max_review_text_chars` for the selected template. If over, fall back to `excerpt_long` and log a warning.

The validated excerpt (or the fallback) is then injected as the `Review-Text` modification when the Creatomate review template renders. The old behavior of grabbing the first N characters of the review is gone — first-N-character openers tend to capture generic pleasantries ("Great company to work with") instead of the portion that justifies the 5-star rating.

**Caption structure:**
```
[Hook — prepended from Hook Creation Skill output]

[Caption body — written by the Drafter]

[CTA — phrased per CTA Skill rules, last element]
```

**Key rules (from brand voice and skill files):**
- Caption body does NOT include its own opening hook (the hook is prepended)
- Plain text only — no markdown, no emoji, no exclamation points, no em dashes, no hashtags
- Vertical stack formatting — every content line followed by a blank line
- Sentence length: median 8-12 words, max 18 words
- At least 2 fragment lines (5 words or fewer)
- Word count within target range for the post type and platform
- Reading level: 6th-7th grade Flesch-Kincaid
- One idea per post
- No pricing language (per `strategy.pricing_in_posts` policy)

**Objective-specific rules:**
- Brand awareness: no conversion CTA, engagement CTA optional, looser voice
- Lead generation: call or DM CTA required, CTA last, name situation before the ask
- Link post: create tension the linked content resolves, no URL in caption, CTA directs to first comment
- Advisory post: deliver one complete practical takeaway, no link reference

### 4. Phrase CTA

Using the CTA Skill, phrase the assigned `cta_type` with:
- The specific destination (phone number, DM prompt, first comment direction)
- Business-specific values from `business_config.yaml` (phone, website, booking URL)
- A reason to act that connects to the post content

The CTA is the last element of the caption.

### 5. Populate First Comment (Link Posts Only)

For link posts (`cta_type = click`), populate the `first_comment` field with the blog URL and a short prefix (e.g., "Full post here: [URL]").

### 6. Generate Media Asset

Based on the assigned `media_format`, execute the appropriate pipeline:

#### `image2_enhanced` (clean photo, no text)
1. Retrieve the source photo from Google Drive
2. Assemble the image prompt: Universal Preamble + Image Prompt Social Section B (clean photo)
3. Optionally append the content type hint from the Image Prompt Social guidance table
4. Send source photo + assembled prompt to OpenAI Image 2 `/images/edits` endpoint
5. The code pipeline handles: resizing to target dimensions, logo overlay, format conversion

#### `image2_text_overlay` (Image 2 photo + hook text)
1. Retrieve the source photo from Google Drive
2. Assemble the image prompt: Universal Preamble + Image Prompt Social Section A (text overlay), with `[HOOK_TEXT]` replaced by the recommended image overlay hook
3. Optionally append the content type hint
4. Send source photo + assembled prompt to OpenAI Image 2 `/images/edits` endpoint
5. The code pipeline handles: resizing, logo overlay, format conversion

#### `creatomate_text_overlay` (Creatomate template photo + hook text)
1. Retrieve the source photo from Google Drive
2. Upload/host the source photo at a URL accessible to Creatomate (or use Drive direct link if supported)
3. Call the Creatomate API `POST /v1/renders` with:
   - The text overlay template ID (from business config or system config)
   - Dynamic field values: source image URL (`Equipment-Photo`), `Hook-Text` ← `creative_hook_text`, any brand-specific values (colors, fonts)
4. Poll for render completion or receive webhook callback
5. Download the rendered output
6. The code pipeline handles: logo overlay, format conversion

#### `creatomate_video` (motion video from source still)
1. Retrieve the source photo from Google Drive
2. Upload/host the source photo at a URL accessible to Creatomate
3. Call the Creatomate API `POST /v1/renders` with:
   - The video template ID (from business config or system config)
   - Dynamic field values: source image URL (`Equipment-Photo`), `Hook-Text` ← `creative_hook_text`, any motion parameters
4. Poll for render completion
5. Download the rendered video
6. The code pipeline handles: logo overlay (if applicable to video), format conversion

#### `creatomate_review_image` (static review card from a Reviews Sheet row)
1. Pick a template from `creatomate.review_image.templates` (deterministic rotation by `row_id`)
2. Build modifications:
   - `Review-Text` → the LLM-selected `review_excerpt` validated as a verbatim substring of the original review text and ≤ the template's `max_review_text_chars` (falls back to `excerpt_long` from the Reviews Sheet on validation failure — see Step 3 above)
   - `Reviewer-Name` → `reviewer_first_name`
   - `Star-Rating` → `"★★★★★"` (Reviews Sheet only marks 5-star reviews usable)
   - `Equipment-Photo` → set ONLY when the selected template's `extra_dynamic_fields` includes `Equipment-Photo` AND a source image URL is available (currently `photo_testimonial` only)
3. Call Creatomate, poll for completion, download as PNG
4. No fallback chain — review image doesn't need a source photo, so there's no equipment-format to fall back to

#### `creatomate_review_video` (motion review card from a Reviews Sheet row)
1. Pick a template from `creatomate.review_video.templates`
2. Build modifications:
   - `Review-Text` → the LLM-selected `review_excerpt` (validated as substring + length); falls back to `excerpt_long` from the Reviews Sheet on validation failure
   - `Reviewer-Name` → `reviewer_first_name`
   - `Equipment-Photo` → set ONLY when the template's `extra_dynamic_fields` includes `Equipment-Photo` AND a source image URL is available (currently `photo_reveal` only)
3. Call Creatomate, poll for completion, download as MP4
4. **Fallback:** if the video render fails, fall back to `creatomate_review_image` using the same review data and the same validated excerpt

### 7. Write Draft Rationale

Write a 1-2 sentence note explaining the draft for the Critic and owner:
- What angle was taken and why
- Any quality concerns (thin experience input, low-scoring hooks, fallback image selection)
- If this is a revision round: what was changed per the Critic's instructions

### 8. Update Content Queue Row

Write all outputs to the Content Queue row:
- `caption`: Full caption (hook + body + CTA)
- `creative_hook_text`: The distinct ≤7-word hook used as Creatomate Hook-Text on equipment templates (always populated, even when the rendered media format does not consume it)
- `first_comment`: Blog URL with prefix (link posts only, empty otherwise)
- `cta_text`: The CTA line as a standalone field (for the Critic to evaluate independently)
- `hook_text`: The caption hook used
- `image_overlay_text`: The image overlay hook used (if text overlay, empty otherwise)
- `media_url`: Drive file URL or ID of the generated media asset
- `media_format_used`: The actual media format used (should match the assignment unless fallback occurred)
- `draft_rationale`: The rationale note
- `status`: Update to `drafted`

### 9. Update Review Usage (Social Proof Posts Only)

After media generation succeeds for a Social Proof post, find the matching `review_id` in the Reviews Sheet and update two cells:
- Increment `times_used` by 1
- Set `last_used_date` to today's ISO date

This feeds the Strategist's rotation logic (prefer unused reviews, then oldest used). Failures here log a warning but do not abort the draft — the main caption/media write has already succeeded.

## Revision Handling

When the Critic returns a `soft_fail`, the Drafter receives:
- The previous draft (full Content Queue row)
- The Critic's `failed_checks` array with specific fix instructions

The Drafter applies the specific fixes, re-runs any affected steps (e.g., if the CTA was wrong, re-phrase it; if the caption had banned language, rewrite that section), and resubmits. The Drafter does NOT regenerate the entire draft from scratch unless the issues are pervasive.

For media regeneration requests (from the Slack approval card), the Drafter re-runs step 6 only, keeping the caption unchanged.

## Autonomous Decisions

- Exact wording of the caption body
- Which hook candidate to use (within the Hook Creation Skill's scoring framework)
- Source photo selection (if `source_image_id` is not pre-assigned by the Strategist)
- Content type hint selection for the image prompt
- How to apply Critic fix instructions

## Human-in-Loop

None at the drafting step. The draft goes to the Critic, then to Slack approval.

## Error Handling

| Error | Behavior |
|-------|----------|
| Catalog item not found | Log error, flag row as `hard_fail`, post to `{{SLACK_ERROR_CHANNEL}}` |
| Source photo not accessible | Try alternate images from the catalog item. If none available, flag in `draft_rationale` and set Critic warning W1 |
| Image 2 API failure | Retry once. If still failing, fall back to `creatomate_text_overlay` or `image2_enhanced` (depending on whether text overlay was assigned). Log the fallback in `draft_rationale`. |
| Creatomate API failure | Retry once. If still failing, fall back to `image2_text_overlay` or `image2_enhanced`. Log the fallback. |
| Creatomate template not found | Log error, fall back to Image 2 pipeline for this post. Flag in `draft_rationale`. |
| Hook Creation Skill produces no passing hooks | Use highest-scoring candidate regardless of threshold. Flag in `draft_rationale`. |
| Caption exceeds platform character limit | Trim and re-check. If still over after trimming, flag for Critic review. |

## Failure Mode

If the Drafter fails on a single row, that row stays at `status = planned` and Make.com can retry on the next trigger cycle. Other planned rows are unaffected. If the Drafter fails repeatedly on the same row, it eventually expires (scheduled_datetime passes) and the Strategist backfills the slot on its next run.

## Config Dependencies

| Config Path | Purpose |
|-------------|---------|
| `contact.phone` | Phone number for call CTAs |
| `contact.website` | Website URL for visit CTAs |
| `contact.booking_url` | Booking URL for book CTAs |
| `contact.dm_platforms` | Which platforms support DM CTAs |
| `contact.google_maps_url` | Google Maps URL for directions CTAs |
| `catalog.primary_subject` | For image prompt placeholders |
| `brand_visuals.typography_style` | For image prompt text overlay |
| `brand_visuals.feel` | For image prompt brand feel |
| `brand_visuals.logo_file_id` | Logo for code pipeline overlay |
| `apis.image_generation_provider` | Which image API to call |
| `apis.image_generation_model` | Model ID for Image 2 |
| `apis.video_provider` | Creatomate |
| `drive.generated_images_folder_id` | Where to save generated assets |
| `strategy.pricing_in_posts` | Pricing policy |
| `catalog.reviews_sheet_id` | Reviews Sheet read by Social Proof posts to source review text + reviewer name |
| `creatomate.review_image.templates` | Static review-card templates (Bold Quote Card, Photo Testimonial, etc.) |
| `creatomate.review_video.templates` | Motion review-card templates (Star Cascade, Photo Reveal, etc.) |
| `creatomate.review_image.templates.<key>.max_review_text_chars` | Per-template character budget passed to the LLM as the excerpt's hard length cap |
| `creatomate.review_video.templates.<key>.max_review_text_chars` | Same, for review-video templates |

---

*Drafter Agent — Workflow SOP v1*
*Last updated: 2026-05-27*
