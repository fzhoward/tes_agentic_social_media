# Orchestration Contract — n8n Migration Spec

**Purpose:** Complete, code-verified specification of every integration point the orchestrator must reproduce when migrating from Make.com to n8n Cloud. The agents and tools are unchanged. Only the orchestrator changes.

**Ground rules for this document:** Every behavioral claim cites the file and line it was read from. Anything not directly verifiable from the code is labeled as either INFERRED or UNKNOWN-FROM-CODE and must be resolved from the Make.com UI before cutting over.

**Source branch:** `evals/critic-variance-harness` (current branch at time of writing).

---

## Summary Table

| Orchestration concern | Current Make scenario | Endpoint / CLI | Schedule | Auth |
|---|---|---|---|---|
| Asset Indexer | Unnamed (nightly) | `POST /run/indexer` → `python -m agents.asset_indexer` | UNKNOWN-FROM-CODE | Bearer EXECUTOR_TOKEN |
| Strategist planning | Unnamed (daily) | `POST /run/strategist` → `python -m agents.strategist` | UNKNOWN-FROM-CODE (workflow SOP says "Daily at 6:00 AM ET") | Bearer EXECUTOR_TOKEN |
| Draft-cycle (revision loop) | Unnamed (periodic) | `POST /run/draft-cycle` → `python -m agents.draft_cycle` | UNKNOWN-FROM-CODE | Bearer EXECUTOR_TOKEN |
| Approval card posting | "Make Scenario 5" | `POST /run/approval-card` → `python -m tools.approval_card` | UNKNOWN-FROM-CODE (event-driven: queue row reaches `awaiting_approval`) | Bearer EXECUTOR_TOKEN |
| Missed approval reschedule | "Make Scenario 7" | `POST /run/approval-card-reschedule` → `python -m tools.approval_card --reschedule` | UNKNOWN-FROM-CODE | Bearer EXECUTOR_TOKEN |
| Slack button actions (approve / reject / edit_caption / regen_media / regen_all) | "Slack Scenario 6" | `POST /slack/interactivity` → `python -m tools.approval_router --payload ...` | Event-driven (Slack sends directly) | Slack v0 signature (SLACK_SIGNING_SECRET) |
| Edit-caption threaded reply capture | Slack Events API | `POST /slack/events` → `python -m tools.approval_router --edit-commit ...` | Event-driven (Slack sends directly) | Slack v0 signature (SLACK_SIGNING_SECRET) |
| Media delivery to Slack | (none — executor serves directly) | `GET /media/<row_id>/<token>` | On-demand (Slack fetches when rendering card) | HMAC token in path (MEDIA_URL_SECRET) |
| Liveness probe | (none) | `GET /healthz` | On-demand | None |

---

## Section 1: Executor Service Overview

The executor (`tools/executor.py`) is a thin Flask application that the orchestrator calls. It does not contain pipeline logic. Its entire job is to shell out to the correct CLI module and return its JSON output. Keeping it a "dumb runner" means a failure in one handler cannot crash the listener (docstring, line 1–8).

**Production runtime** (executor.py line 53):
```
.venv/bin/gunicorn --workers 2 --bind 127.0.0.1:8000 tools.executor:app
```

The service binds to localhost:8000 and sits behind a reverse proxy / tunnel that provides the public HTTPS URL. The `approval.media_base_url` in `business_config_tes_rentals.yaml` (line 91) is `https://executor.tessys.org`, confirming a public ingress exists. **Migration prerequisite:** n8n Cloud is off-box; it must reach the executor over HTTPS. If Make.com currently calls `/run/*` from the cloud, that public ingress already exists and n8n can use the same hostname. Verify this before cutover.

**Key timeouts** (executor.py lines 83–89):
- Synchronous subprocess cap: 120 seconds (all `/run/*` endpoints except the two below).
- Draft-cycle subprocess cap: 1800 seconds / 30 minutes (background thread).
- Strategist subprocess cap: 1800 seconds / 30 minutes (background thread).

---

## Section 2: Endpoint-by-Endpoint Contracts

### 2.1 GET /healthz

**Auth:** None.

**Request:** No body, no query params.

**What it does:** Returns a static JSON object inline (no subprocess). (executor.py lines 273–275)

**Response:**
- `200 OK`: `{"ok": true, "service": "executor"}`
- No failure path documented (Flask itself would 500 on an unhandled exception, which should never happen here).

