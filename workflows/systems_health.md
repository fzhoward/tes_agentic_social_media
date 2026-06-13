# Systems Health Agent — Workflow SOP

*V1 — read-only observer across the full pipeline; reports on system health and recommends corrective actions*

## Role

The Systems Health Agent is the system's operations manager. It has read-only access across the entire system state — Content Queue, Performance Log, n8n execution logs, Slack approval activity, and API cost data. It does not modify any state, create content, or change any agent's behavior directly. Its sole output is a weekly Slack report and immediate critical-threshold alerts. The owner reads it, decides what to act on, and moves on.

The Systems Health Agent **never says "everything is fine."** Even in a good week, it surfaces the weakest link and suggests what would make the system incrementally better. It always pushes toward optimization, not just alerting on failures.

## Trigger

| Trigger | Source | Frequency |
|---------|--------|-----------|
| Scheduled (weekly report) | n8n cron | Weekly — `{{SYSTEMS_HEALTH_DAY}}` morning (default: Monday, after Learning Agent) |
| Critical threshold alert | n8n monitoring trigger | Immediate — fires when any critical threshold is crossed |
| On-demand | n8n webhook or executor endpoint | Owner triggers on-demand |

**Scheduling note:** The Systems Health Agent runs *after* the Learning Agent on Monday mornings. The Learning Agent rewrites Strategy Guidance first (default: Monday 7:00 AM), then the Systems Health Agent produces its report (default: Monday 8:00 AM). This way the health report can include whether the Learning Agent ran successfully and what it changed.

## Inputs

All inputs are **read-only**. The Systems Health Agent writes nothing to any sheet or file.

| Input | Source | Purpose |
|-------|--------|---------|
| Content Queue sheet | Google Sheets (`drive.content_queue_sheet_id`) | Pipeline flow: how many posts at each status, where drop-off occurs, stalled rows |
| Performance Log sheet | Google Sheets (`drive.performance_log_sheet_id`) | Published post metrics, missing metrics entries, orphaned rows |
| Strategy Guidance file | Drive file (`drive.strategy_guidance_file_id`) | Staleness check — when was it last updated? Is the Learning Agent silently failing? |
| n8n execution logs | n8n Executions API or data passed by trigger | Agent run success/failure rates, duration, error messages |
| Slack approval activity | Slack API | Approval timestamps, rejection reasons, regeneration actions, response latency |
| API cost data | Provider billing APIs | Per-agent API spend: LLM calls, image generation (DALL-E), video renders (Creatomate) |
| Business config | `business_config.yaml` | Thresholds, budget limits, queue depth limits, channel names |

## Outputs

| Output | Destination | Description |
|--------|-------------|-------------|
| Weekly health report | Slack `{{SLACK_HEALTH_CHANNEL}}` (default: `#system-health`) | Full system health digest — see report structure below |
| Critical threshold alert | Slack `{{SLACK_HEALTH_CHANNEL}}` | Immediate alert when a critical threshold is crossed |

The Systems Health Agent does **not** post to `#approvals` or `#system-errors`. It has its own channel to avoid competing with approvals or burying operational noise in error logs.

## Weekly Report Structure

The weekly report is a single Slack message with the following sections:

### 1. System Score

A simple **green / yellow / red** health indicator based on the aggregate of all checks below.

| Score | Meaning |
|-------|---------|
| 🟢 Green | All agents healthy, pipeline flowing, no critical flags |
| 🟡 Yellow | One or more non-critical issues detected (degraded performance, rising trends, minor data gaps) |
| 🔴 Red | Critical issue detected (pipeline stall, repeated failures, budget breach, severe data quality problems) |

The score is the *worst* individual score across all sections. One red subsection makes the whole report red.

### 2. Pipeline Summary

Posts planned vs. published this week, with the drop-off analysis:

- Total posts **planned** (status=planned created this week)
- Total posts **drafted** (reached status=drafted)
- Total posts **critiqued** (reached status=critiqued)
- Total posts **approved** by owner
- Total posts **published**
- Total posts **rejected** by owner (with top rejection reasons if available)
- Total posts **stalled** (stuck at a status for >24h)
- **Drop-off point:** Where the biggest loss occurs in the funnel (e.g., "12 planned → 10 drafted → 8 critiqued → 6 approved → 6 published — biggest drop at critic stage")

### 3. Agent Scorecards

One scorecard per agent:

