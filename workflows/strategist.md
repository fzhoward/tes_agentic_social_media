# Strategist Agent — Workflow SOP

*V1 — decides what to post, when, on which platform, with what objective and media format*

## Role

The Strategist is the system's editorial planner. It produces a rolling content plan by writing new rows to the Content Queue sheet. Each row is a fully specified assignment the Drafter can execute without ambiguity: what catalog item to feature, what content type to use, what objective to pursue, which platform to target, what media format to produce, whether to include text overlay, and when to publish.

## Trigger

| Trigger | Source | Frequency |
|---------|--------|-----------|
| Scheduled | Make.com cron | Daily at 6:00 AM ET (configurable) |
| On-demand | Slack `/replan` command via Make.com webhook | Manual kick by owner |

## Inputs

| Input | Source | Purpose |
|-------|--------|---------|
| Catalog sheet | Google Sheets (`catalog.spec_sheet_id`) | What items are available to feature, their specs, tags, categories, `last_posted`, `post_count` |
| Reviews sheet | Google Sheets (`catalog.reviews_sheet_id`) | Available customer reviews for Social Proof content — reviewer name, star rating, review text, excerpts, usage tracking |
| Content Queue sheet | Google Sheets (`drive.content_queue_sheet_id`) | Current planned/drafted/published posts — used to avoid duplicates, honor variety constraints, and check queue depth |
| Performance Log sheet | Google Sheets (`drive.performance_log_sheet_id`) | Recent post performance — used for objective ratio correction and content type weighting |
| Strategy Guidance | Drive file (`drive.strategy_guidance_file_id`) | Evolving playbook written by the Learning Agent — content type rankings, timing recommendations, pattern insights |
| Local Calendar sheet | Google Sheets (`drive.local_calendar_sheet_id`) | Optional — owner-maintained seasonal beats, local events, holidays |
| Brand voice file | Drive (per-business instance) | Post objective rules, CTA policies |
| Content Type Definitions | Drive (portable) | Available content types, objective leans, platform fit |
| CTA Skill | Drive (portable) | Which CTA types are allowed per objective |
| Business config | `business_config.yaml` | Posts per week per platform, objective ratio, variety constraints, pricing policy, platform list |

## Outputs

| Output | Destination | Description |
|--------|-------------|-------------|
| New Content Queue rows | Google Sheets | One row per planned post with all fields populated (see Content Queue Row Schema below) |

The Strategist does not post to Slack. Its output is rows in the queue. The Drafter picks them up when they enter the lead-time window.

## Content Queue Row Schema

Each row the Strategist writes contains:

| Field | Type | Set By | Description |
|-------|------|--------|-------------|
| `row_id` | string | Strategist | Unique ID for this queue entry |
| `status` | enum | Strategist (initial) | `planned` on creation. Downstream agents update to: `drafted`, `critiqued`, `awaiting_approval`, `approved`, `published`, `rejected`, `expired` |
| `platform` | enum | Strategist | `facebook`, `instagram`, `gbp` |
| `scheduled_datetime` | datetime | Strategist | Target publish time |
| `objective` | enum | Strategist | `brand_awareness` or `lead_generation` |
| `content_type` | string | Strategist | One of the 9 content types from Content Type Definitions |
| `focus_equipment_id` | string (nullable) | Strategist | Catalog `item_id` of the featured item, if applicable |
| `angle` | string | Strategist | Short direction for the Drafter: the hook idea, framing, or specific aspect to focus on |
| `cta_type` | string | Strategist | From CTA Skill: `call`, `dm`, `click`, `comment`, `save`, `directions`, `book`, `visit`, `none` |
| `media_format` | enum | Strategist | `image2_enhanced`, `image2_text_overlay`, `creatomate_text_overlay`, `creatomate_video` |
| `text_overlay` | boolean | Strategist | Whether the image should include hook text overlay. Derived from `media_format` (true for `image2_text_overlay` and `creatomate_text_overlay`, false for `image2_enhanced` and `creatomate_video`) |
| `source_image_id` | string (nullable) | Strategist | Drive file ID of the recommended source photo from the catalog item's images. Nullable if the Drafter should select. |
| `review_id` | string (nullable) | Strategist | GBP review ID from the Reviews Sheet. Set only for Social Proof posts. The Drafter uses this to look up review text, reviewer name, and excerpt for Creatomate templates. Empty for all other content types. |
| `draft_notes` | string | Strategist | Any additional context for the Drafter: seasonal tie-in, experience angle to use, specific spec to highlight |

## Processing Steps

### 1. Check Queue Depth