**n8n use:** Liveness check before triggering any scheduled run. Wire as a prerequisite step or use n8n's built-in health-check node.

---

### 2.2 POST /run/indexer

**Maps to:** The unnamed Make cron scenario that triggers the Asset Indexer nightly.

**Auth:** `Authorization: Bearer <EXECUTOR_TOKEN>` (executor.py lines 279–281, `_check_bearer`).

**Request:**
- Content-Type: not inspected — no body is read.
- No required or optional body fields.

**What it does (sync):**
Shells out: `python -m agents.asset_indexer` (executor.py line 282)
Runs from REPO_ROOT with `BUSINESS_CONFIG_PATH` set. Waits up to 120 seconds. (executor.py lines 108–143)

The Asset Indexer reads the Image Metadata sheet and Equipment Catalog, matches images to catalog items by category + model, updates `primary_image_id` and `image_count`, creates new catalog rows for unmatched equipment, and posts a plain-text summary to the Slack `#system-health` channel. It requires no LLM calls. (agents/asset_indexer.py lines 1–12, 246–263)

CLI accepts only `--dry-run` (agents/asset_indexer.py lines 298–308). The executor calls it with no arguments, so it runs in live mode every time.

**Response:**
- `200 OK` (exit_code 0): `{"exit_code": 0, "result": <agent JSON>}`
- `500` (non-zero exit): `{"exit_code": N, "result": <agent JSON or stderr string>}`
(executor.py lines 283–285)

**Side effects:** Writes to Equipment Catalog sheet; posts to Slack `#system-health` channel.

**Schedule:** UNKNOWN-FROM-CODE. Workflow SOP says "Nightly (configurable in Make)" (workflows/asset_indexer.md line 13). Exact time must be read from the Make scenario.

---

### 2.3 POST /run/strategist

**Maps to:** The unnamed Make cron scenario that triggers the Strategist daily.

**Auth:** `Authorization: Bearer <EXECUTOR_TOKEN>` (executor.py lines 302–304).

**Request:** No body read.

**What it does (async — 202 + background thread):**
The Strategist makes many sequential LLM calls and exceeds the synchronous 120-second cap (executor.py lines 306–309). The endpoint starts a daemon thread running `_dispatch_strategist` (executor.py lines 288–298), returns `202` immediately, and lets the thread run to completion.

The background thread shells out: `python -m agents.strategist` (executor.py line 293), with a 30-minute timeout.

The Strategist plans a rolling 7-day content calendar. It reads the Equipment Catalog, Reviews sheet, Content Queue, and Performance Log; calls an LLM (INFERRED: the strategist uses Claude/Anthropic — see agents/strategist.py for the actual import); validates and enforces constraints; writes new rows to the Content Queue with `status = planned`; and posts a run-summary to the Slack `#system-health` channel (agents/strategist.py lines 1497–1509, 1463–1492).

CLI accepts only `--dry-run` (agents/strategist.py lines 1848–1859). The executor calls it with no arguments, so it runs live.

**Response (immediate, before background work completes):**
- `202 Accepted`: `{"accepted": true}`
- `401` on bad auth: `{"success": false, "error": "unauthorized"}`

**Outcome logging:** The background thread logs the exit code and result JSON to stdout (executor.py lines 295–298), captured by journalctl.

**Side effects:** Writes new `planned` rows to Content Queue sheet; posts to Slack `#system-health`.

**n8n implication:** n8n will receive `202` immediately and must not treat this as an error. The actual result is logged on the machine, not returned to the caller. If n8n needs to know whether the run succeeded, it must either poll a separate status source (the Content Queue) or accept the fire-and-forget pattern.

**Schedule:** UNKNOWN-FROM-CODE. Workflow SOP says "Daily at 6:00 AM ET (configurable)" (workflows/strategist.md line 13). Exact time must be read from the Make scenario.

---

### 2.4 POST /run/draft-cycle

**Maps to:** The unnamed Make scenario that triggers the front-half revision loop (the executor docstring labels it "Front-half revision loop," line 17).

**Auth:** `Authorization: Bearer <EXECUTOR_TOKEN>` (executor.py lines 329–331).

**Request:** No body read.

**What it does (async — 202 + background thread):**
Starts a daemon thread running `_dispatch_draft_cycle` (executor.py lines 328–336), returns `202` immediately.

The background thread shells out: `python -m agents.draft_cycle` (executor.py line 315), with a 30-minute timeout.