| Metric | Description |
|--------|-------------|
| Success rate | Runs completed without error / total runs |
| Average run time | Mean execution duration |
| Failure count | Total errors this week |
| Soft-fail rate (Critic only) | Percentage of drafts that required revision |
| Hard-fail rate (Critic only) | Percentage of drafts escalated to owner |
| Cost | Estimated API spend this week (LLM tokens, image generation calls, video renders) |

Agents covered: Asset Indexer, Strategist, Drafter, Critic, Learning Agent.

### 4. Owner Activity (Bottleneck Analysis)

| Metric | Description |
|--------|-------------|
| Average approval time | Mean time from approval card posted → owner action |
| Median approval time | Median (more robust to outliers) |
| Rejection rate | Posts rejected / total presented for approval |
| Regeneration rate | Posts where owner requested media or full regen / total approved |
| Edit rate | Posts where owner edited caption / total approved |
| Longest wait | The single longest-pending post and how long it waited |
| Queue depth | Current unapproved posts per platform |

If approval time is trending upward or rejection/regeneration rates are high, include a specific observation (e.g., "Average approval time increased from 2.1h to 4.8h this week — queue may be backing up" or "You're regenerating media on 30% of posts — source photos in Drive may need refreshing").

### 5. Timing Chain Health

Are agents finishing within expected windows?

| Check | What it measures |
|-------|-----------------|
| Strategist completion | Did the daily 6 AM run finish before the lead-time window? |
| Drafter throughput | Are drafted posts keeping pace with planned posts? |
| Critic throughput | Are critiqued posts keeping pace with drafted posts? |
| Approval card delivery | Is the morning digest arriving on time? |
| Learning Agent completion | Did Monday's Learning Agent run complete successfully? |
| Metrics collection | Are T+24h and T+7d metrics snapshots firing on schedule? |

Flag any step that is consistently late or skipped.

### 6. Cost Trends

| Metric | Description |
|--------|-------------|
| Total API spend this week | Across all agents and tools |
| Per-agent breakdown | LLM costs (Anthropic, OpenAI), image generation (DALL-E/Image 2), video renders (Creatomate) |
| Week-over-week trend | Up, down, or flat — with percentage change |
| Projected monthly spend | Based on current weekly rate |
| Budget status | Current spend vs. `{{MONTHLY_BUDGET_LIMIT}}` if set |

Flag: "Drafter cost increased 40% week-over-week — DALL-E calls up from 18 to 29. Check if media format distribution has shifted."

### 7. Data Quality Flags

| Check | What it catches |
|-------|-----------------|
| Orphaned Content Queue rows | Rows with status=published but no matching Performance Log entry |
| Missing metrics | Posts published >24h ago with empty T+24h metrics, or >7d ago with empty T+7d metrics |
| Stale Strategy Guidance | Strategy Guidance file not updated in 2+ weeks (Learning Agent may be silently failing) |
| Broken sheet references | Content Queue rows referencing equipment IDs not found in the Catalog sheet |
| Empty required fields | Drafted posts missing caption, media_url, or CTA |
| Status inconsistencies | Rows that skipped a status step or have impossible status transitions |

### 8. Suggested Corrective Actions

Ranked by estimated impact. Each action is:
- **Specific** — names the problem and the agent/file/sheet involved
- **Actionable** — phrased as something the owner can actually do
- **Prioritized** — ordered from highest impact to lowest

Examples:
- "🔴 Drafter soft-fail rate is 45% (up from 15% last week) — review Brand Voice doc for clarity on tone, or check if recent Strategy Guidance changes introduced conflicting content type guidance"
- "🟡 You're regenerating media on 32% of posts — Drive source photos may need refreshing, or the image prompt skill may need tuning for recent content types"
- "🟡 Instagram T+24h metrics are empty on 60% of posts — SocialBu IG insights coverage is partial; this is a known limitation but worth monitoring"
- "🟢 Lowest performer this week: educational/tip content on GBP (0.8% engagement rate vs. 2.1% system average) — consider reducing GBP allocation for this content type"

**The corrective actions section is never empty.** Even when everything is green, the Systems Health Agent identifies the weakest link and suggests an improvement.

## Critical Threshold Alerts

These fire immediately (not waiting for the weekly report) to `{{SLACK_HEALTH_CHANNEL}}`:

| Threshold | Condition | Alert message |
|-----------|-----------|---------------|
| Pipeline stall | No posts have progressed to a new status in 24h+ | "⚠️ Pipeline stall detected — no posts have moved status in {N} hours. Check n8n execution logs and agent error logs." |
| Consecutive agent failure | Any single agent has failed 5+ times consecutively | "⚠️ {Agent} has failed {N} consecutive times. Last error: {error_message}. Pipeline may be blocked at the {stage} step." |
| Budget breach | Monthly API spend exceeds `{{MONTHLY_BUDGET_LIMIT}}` | "⚠️ Monthly API spend has reached ${amount} (budget: ${limit}). Top cost driver: {agent} at ${agent_cost}." |
| Queue overflow | Unapproved queue depth exceeds `approval.max_queue_depth` on any platform | "⚠️ {Platform} approval queue depth is {N} (limit: {max}). The Strategist will pause planning for this platform. Approve or reject pending posts to resume." |
| Learning Agent stale | Strategy Guidance not updated in 14+ days | "⚠️ Strategy Guidance has not been updated in {N} days. The Learning Agent may be failing silently. Check Monday execution logs." |
| Metrics blackout | No new metrics written to Performance Log in 48h+ (and posts have been published) | "⚠️ No metrics collected in 48h despite active publishing. Check metrics collection workflows and API credentials." |

## Processing Steps

### 1. Collect System State

Pull all inputs for the reporting period (default: last 7 days):

- Content Queue: all rows, all statuses, with timestamps
- Performance Log: all rows from the reporting period
- Strategy Guidance: file metadata (last modified date)
- n8n execution logs: success/failure/duration per workflow
- Slack activity: approval card timestamps, owner action timestamps, action types
- Cost data: API call counts and estimated spend per agent

### 2. Calculate Pipeline Funnel

Walk the Content Queue rows created or modified this week through the status progression: planned → drafted → critiqued → awaiting_approval → approved → published. Count how many reached each stage. Identify the biggest drop-off point.

### 3. Build Agent Scorecards

For each agent, compute:
- Success rate = successful runs / total runs
- Average duration = mean run time
- Cost = sum of API calls attributed to this agent × per-call cost estimates
- For the Critic specifically: soft-fail rate, hard-fail rate

### 4. Analyze Owner Behavior

From Slack activity data:
- Compute approval latency distribution (mean, median, max)
- Count rejections, regeneration requests, caption edits
- Identify any platform-specific patterns (e.g., always slow to approve GBP posts)

### 5. Check Timing Chain

Compare actual execution timestamps against expected windows:
- Strategist should complete by 6:30 AM
- Drafter should process planned rows within lead-time window
- Critic should process drafted rows within 2h of drafting
- Learning Agent should complete on its scheduled day
- Metrics snapshots should fire at T+24h and T+7d ±1h

### 6. Calculate Cost Trends

Sum API spend for the current week. Compare against previous week. Project monthly spend at current rate. Compare against budget limit if configured.

### 7. Run Data Quality Checks

Execute each check in the Data Quality Flags table. Record findings.

### 8. Generate Corrective Actions

Review all findings from steps 2-7. For each issue found:
- Assess severity (red/yellow/green)
- Write a specific, actionable recommendation
- Estimate relative impact (which fix would improve the system most?)
- Rank by impact

### 9. Compose and Post Report

Assemble all sections into a single Slack message. Post to `{{SLACK_HEALTH_CHANNEL}}`.

Format for Slack readability:
- Use emoji indicators (🟢🟡🔴) for quick scanning
- Keep each section concise — the full report should be readable in under 2 minutes
- Bold the corrective actions — that's the section the owner cares about most
- Include the reporting period dates at the top

## Autonomous Decisions

- What to flag and how to prioritize corrective actions
- Severity scoring (green/yellow/red) for each subsection
- Which trends are meaningful vs. normal variance
- How to phrase recommendations for maximum clarity
- Whether a critical threshold warrants an immediate alert

## Human-in-Loop

**None.** The Systems Health Agent is report-only. The owner reads the report and decides what to act on. There are no approval gates, acknowledgment requirements, or interactive elements.

The owner can:
- Read and ignore the report (no consequence)
- Act on specific corrective actions at their discretion
- Adjust thresholds in business_config.yaml to tune alert sensitivity
- Trigger an on-demand report via Make webhook

## Error Handling