Read the Content Queue for each active platform. If any platform has more than `{{MAX_QUEUE_DEPTH}}` posts at status `planned` or `awaiting_approval`, stop generating new plans for that platform. This prevents pile-up when the owner is unavailable for approvals.

### 2. Calculate Planning Window

Determine how many posts are needed to fill the next 7 days per platform, based on `{{POSTS_PER_WEEK_PER_PLATFORM}}`. Subtract any already-planned posts in that window. The remainder is the batch to plan.

### 3. Correct Objective Ratio

Read the last 14 days of published posts from the Performance Log. Calculate the actual `brand_awareness : lead_generation` ratio. Compare against the target ratio from `business_config.yaml` → `strategy.objective_ratio`. Bias the new batch to correct any drift.

Example: Target is 60:40 brand:lead. Last 14 days actual is 70:30. The new batch should lean heavier on lead generation to pull the ratio back toward target.

### 4. Select Content Types

For each post slot in the batch, select a content type using:

1. **Strategy Guidance rankings** — the Learning Agent's recommendations for which content types to lean into or de-emphasize
2. **Variety constraint** — no content type repeated consecutively on the same platform (`strategy.no_content_type_repeat_consecutive`)
3. **Platform fit** — reference Content Type Definitions for which types are strong/moderate/weak on each platform
4. **Calendar fit** — if Local Calendar has a seasonal beat in the planning window, preferentially pick content types that match
5. **Objective alignment** — pick content types whose native lean matches the needed objective, or explicitly override the lean with appropriate framing
6. **Review data availability** — Social Proof / Customer Story content type requires at least one usable review in the Reviews Sheet (`usable_for_social=TRUE`). If no usable reviews exist, do not plan Social Proof posts. If reviews are available, include Social Proof in the content mix — aim for 1-2 Social Proof posts per platform per week when 66+ usable reviews are available.

### 5. Select Catalog Items

For each post that features a catalog item:

1. **No repeat within 7 days** — do not feature an item that has `last_posted` within the last 7 days (`strategy.no_item_repeat_within_days`)
2. **Underrepresented items first** — favor items with low `post_count` relative to others in the same category
3. **Spec richness** — favor items with populated spec fields (they produce more specific, higher-quality posts)
4. **Active/seasonal only** — only select items with `status` = `active` or `seasonal`

### 5a. Select Review (Social Proof posts only)

When a post is assigned content type Social Proof / Customer Story:

1. Read the Reviews Sheet (filtered to `usable_for_social=TRUE`)
2. Prefer reviews with `times_used = 0` (unused reviews first)
3. Among unused reviews, prefer longer reviews (`review_length` descending) — they give the Drafter more material
4. If all reviews have been used at least once, pick the review with the oldest `last_used_date`
5. Write the selected `review_id` to the Content Queue row
6. Optionally pair with a catalog item — if the review mentions specific equipment or a job type that maps to a catalog item, set `focus_equipment_id` to that item. This enables the `photo_testimonial` and `photo_reveal` templates that accept `Equipment-Photo`. If no clear equipment match, leave `focus_equipment_id` empty.

### 6. Assign Media Format

For each post, assign one of the four media formats:

| Media Format | When to Use |
|-------------|-------------|
| `image2_enhanced` | Clean photo posts with no text overlay. Behind-the-scenes, equipment spotlight with strong source photo, social proof. |
| `image2_text_overlay` | Posts where the hook text on the image is the scroll-stopper. Alternates with `creatomate_text_overlay` for variety. |
| `creatomate_text_overlay` | Same use case as `image2_text_overlay`. Alternates with it to keep the feed visually fresh. |
| `creatomate_video` | Short motion video from a source still. Use for variety — no more than 2 video posts per platform per week unless Strategy Guidance recommends more. |
| `creatomate_review_image` | Social Proof posts — static review card. Default for Social Proof. |
| `creatomate_review_video` | Social Proof posts — motion review card. Use for variety, same frequency rules as `creatomate_video` (max 2 video posts per platform per week). |

**Review media format scoping:** Social Proof posts always use `creatomate_review_image` or `creatomate_review_video`. They never use `image2_enhanced`, `image2_text_overlay`, `creatomate_text_overlay`, or `creatomate_video` — those are for equipment-based content types. Likewise, non–Social Proof content types never use the review formats.

**Variety rules for text overlay alternation:**
- Track the last 3 text-overlay posts per platform
- If the last 2 used Image 2, the next text-overlay post should use Creatomate (and vice versa)
- This is a soft preference, not a hard constraint — if one tool is temporarily unavailable, the other can cover

**Video frequency:**
- Default: max 2 `creatomate_video` posts per platform per week
- The Learning Agent can adjust this in Strategy Guidance based on video performance data