**The revision loop runs entirely server-side within a single invocation of `agents.draft_cycle`.** n8n does not need to implement loop logic. The loop is: for each `planned` row in the lead-time window, run `agents.drafter --row-id <id>`, then `agents.critic --row-id <id>`; on `soft_fail`, write Critic output to a tempfile and run `agents.drafter --row-id <id> --revision-round N --previous-output <path>` followed by `agents.critic --row-id <id> --revision-round N --previous-output <path>`; repeat up to `MAX_REVISION_ROUNDS = 3`; escalate a round-3 `soft_fail` to `hard_fail`. This is all Python running inside one subprocess call. (agents/draft_cycle.py lines 1–506, particularly lines 257–337 for the loop body)

The draft_cycle CLI accepts: `--row-id` (single row, for testing), `--limit N` (cap in all-planned mode), `--dry-run`. The executor calls it with no arguments, so it runs in all-planned mode on all rows in the 36-hour lead-time window (agents/draft_cycle.py lines 453–478, strategy.lead_time_hours from config, default 36).

**Response (immediate):**
- `202 Accepted`: `{"accepted": true}`
- `401` on bad auth.

**Outcome logging:** Background thread logs exit code and result JSON to stdout/journalctl (executor.py lines 319–325).

**Side effects:** Writes to Content Queue rows (status transitions: `planned` → `drafted` → `awaiting_approval` or `hard_fail`); uploads media assets to Google Drive; may call Creatomate API for template rendering; calls Anthropic (Drafter) and OpenAI (Critic) APIs.

**Schedule:** UNKNOWN-FROM-CODE. Workflow SOP describes this as a queue-driven fire when a row has `status = planned` and `scheduled_datetime` is within the lead-time window. Whether Make triggers this on a short polling interval or an event is unknown from the code. Must be read from the Make scenario.

---

### 2.5 POST /run/approval-card

**Maps to:** "Make Scenario 5" (executor.py docstring, line 18).

**Auth:** `Authorization: Bearer <EXECUTOR_TOKEN>` (executor.py lines 340–342).

**Request:** No body read.

**What it does (sync):**
Shells out: `python -m tools.approval_card` (executor.py line 343). Runs with 120-second timeout.

The approval_card tool reads all Content Queue rows with `status = awaiting_approval`, checks each against recent Slack `#tes-sm-approval` channel history to avoid duplicate cards, and for any row without an existing card: builds a Slack Block Kit message (header, schedule, caption, Critic notes, media preview or link, five action buttons), posts it to the `#tes-sm-approval` channel, and writes the returned Slack message `ts` back to the Content Queue row as `slack_message_ts` (tools/approval_card.py lines 340–396, 407–437).

The CLI accepts `--dry-run` and `--row-id` flags, but the executor calls it with no arguments, so it runs in full mode.

**Response:**
- `200 OK`: `{"exit_code": 0, "result": <list of per-row outcomes>}`
- `500`: `{"exit_code": N, "result": ...}`

**Side effects:** Posts to Slack `#tes-sm-approval`; writes `slack_message_ts` to Content Queue.

**Trigger:** UNKNOWN-FROM-CODE. The SOP implies this fires when a row transitions to `awaiting_approval`. Whether Make polls the queue or catches a status-change event is not in the code. Must be read from the Make scenario.

---

### 2.6 POST /run/approval-card-reschedule

**Maps to:** "Make Scenario 7" (executor.py docstring, line 20).

**Auth:** `Authorization: Bearer <EXECUTOR_TOKEN>` (executor.py lines 350–352).

**Request:** No body read.

**What it does (sync):**
Shells out: `python -m tools.approval_card --reschedule` (executor.py line 353). Runs with 120-second timeout.

The `--reschedule` path scans for `awaiting_approval` rows whose `scheduled_datetime` has passed, increments a `[RESCHEDULED: N]` counter in `draft_notes`, slides `scheduled_datetime` forward by 24 hours, re-posts the approval card, and auto-rejects the row after `approval.auto_reject_after_misses` (configured as 3 in business_config_tes_rentals.yaml line 89) consecutive misses. Auto-rejection posts a warning to the `approval.error_channel` (`#tes-sm-system-errors`). (tools/approval_card.py lines 480–582)

**Response:**
- `200 OK`: `{"exit_code": 0, "result": <list of per-row outcomes>}`
- `500`: `{"exit_code": N, "result": ...}`