| Error | Behavior |
|-------|----------|
| Content Queue inaccessible | Post partial report to `{{SLACK_HEALTH_CHANNEL}}` noting the gap. Compute what's possible from other inputs. |
| Performance Log inaccessible | Skip metrics and data quality sections. Note in report: "Performance Log unavailable — metrics analysis skipped." |
| n8n execution logs unavailable | Skip agent scorecards timing sections. Note in report. |
| Slack activity data unavailable | Skip owner bottleneck analysis. Note in report. |
| Cost data unavailable | Skip cost trends section. Note in report. |
| All inputs unavailable | Post a single alert: "⚠️ Systems Health Agent could not access any system data. All inputs returned errors. Investigate n8n, the executor, and Google Sheets connectivity." |
| `{{SLACK_HEALTH_CHANNEL}}` not writable | Log error to `{{SLACK_ERROR_CHANNEL}}` as fallback. |

**Graceful degradation:** The Systems Health Agent always produces *something*, even if inputs are partially missing. It reports on what it can access and clearly notes what it couldn't check.

## Failure Mode

If the Systems Health Agent fails entirely, **nothing breaks.** No other agent depends on its output. The pipeline continues operating. The only consequence is that the owner doesn't get the weekly health digest and may miss emerging issues until they become obvious.

This is low-urgency in terms of system impact, but the Systems Health Agent failing is itself a data quality signal — if it's been more than 2 weeks since a report, n8n execution logs should surface that and the error should be checked in `#system-errors`.

## What the Systems Health Agent Does NOT Do

- **Does not modify any sheet, file, or system state.** Read-only access across everything.
- **Does not change agent behavior.** It recommends; the owner decides.
- **Does not rewrite Strategy Guidance.** That's the Learning Agent's job.
- **Does not create or modify content.** It has no role in the content pipeline.
- **Does not interact with publishing APIs.** No SocialBu, no platform APIs.
- **Does not duplicate the Learning Agent's analysis.** The Learning Agent finds *content performance* patterns. The Systems Health Agent finds *operational efficiency* patterns. Different lenses on different data.
- **Does not require owner acknowledgment.** Unlike the Learning Agent's major-shift flags, the health report is purely informational.

## Relationship to Other Agents

| Agent | Relationship |
|-------|-------------|
| Learning Agent | Complementary. Learning Agent analyzes *what content works*. Systems Health analyzes *whether the system works*. Systems Health checks that the Learning Agent is running and producing updates. |
| Strategist | Systems Health monitors the Strategist's output volume and timing but never modifies planning behavior. |
| Drafter | Systems Health tracks Drafter success rate, cost, and soft-fail frequency but never modifies drafting behavior. |
| Critic | Systems Health monitors Critic pass/fail rates as a signal of Drafter quality. |
| Asset Indexer | Systems Health checks that the Asset Indexer is running and the Catalog sheet is being maintained. |

## Config Dependencies

| Config Path | Purpose |
|-------------|---------|
| `approval.slack_channel` | Where approval cards go (read for timing analysis) |
| `approval.error_channel` | Fallback channel if health channel fails |
| `approval.max_queue_depth` | Threshold for queue overflow alert |
| `drive.content_queue_sheet_id` | Content Queue sheet |
| `drive.performance_log_sheet_id` | Performance Log sheet |
| `drive.strategy_guidance_file_id` | Strategy Guidance file (staleness check) |
| `metrics.snapshot_intervals` | Expected metrics collection schedule |

### Config paths to add to business_config.yaml

These are new config entries needed for the Systems Health Agent:

| Config Path | Default | Purpose |
|-------------|---------|---------|
| `health.slack_channel` | `#system-health` | Where weekly reports and critical alerts post |
| `health.report_day` | `monday` | Day of the week for the scheduled report |
| `health.report_time` | `08:00` | Time for the scheduled report (after Learning Agent) |
| `health.monthly_budget_limit` | `null` | Monthly API spend threshold for budget alert (null = no limit) |
| `health.pipeline_stall_hours` | `24` | Hours without status progression before stall alert |
| `health.consecutive_failure_threshold` | `5` | Consecutive agent failures before alert |
| `health.strategy_guidance_stale_days` | `14` | Days without Strategy Guidance update before alert |
| `health.metrics_blackout_hours` | `48` | Hours without metrics collection before alert |

---

*Systems Health Agent — Workflow SOP v1*
*Last updated: 2026-05-21*
