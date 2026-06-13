# Learning Agent — Workflow SOP

*V1 — converts performance data into the system's evolving playbook*

## Role

The Learning Agent is the system's analyst. It reads performance data, identifies patterns, and rewrites Strategy Guidance — the file the Strategist reads on every planning run. The Learning Agent does not produce content or make immediate changes. It writes recommendations that take effect on the Strategist's next run.

## Trigger

| Trigger | Source | Frequency |
|---------|--------|-----------|
| Scheduled | n8n cron | Weekly — `{{LEARNING_AGENT_DAY}}` morning (default: Monday) |
| On-demand | n8n webhook or executor endpoint | Owner triggers on-demand |

## Inputs

| Input | Source | Purpose |
|-------|--------|---------|
| Performance Log sheet | Google Sheets (`drive.performance_log_sheet_id`) | Full history of published posts with metrics snapshots at T+24h and T+7d |
| Content Queue sheet | Google Sheets (`drive.content_queue_sheet_id`) | What each post looked like (content type, objective, media format, platform, CTA type, catalog item, angle) |
| Current Strategy Guidance | Drive file (`drive.strategy_guidance_file_id`) | The existing playbook to compare against and rewrite |
| Business config | `business_config.yaml` | Objective ratio target, min data points for patterns, major shift threshold |
| Owner overrides | Slack `/override <note>` captured via the executor | Manual instructions to incorporate into the next analysis |

## Outputs

| Output | Destination | Description |
|--------|-------------|-------------|
| Rewritten Strategy Guidance | Drive file (`drive.strategy_guidance_file_id`) | Updated playbook with new recommendations, rankings, and timing guidance |
| Slack weekly digest | `{{SLACK_APPROVALS_CHANNEL}}` | Summary of what changed and why, key performance highlights, any major shift recommendations |

## Performance Log Schema

Each row in the Performance Log represents one published post with:

| Field | Source | Description |
|-------|--------|-------------|
| `post_id` | SocialBu | The SocialBu post ID |
| `queue_row_id` | Content Queue | Links back to the full Content Queue row |
| `platform` | Content Queue | facebook, instagram, gbp |
| `objective` | Content Queue | brand_awareness, lead_generation |
| `content_type` | Content Queue | The assigned content type |
| `media_format` | Content Queue | image2_enhanced, image2_text_overlay, creatomate_text_overlay, creatomate_video |
| `cta_type` | Content Queue | The assigned CTA type |
| `focus_equipment_id` | Content Queue | Catalog item featured (nullable) |
| `posted_datetime` | Publishing step | Actual publish time |
| `day_of_week` | Derived | Day of week posted |
| `hour` | Derived | Hour of day posted |
| **Metrics (T+24h snapshot)** | | |
| `impressions_24h` | Platform APIs | |
| `reach_24h` | Platform APIs | |
| `engagement_24h` | Platform APIs | Reactions + comments + shares (FB), likes + comments + saves + shares (IG), views (GBP) |
| `clicks_24h` | Platform APIs | Link clicks, profile visits |
| `cta_conversions_24h` | Platform APIs | CTA-specific: phone calls, DMs received, booking clicks, etc. |
| **Metrics (T+7d snapshot)** | | |
| `impressions_7d` | Platform APIs | |
| `reach_7d` | Platform APIs | |
| `engagement_7d` | Platform APIs | |
| `clicks_7d` | Platform APIs | |
| `cta_conversions_7d` | Platform APIs | |

## Processing Steps

### 1. Load Data

Pull the last 90 days of the Performance Log. Join with the Content Queue to get the full post context (content type, media format, objective, CTA type, angle, catalog item).

If fewer than `{{MIN_DATA_POINTS_FOR_PATTERN}}` posts exist in total, produce a minimal Strategy Guidance with defaults and note that the system is still in the data collection phase. Skip pattern analysis.

### 2. Segment and Analyze

Segment the data along these dimensions and calculate performance metrics for each segment:

| Dimension | Segments | Primary KPI |
|-----------|----------|-------------|
| Platform | facebook, instagram, gbp | Engagement rate (engagement / reach) for awareness; CTA conversions for lead gen |
| Objective | brand_awareness, lead_generation | Engagement rate for awareness; CTA conversion rate for lead gen |
| Content type | The 9 defined types | Engagement rate (awareness) or CTA conversions (lead gen) |
| Media format | image2_enhanced, image2_text_overlay, creatomate_text_overlay, creatomate_video | Engagement rate across all posts using each format |
| CTA type | call, dm, click, comment, save, none | CTA conversion rate (CTA-dependent metric from business config) |
| Day of week | Mon-Sun | Engagement rate |
| Hour of day | Bucketed (morning, midday, afternoon, evening) | Engagement rate |
| Equipment category | From catalog | Engagement rate per category |

For each segment, identify:
- **Top quartile performers** — what do the best posts have in common?
- **Bottom quartile performers** — what do the worst posts have in common?
- **Repeatable patterns** — only flag patterns with N ≥ `{{MIN_DATA_POINTS_FOR_PATTERN}}` data points

### 3. Identify Actionable Patterns

Look for patterns that are:
- **Statistically meaningful** — enough data points to not be noise
- **Actionable** — the Strategist can do something with them (change content type mix, shift timing, adjust media format ratio)
- **Directional** — clear "lean into X" or "de-emphasize Y" guidance

Examples of actionable patterns:
- "Equipment spotlight posts on Instagram with Creatomate video get 2.3x the engagement rate of static image spotlights"
- "Lead gen posts with call CTAs on Facebook outperform DM CTAs 1.8:1 for CTA conversions"
- "Posts published between 6-8 PM on weekdays consistently outperform morning posts across all platforms"
- "Behind-the-scenes content type has the highest engagement rate for brand awareness on all platforms"