**Side effects:** Writes `scheduled_datetime` and `draft_notes` to Content Queue; posts to Slack `#tes-sm-approval`; posts auto-rejection notice to `#tes-sm-system-errors` on terminal miss.

**Schedule:** UNKNOWN-FROM-CODE. Must be read from the Make scenario. INFERRED: likely a daily cron that runs after posting hours to check for missed windows.

---

### 2.7 POST /slack/interactivity

**Maps to:** "Slack Scenario 6" (executor.py docstring, line 19).

**Auth:** Slack v0 HMAC signature over raw request body (executor.py lines 372–375, `_verify_slack_signature`). Required headers: `X-Slack-Signature`, `X-Slack-Request-Timestamp`. Requests with a timestamp more than 5 minutes old are rejected (replay protection, line 82). This is NOT bearer-token auth.

**Who calls this endpoint:** This endpoint performs its own Slack signature verification using `SLACK_SIGNING_SECRET`, which means Slack posts directly to this URL without going through Make.com as a relay. The signature check would fail if Make.com forwarded the request (Slack signs the original body against a shared secret; a forwarded request would need to re-sign or pass the secret through). **Confirm at cutover:** verify in the Slack app configuration whether the Interactivity Request URL points directly to the executor or to a Make webhook. If Slack posts directly (as the auth scheme implies), n8n does not need to handle this traffic at all — Slack → executor is already point-to-point.

**Request:**
- Content-Type: `application/x-www-form-urlencoded`
- Body field: `payload` (a JSON string containing the Slack `block_actions` payload) (executor.py lines 379–381)

**What it does (async — returns 200 immediately):**
Acknowledges within Slack's 3-second window, then spawns a background thread calling `_dispatch_router(payload_str)` (executor.py lines 383–386).

The background thread shells out: `python -m tools.approval_router --payload <json_string>` (executor.py lines 361–368).

The router parses the `action_id` (format: `"<action_type>::<row_id>"`), verifies the row is still at `awaiting_approval`, and dispatches to one of five handlers (tools/approval_router.py lines 53–58, 384–431):

| action_id prefix | Handler | What it does |
|---|---|---|
| `approve::<row_id>` | `_handle_approve` | Sets status `approved`, publishes via SocialBu, sets status `published`; on failure reverts to `awaiting_approval` |
| `reject::<row_id>` | `_handle_reject` | Sets status `rejected`, posts thread reply asking for reason |
| `edit_caption::<row_id>` | `_handle_edit_caption` | Resets status to `drafted`; posts thread prompt for revised caption; waits for `/slack/events` to capture the reply |
| `regen_media::<row_id>` | `_handle_regen_media` | Resets status to `planned`, clears `media_url` and `media_format_used`; caption preserved |
| `regen_all::<row_id>` | `_handle_regen_all` | Resets status to `planned`, clears all Drafter + Critic fields (14 fields total per DRAFTER_FIELDS + CRITIC_FIELDS, tools/approval_router.py lines 33–48) |

All five handlers post a thread reply and add a reaction emoji to the original card message (tools/approval_router.py lines 90–109).

**Response (immediate):**
- `200` with empty body on a signed request (executor.py line 386)
- `401` on invalid signature
- `400` if `payload` field is missing

**Side effects:** Writes to Content Queue; may publish to SocialBu; posts Slack thread replies and reactions; posts to `#tes-sm-system-errors` on publish failure.

---

### 2.8 POST /slack/events

**Auth:** Same Slack v0 signature verification as `/slack/interactivity` (executor.py lines 484–487).

**Who calls this endpoint:** Slack posts directly (same reasoning as `/slack/interactivity`). The URL must be registered in the Slack app's Event Subscriptions configuration. n8n is not in this path.

**Request:**
- Content-Type: `application/json` (unlike `/slack/interactivity` which is `application/x-www-form-urlencoded` — executor.py line 29 documents this distinction explicitly)
- Body: Slack Events API JSON payload

**Special case — URL verification handshake:**
On first setup (or when the Slack app config changes), Slack sends `{"type": "url_verification", "challenge": "..."}`. The endpoint responds synchronously with `{"challenge": "<value>"}` and `200` (executor.py lines 495–496). This must succeed for Slack to accept the URL.

**What it does (normal event path — async):**
For `event_callback` type events, spawns a background thread running `_dispatch_event(event)` and immediately returns `200` (executor.py lines 498–507). Always returns `200` on a signed callback, even for ignored events, to prevent Slack from retrying.