### 7. Assign CTA Type

Based on the post objective and platform, assign the CTA type using the CTA Skill rules:

- **Brand awareness** → `comment`, `save`, or `none`
- **Lead generation** → `call` or `dm`
- **Link/click-through** → `click` (if the post is tied to a blog article or external content)
- **GBP** → map to the appropriate GBP button type

### 8. Assign Timing

For each post:

1. Start from per-platform optimal post times (from Strategy Guidance, or generic best-practice defaults if no data yet)
2. Respect minimum gap: `{{MIN_GAP_HOURS}}` between posts on the same platform
3. Respect lead time: schedule at least `{{LEAD_TIME_HOURS}}` in the future to give the Drafter and Critic time to process
4. Spread posts across the week — avoid clustering

### 9. Write to Content Queue

Write all new rows to the Content Queue sheet in a single batch. Each row has `status = planned`.

## Autonomous Decisions

The Strategist makes these decisions without human input:

- Which content types to use and in what mix
- Which catalog items to feature
- What media format to assign
- What CTA type to assign
- What angle or framing to use
- Post timing and platform distribution
- Objective ratio correction

All of these are steered by Strategy Guidance (written by the Learning Agent) and the business config. The owner's control point is downstream at Slack approval.

## Human-in-Loop

None at the planning step. The human gate is at approval (after Drafter + Critic). The owner can:

- `/replan` to trigger a fresh planning run
- `/pause [platform]` to halt planning for a platform
- Edit Strategy Guidance manually or via `/override <note>` to steer the next run

## Error Handling

| Error | Behavior |
|-------|----------|
| Catalog sheet empty or inaccessible | Log error, post to `{{SLACK_ERROR_CHANNEL}}`, abort run |
| Content Queue sheet inaccessible | Log error, post to `{{SLACK_ERROR_CHANNEL}}`, abort run |
| Performance Log empty (first run) | Use default objective ratio and generic timing — no performance data to correct against. Log info message. |
| Strategy Guidance missing (first run) | Use default content type weights and timing — no learned patterns yet. Log info message. |
| Local Calendar missing or empty | Skip calendar-based content type selection. Rely on seasonality from experience context only. |
| Reviews sheet empty or inaccessible | Skip Social Proof content type selection. Plan other content types normally. Log info message. |
| All queue slots already filled | No new rows written. Silent success. |

## Failure Mode

If the Strategist fails, the existing Content Queue rows continue through the pipeline (Drafter, Critic, approval, publishing). The system operates on its existing plan until the Strategist runs again. No posts are lost — new ones just aren't planned until the next successful run.

## Output Schema

The Strategist returns structured JSON to Make.com:

```json
{
  "run_timestamp": "2026-05-20T06:00:00Z",
  "planning_window_start": "2026-05-20",
  "planning_window_end": "2026-05-27",
  "posts_planned": 21,
  "by_platform": {
    "facebook": 7,
    "instagram": 7,
    "gbp": 7
  },
  "by_objective": {
    "brand_awareness": 13,
    "lead_generation": 8
  },
  "by_media_format": {
    "image2_enhanced": 6,
    "image2_text_overlay": 5,
    "creatomate_text_overlay": 6,
    "creatomate_video": 4
  },
  "skipped_platforms": [],
  "queue_depth_warnings": [],
  "errors": []
}
```

## Config Dependencies

| Config Path | Purpose |
|-------------|---------|
| `strategy.posts_per_week_per_platform` | How many posts to plan per platform |
| `strategy.objective_ratio` | Target brand_awareness:lead_generation split |
| `strategy.no_item_repeat_within_days` | Minimum days before re-featuring an item |
| `strategy.no_content_type_repeat_consecutive` | Prevent same content type back-to-back |
| `strategy.lead_time_hours` | Minimum hours between planning and scheduled time |
| `strategy.min_gap_hours` | Minimum hours between posts on the same platform |
| `strategy.pricing_in_posts` | Pricing policy (never / general_only / allowed) |
| `approval.max_queue_depth` | Max planned+awaiting posts before pausing |
| `platforms.active` | Which platforms to plan for |
| `catalog.spec_sheet_id` | Catalog sheet |
| `catalog.reviews_sheet_id` | Reviews sheet for Social Proof content planning |
| `drive.content_queue_sheet_id` | Content Queue sheet |
| `drive.performance_log_sheet_id` | Performance Log sheet |
| `drive.strategy_guidance_file_id` | Strategy Guidance file |
| `drive.local_calendar_sheet_id` | Local Calendar sheet (optional) |

---

*Strategist Agent — Workflow SOP v1*
*Last updated: 2026-05-20*