### 4. Evaluate Media Format Performance

Specifically analyze the Image 2 vs. Creatomate split:
- Compare engagement rates between `image2_text_overlay` and `creatomate_text_overlay` for the same content types
- Compare `creatomate_video` performance against all static formats
- Determine if the variety rotation is working (are alternating formats maintaining or improving engagement?) or if one tool consistently outperforms

### 5. Check Objective Ratio

Calculate the actual brand_awareness : lead_generation ratio over the last 14 days. Compare against the target from `business_config.yaml`. If the drift exceeds `{{MAJOR_SHIFT_THRESHOLD}}` percentage points, flag it as a recommendation to correct.

### 6. Incorporate Owner Overrides

If the owner sent `/override <note>` commands since the last run, read them and factor the instructions into the analysis. For example: "/override stop posting behind-the-scenes content for 2 weeks" → the updated Strategy Guidance should de-prioritize that content type.

### 7. Rewrite Strategy Guidance

Produce a new version of the Strategy Guidance file. The file is structured so the Strategist can parse it:

```markdown
# Strategy Guidance
## Last updated: [date] by Learning Agent

## Content Type Rankings
[Ordered list of content types from highest to lowest recommended usage,
with brief rationale for each ranking]

## Media Format Recommendations
[Guidance on Image 2 vs. Creatomate split, video frequency]

## Platform-Specific Notes
[Per-platform observations: what works best on FB vs. IG vs. GBP]

## Timing Recommendations
[Best days and hours per platform, based on data]

## CTA Effectiveness
[Which CTA types are performing best per objective and platform]

## Objective Ratio
[Current actual ratio vs. target, and correction recommendation if needed]

## Active Experiments
[Any A/B tests or deliberate variations the system should maintain]

## Owner Overrides
[Active override instructions and their expiration]

## Data Confidence
[How much data this guidance is based on, and which recommendations
are high-confidence vs. preliminary]
```

### 8. Detect Major Shifts

If any recommendation represents a change of more than `{{MAJOR_SHIFT_THRESHOLD}}` percentage points in content mix, flag it in the Slack digest and note that it requires owner acknowledgment before taking full effect. The Strategist should apply major shifts gradually (not all at once) unless the owner explicitly approves.

### 9. Post Slack Digest

Post a summary to `{{SLACK_APPROVALS_CHANNEL}}` containing:
- Key performance highlights from the last week
- What changed in Strategy Guidance and why
- Any major shift recommendations requiring acknowledgment
- Data confidence level (how many data points the patterns are based on)
- Active owner overrides and their status

## Autonomous Decisions

- Which patterns are real vs. noise (N ≥ threshold)
- What to recommend to the Strategist
- How to rank content types
- What timing adjustments to suggest
- How to weight media format performance

## Human-in-Loop

- Owner can `/override <note>` to inject instructions
- Major shifts (> threshold) are flagged and require acknowledgment
- The owner can comment-reply to the Slack digest to provide feedback
- Brand Voice is owner-controlled — the Learning Agent can recommend voice adjustments in the digest but does not modify the brand voice file

## Error Handling

| Error | Behavior |
|-------|----------|
| Performance Log empty or inaccessible | Post to `{{SLACK_ERROR_CHANNEL}}`: "Learning Agent could not access Performance Log. Strategy Guidance not updated." Abort run. |
| Fewer than `{{MIN_DATA_POINTS_FOR_PATTERN}}` total posts | Write minimal Strategy Guidance with defaults and a note: "System is in data collection phase. Recommendations will improve as more posts are published and measured." |
| Strategy Guidance file not writable | Log error, post digest to Slack with recommendations inline (the Strategist won't get the updated file, but the owner can see the analysis). |
| Content Queue not accessible | Skip the join step — analyze Performance Log fields only (limited analysis). Note in digest. |

## Failure Mode

If the Learning Agent fails, the Strategist continues using the previous version of Strategy Guidance. The system operates on stale but functional recommendations. No posts are lost or delayed. The Learning Agent failing is low-urgency — it means the system doesn't learn this week, not that it stops working.

## What the Learning Agent Does NOT Do

- **Does not modify Brand Voice.** Voice changes are the owner's decision. The Learning Agent can recommend voice adjustments in the Slack digest.
- **Does not modify the Critic Checklist.** Quality standards are fixed by the skill files.
- **Does not create or modify content.** It writes guidance that the Strategist reads.
- **Does not have real-time impact.** Changes take effect on the Strategist's next daily run.

## Config Dependencies

| Config Path | Purpose |
|-------------|---------|
| `metrics.learning_agent_cadence` | Run frequency (weekly) |
| `metrics.learning_agent_day` | Which day to run (monday) |
| `metrics.min_data_points_for_pattern` | Minimum N before flagging a pattern |
| `metrics.major_shift_threshold` | Percentage change that triggers owner acknowledgment |
| `metrics.snapshot_intervals` | Which snapshots exist (24h, 168h) |
| `metrics.lead_gen_success_metric` | CTA-dependent metric mapping |
| `strategy.objective_ratio` | Target brand:lead ratio for drift detection |
| `drive.performance_log_sheet_id` | Performance Log sheet |
| `drive.content_queue_sheet_id` | Content Queue sheet |
| `drive.strategy_guidance_file_id` | Strategy Guidance file to rewrite |
| `approval.slack_channel` | Where to post the weekly digest |
| `approval.error_channel` | Where to post errors |

---

*Learning Agent — Workflow SOP v1*
*Last updated: 2026-05-20*