The background `_dispatch_event` function (executor.py lines 389–480) handles the edit-caption capture flow:
1. Ignores events that are not genuine threaded human replies: skips non-`message` types, bot messages, messages with a `subtype` (edited, deleted, etc.), un-threaded messages, and the thread parent itself (lines 399–424).
2. Looks up the Content Queue by `thread_ts == slack_message_ts` (the `ts` written by `post_approval_card` when the card was first posted) (lines 430–444).
3. Guards: row must have `status == "drafted"` (the state `_handle_edit_caption` sets). Reject-reason replies land in the same thread but the row is `rejected`, so this guard keeps them separate (lines 454–462).
4. Shells out: `python -m tools.approval_router --edit-commit --row-id <id> --caption-text <text>` (executor.py lines 465–474).

The `--edit-commit` path in the router (tools/approval_router.py lines 438–483):
- Requires row at `status = drafted`.
- Writes the new caption, sets status to `approved`, then immediately publishes via SocialBu (bypasses the Critic — the row already passed and the human edit is trusted).

**Response:**
- `200` (empty body) for all signed callbacks and the URL verification handshake.
- `401` on invalid signature.
- `400` on malformed JSON.

**Side effects:** Writes caption and status to Content Queue; publishes via SocialBu on successful `--edit-commit`.

---

### 2.9 GET /media/<row_id>/<token>

**Auth:** HMAC-SHA256 token in the URL path, NOT bearer auth (executor.py lines 511–525). Slack fetches image URLs from its server side unauthenticated (no headers controllable), so bearer auth cannot be used here. The HMAC token is computed as: `HMAC-SHA256(MEDIA_URL_SECRET, row_id).hexdigest()[:32]`. The approval card builder computes the identical token (tools/approval_card.py lines 61–72) to construct URLs embedded in the card.

**Request:** GET with no body. `row_id` and `token` are path parameters.

**What it does (sync, direct — no subprocess):**
This is the one documented exception to the "dumb runner, shells out" principle (executor.py lines 513–518). It imports `sheets_helpers` and `drive_helpers` directly because it must return raw image bytes, not JSON.

1. Validates HMAC token. Returns 403 if `MEDIA_URL_SECRET` is unset or token does not match.
2. Looks up the Content Queue row by `row_id` to find `media_url` (a Google Drive file ID, not a URL).
3. Attempts to download the file from Google Drive via the Drive API.
4. Falls back to a `.tmp/<row_id>_*` local file if the Drive fetch fails or `media_url` is empty (executor.py lines 551–574).
5. Serves raw bytes with appropriate MIME type (image/jpeg or image/png).

**Supported media:** Images only (`.jpg`, `.jpeg`, `.png`). Video rows (media_format_used in `VIDEO_MEDIA_FORMATS`) display a link in the approval card rather than an inline image, so the `/media/` route is not called for them (tools/approval_card.py lines 239–254).

**Response:**
- `200 OK`: raw image bytes with `Content-Type: image/jpeg` or `image/png`
- `403`: HMAC mismatch or missing secret
- `404`: row not found in queue, or no media file available

**n8n role:** None. This endpoint is called by Slack's servers when rendering the approval card. n8n does not need to interact with it.

---

## Section 3: Make Scenario Mapping and What Is Unknown

The executor docstring names four scenarios:

| Executor label | Endpoint | What the label tells us | What is UNKNOWN-FROM-CODE |
|---|---|---|---|
| (unnamed, nightly) | `POST /run/indexer` | Runs nightly per workflow SOP | Exact time, timezone, retry count, error handling |
| (unnamed, daily/periodic) | `POST /run/strategist` | Runs daily per workflow SOP (6 AM ET mentioned) | Exact time, whether it is a true cron or a polling scenario |
| (unnamed, periodic) | `POST /run/draft-cycle` | Triggered when planned rows enter lead-time window | Polling interval, whether Make polls the queue or fires on a content-queue webhook |
| Make Scenario 5 | `POST /run/approval-card` | Posts cards for `awaiting_approval` rows | Trigger mechanism (polling vs. event), polling interval, retry behavior |
| Make Scenario 7 | `POST /run/approval-card-reschedule` | Reschedules missed approvals | Schedule time, error handling |
| Slack Scenario 6 | `POST /slack/interactivity` | Receives Slack button events | Whether Slack posts directly to the executor or via a Make webhook relay |

**Items that MUST be inventoried from the Make.com UI before migration:**

1. The exact cron schedule time for the Strategist (6 AM ET is mentioned in the workflow SOP but is not in the code).
2. The exact cron schedule time for the Asset Indexer.
3. How `/run/draft-cycle` is triggered: cron interval, or event-based on queue changes.
4. How `/run/approval-card` is triggered: polling interval, event webhook, or transition-based.
5. The cron schedule for `/run/approval-card-reschedule`.
6. Whether Slack Interactivity and Events are pointing directly at the executor or at a Make webhook relay. (The auth scheme strongly implies direct, but the Make scenario may be routing them.)
7. Any retry counts, error routing, or notification logic that Make adds on top of the executor's HTTP responses.
8. Any in-scenario branching or filtering (e.g., Make checking the response body before proceeding).

**Orchestration logic that lives in the repo (NOT in Make):**

- The entire Drafter → Critic → revision loop (up to 3 rounds). This runs inside one call to `agents.draft_cycle` (agents/draft_cycle.py).
- Drafter validation (banned language, caption length, overlay hook rules) — inside `agents.drafter`.
- Critic deterministic pre-checks and LLM judgment — inside `agents.critic`.
- Approval card deduplication (checks recent Slack history before re-posting) — inside `tools.approval_card`.
- Approval card reschedule logic and auto-reject counter — inside `tools.approval_card`.
- Edit-caption flow state machine (`awaiting_approval` → `drafted` → `approved` → `published`) — inside `tools.approval_router`.
- SocialBu publish and rollback on failure — inside `tools.approval_router` and `tools.socialbu_publish`.
- Media HMAC token generation and validation — inside `tools.executor` and `tools.approval_card`.

---

## Section 4: The Revision Loop — Where It Runs

**The loop is entirely server-side.** n8n does not need to re-trigger the Drafter or Critic between rounds.

When `POST /run/draft-cycle` is called, the executor starts a background thread that invokes `python -m agents.draft_cycle`. Inside that single process, `run_cycle()` calls `run_row_cycle()` for each target row. `run_row_cycle()` runs the following sequence for a single row (agents/draft_cycle.py lines 257–337):

```
Round 1:
  _run_agent_cli(["agents.drafter", "--row-id", <id>])
  _run_agent_cli(["agents.critic", "--row-id", <id>])
  verdict = critic output

If verdict == "soft_fail" and round < 3:
  write Critic JSON to tempfile
  Round 2:
    _run_agent_cli(["agents.drafter", "--row-id", <id>, "--revision-round", "2", "--previous-output", <path>])
    _run_agent_cli(["agents.critic", "--row-id", <id>, "--revision-round", "2", "--previous-output", <path>])
    verdict = critic output

  If still "soft_fail":
    Round 3 follows the same pattern.
    The Critic escalates a round-3 soft_fail to hard_fail (agents/critic.py lines 1851–1858).

Terminal states: "pass" (row → awaiting_approval), "hard_fail" (row → hard_fail), or "error" (sub-agent failure).
```

Each sub-agent invocation has a 10-minute timeout (agents/draft_cycle.py line 67). The whole cycle runs inside a 30-minute background thread. Tempfiles holding Critic output are cleaned up in a `finally` block (agents/draft_cycle.py lines 332–337).

**What the workflow SOP says vs. what the code does:** The `critic.md` workflow SOP (lines 42–44) describes Make routing the row back to the Drafter on `soft_fail`. That description reflects the original design. The code was refactored (agents/draft_cycle.py docstring, lines 7–21) to run the entire loop in Python — "Option C hybrid." **The SOP is stale.** The loop does not require Make to re-trigger between rounds. This is the most important architectural fact for the n8n migration.

---

## Section 5: Slack Integration Contract

### Action IDs

Action IDs are embedded in Slack block buttons by `tools.approval_card.build_approval_blocks` (tools/approval_card.py lines 278–313). The exact format is `"<action_type>::<row_id>"`:

| Button label | action_id format | Example |
|---|---|---|
| Approve | `approve::<row_id>` | `approve::STR-20260527-FB-01` |
| Reject | `reject::<row_id>` | `reject::STR-20260527-FB-01` |
| Edit Caption | `edit_caption::<row_id>` | `edit_caption::STR-20260527-FB-01` |
| Regen Media | `regen_media::<row_id>` | `regen_media::STR-20260527-FB-01` |
| Regen All | `regen_all::<row_id>` | `regen_all::STR-20260527-FB-01` |

Parsed by `parse_action_id()` in tools/approval_router.py (lines 53–58) which splits on `"::"`.

### URL Registration in Slack App

Two URLs must be registered in the Slack app configuration:

1. **Interactivity Request URL:** `https://executor.tessys.org/slack/interactivity` (inferred from `approval.media_base_url` in config)
2. **Event Subscriptions Request URL:** `https://executor.tessys.org/slack/events`

The `/slack/events` endpoint handles the `url_verification` challenge automatically (executor.py lines 495–496).

**Slack subscription scope needed for `/slack/events`:** The endpoint listens for `message` events in channels (to capture threaded replies). The specific event type is a standard Slack message event. Ensure the Slack app has the `message.channels` or equivalent scope, and that the approval channel (`#tes-sm-approval`) is subscribed.

**Implication for n8n migration:** These two Slack endpoints are served by the executor, not by n8n. Slack must continue pointing at the executor URL. n8n only replaces the scheduled triggers and the five `/run/*` calls that Make currently makes.

---

## Section 6: Auth and Secrets — Orchestration Surface

### What n8n Cloud needs

n8n Cloud only calls the `/run/*` endpoints. It needs exactly one credential:

| Secret | Where it lives | Purpose |
|---|---|---|
| `EXECUTOR_TOKEN` | `.env` on the executor machine | Bearer token for all `/run/*` calls |

n8n should store this as a credential (HTTP Header Auth) and reference it in every HTTP Request node that calls the executor.

### What stays on the executor machine

The executor reads all other secrets from `.env` at startup. n8n does not need to know these:

| Env var | Purpose |
|---|---|
| `SLACK_SIGNING_SECRET` | Verifies Slack interactivity and events requests |
| `MEDIA_URL_SECRET` | Signs and validates `/media/<row_id>/<token>` URLs |
| `BUSINESS_CONFIG_PATH` | Path to `business_config_tes_rentals.yaml` (defaults to filename if unset, executor.py line 77–79) |
| `ANTHROPIC_API_KEY` | Used by `agents.drafter` (called inside draft-cycle) |
| `OPENAI_API_KEY` | Used by `agents.critic` (called inside draft-cycle) and `tools.infographic_generator` |
| `CREATOMATE_API_KEY` | Used by `tools.creatomate_helpers` (called inside draft-cycle via drafter) |
| `SOCIALBU_API_KEY` | Used by `tools.socialbu_publish` (called inside approval_router) |
| `SLACK_BOT_TOKEN` | Used by `tools.slack_helpers` (called by multiple agents and the executor itself for Slack posts) |
| Google OAuth credentials | `credentials_*.json` / `token_*.json` — Drive, Sheets, GBP access |

---

## Section 7: Agent Inventory and CLI Entrypoints

These are the commands the orchestrator ultimately drives (via the executor's subprocess calls).

### agents.asset_indexer

```
python -m agents.asset_indexer [--dry-run]
```
- No `--row-id` or selection flags. Always processes the full catalog.
- Output: JSON to stdout. (agents/asset_indexer.py lines 297–316)

### agents.strategist

```
python -m agents.strategist [--dry-run]
```
- No row selection; plans the full window.
- Output: JSON to stdout with `status`, `posts_planned`, `by_platform`, etc. (agents/strategist.py lines 1848–1859)

### agents.draft_cycle

```
python -m agents.draft_cycle [--dry-run] [--limit N] [--row-id <id>]
```
- Default (no `--row-id`): processes all `planned` rows in the `strategy.lead_time_hours` window (36 hours per config).
- `--row-id`: single-row mode for testing.
- `--limit N`: cap on rows in default mode.
- `--dry-run`: threads through to Drafter and Critic.
- Output: JSON with `status`, `rows_processed`, `outcomes` dict, per-row `rows` list. (agents/draft_cycle.py lines 453–497)

### agents.drafter (called by draft_cycle, not by the executor directly)

```
python -m agents.drafter --row-id <id> [--dry-run] [--revision-round N] [--previous-output <path>]
python -m agents.drafter --all-planned [--dry-run] [--limit N]
```
- `--revision-round` and `--previous-output` are mutually required on rounds > 1.
- `--all-planned` is mutually exclusive with revision flags.
- LLM: Anthropic (claude-sonnet-4-6, agents/drafter.py line 53).
- Output: JSON with `status`, `row_id`, `caption_length`, `media_format_used`, etc. (agents/drafter.py lines 2864–2945)

### agents.critic (called by draft_cycle, not by the executor directly)

```
python -m agents.critic --row-id <id> [--dry-run] [--revision-round N] [--previous-output <path>]
```
- `--row-id` is required.
- `--previous-output` accepts a JSON file path for round-2+ comparisons.
- LLM: OpenAI gpt-4o (agents/critic.py lines 38–39).
- Output: JSON with `status`, `verdict` (pass/soft_fail/hard_fail), `failed_check_count`, `critic_output`. (agents/critic.py lines 2310–2349)

### tools.approval_card (called by executor via /run/approval-card and /run/approval-card-reschedule)

```
python -m tools.approval_card [--dry-run] [--row-id <id>] [--reschedule]
```
- No flags: posts cards for all `awaiting_approval` rows.
- `--reschedule`: reschedules/auto-rejects overdue rows.
- Output: JSON list of per-row outcomes. (tools/approval_card.py lines 589–668)

### tools.approval_router (called by executor via /slack/interactivity and /slack/events)

```
python -m tools.approval_router --payload '<json_string>' [--dry-run]
python -m tools.approval_router --edit-commit --row-id <id> --caption-text '<text>'
```
- `--payload` and `--edit-commit` are mutually exclusive.
- `--edit-commit` requires both `--row-id` and `--caption-text`.
- Output: JSON with `action`, `row_id`, `success`, `message`, `error`. (tools/approval_router.py lines 490–581)

---

## Section 8: Network and Deployment Prerequisites for n8n Migration

1. **Public HTTPS ingress already exists** (confirmed by `approval.media_base_url: https://executor.tessys.org` in config). n8n Cloud can call the same hostname for all `/run/*` endpoints.

2. **Executor must be running** when n8n triggers arrive. The gunicorn process (`tools.executor:app`, 2 workers, bound to 127.0.0.1:8000) must be managed by systemd or equivalent so it survives reboots.

3. **Slack must continue pointing at the executor** for `/slack/interactivity` and `/slack/events`. These URLs do not change with the orchestrator migration.

4. **n8n needs only outbound HTTPS** to `https://executor.tessys.org` and the `Authorization: Bearer <EXECUTOR_TOKEN>` header. No inbound connections from the executor to n8n.

5. **202 responses are normal** for `/run/strategist` and `/run/draft-cycle`. n8n HTTP Request nodes must be configured to treat 202 as success, not as an error.

6. **No response body from async endpoints** contains the actual run outcome. Results are logged to stdout/journalctl on the machine. If n8n needs to verify completion, it must poll the Content Queue sheet directly (outside the executor).

---

## Open Questions

1. **Exact cron times for all scheduled scenarios** — must be read from the Make.com UI before building n8n schedules. The workflow SOP mentions "6:00 AM ET" for the Strategist; all other times are undocumented in the code.

2. **Draft-cycle trigger mechanism** — is it a cron (polling every N minutes), or does Make react to Content Queue changes via a watch/webhook? This determines whether n8n should poll on a schedule or watch a data source.

3. **Approval-card trigger mechanism** — same question as above. Does Make poll for `awaiting_approval` rows, or does it get triggered by a row status change?

4. **Slack endpoint routing** — do `/slack/interactivity` and `/slack/events` receive traffic directly from Slack, or does Make relay them? The Slack v0 signature check in the executor implies direct, but the Make scenario may have an intermediate step. Confirm by checking the Slack app's Interactivity and Event Subscriptions URLs in the Slack API dashboard.

5. **Make retry and error routing** — does Make retry failed `/run/*` calls, and if so how many times and with what delay? Does it post an alert on 500 responses? These behaviors need to be replicated in n8n.

6. **Any in-scenario branching** — does Make inspect the response body (e.g., checking `exit_code` or `outcome` fields) before proceeding to the next step? If so, those branches need n8n equivalents.

7. **Learning and Systems Health agents** — `agents/learning.py` and `agents/systems_health.py` are listed in CLAUDE.md's agent inventory but neither file exists in the repo at time of writing (`agents/learning.py` and `agents/systems_health.py` both returned "File does not exist"). Their executor endpoints, CLI contracts, and Make scenarios are therefore undocumented here. Verify whether they exist under a different path or have not yet been built before completing the migration spec.
