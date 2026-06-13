# Agent Instructions

You're working inside the WAT framework (Workflows, Agents, Tools) to build and operate the **Portable Agentic Social Media System**. This architecture separates concerns so that probabilistic AI handles reasoning while deterministic code handles execution. That separation is what makes this system reliable.

---

## The WAT Architecture

### Layer 1: Workflows (The Instructions)

- Markdown SOPs stored in Google Drive (file IDs in `business_config.yaml` under `workflows.*`)
- Each workflow defines the objective, required inputs, which tools to use, expected outputs, and how to handle edge cases
- Written in plain language, the same way you'd brief someone on your team
- Loaded at runtime by `tools/skill_loader.py` — never hardcoded into agent scripts

### Layer 2: Agents (The Decision-Maker)

- This is your role. You're responsible for intelligent coordination.
- Read the relevant workflow, run tools in the correct sequence, handle failures gracefully, and ask clarifying questions when needed
- You connect intent to execution without trying to do everything yourself
- Each agent is a Python script in `agents/` that accepts structured JSON input and returns structured JSON output
- Example: The Drafter agent reads `workflows/drafter.md`, loads the brand voice and few-shot library via `skill_loader`, pulls the equipment record from Sheets via `sheets_helpers`, generates a caption and media asset, then writes the result back to the Content Queue

### Layer 3: Tools (The Execution)

- Python scripts in `tools/` that do the actual work
- API calls, data transformations, file operations, Google Sheets/Drive interactions
- Credentials and API keys are stored in `.env`
- These scripts are consistent, testable, and reusable across agents

**Why this matters:** When AI tries to handle every step directly, accuracy drops fast. If each step is 90% accurate, you're down to 59% success after just five steps. By offloading execution to deterministic scripts, you stay focused on orchestration and decision-making where you excel.

---

## Project-Specific Principles

### Config-First — No Hardcoded Business References

All business-specific values come from `business_config.yaml` via `tools/config_loader.py`. If you need a file ID, sheet name, brand color, API endpoint, template ID, or contact detail — it comes from config. Never hardcode business references in agent or tool code.

This system is designed to be portable. A different business should be able to swap in their own `business_config.yaml` and run the same agents.

### Skill Loading Pattern

Skill files and workflow SOPs live in the repo as `.md` files under `skills/` and `workflows/`. Agents load them via `tools/skill_loader.py`, which resolves the path by convention: `load_skill("hook_creation")` reads `skills/hook_creation.md`, and `load_workflow("strategist")` reads `workflows/strategist.md`. The loader injects business config values into `{{PLACEHOLDER}}` tokens in the file text before returning it.

Example: A skill file containing `{{BUSINESS_NAME}}` and `{{SERVICE_AREA}}` gets resolved to "T.E.S. Rentals" and "North Florida" at load time.

The `skills.*` and `workflows.*` sections in `business_config.yaml` retain the original Drive file IDs for traceability but are no longer read by the loader.

### LLM Provider Routing

Different agents use different LLM providers. The Drafter uses Anthropic, the Critic uses OpenAI (per `apis.llm_provider_drafter` and `apis.llm_provider_critic` in config). Agents read their provider from config — never assume which LLM to call.

### Standalone First, Pipeline Later

Each agent is built and tested as a standalone unit before pipeline integration. The owner provides manual inputs, evaluates outputs, and iterates on prompts and SOPs until quality is proven. Content-producing agents (Strategist, Drafter, Critic) start generating usable assets immediately — weeks before the full orchestration layer exists.

The pipeline gets wired together with the orchestrator only after the individual agents are proven.

### n8n Orchestration

n8n (https://tessys.app.n8n.cloud) is the orchestrator. It triggers agent runners on a cron schedule via HTTP POST to the executor (`tools/executor.py`) `/run/*` endpoints with bearer auth (`EXECUTOR_TOKEN`). Agents don't know about n8n — they receive input, do their job, and return output. The orchestration layer is entirely separate from agent logic.

The five n8n workflows (managed via `tools/n8n_deploy.py`) are:
- **Indexer** — daily 02:00 ET → `POST /run/indexer`
- **Strategist** — Sunday 03:00 ET → `POST /run/strategist`
- **Drafter Cycle** — daily 04:00 ET → `POST /run/draft-cycle` (runs `agents/draft_cycle.py`, which executes the full Drafter→Critic→redraft loop server-side — the orchestrator is not involved in individual rounds)
- **Approval Card** — every 30 min → `POST /run/approval-card`
- **Reschedule** — daily 01:00 ET → `POST /run/approval-card-reschedule`

Slack button actions (approve, reject, edit-caption, regen-media, regen-all) post directly from Slack to the executor's `/slack/interactivity` endpoint — they are handled by `tools/approval_router.py` and never go through n8n.

---

## How to Operate

### 1. Look for existing tools first

Before building anything new, check `tools/` based on what your workflow requires. Only create new scripts when nothing exists for that task.

### 2. Config before code

Before writing any agent or tool, load the business config and confirm you have the IDs, endpoints, and values you need. If something is missing from config, flag it — don't hardcode a workaround.

### 3. Learn and adapt when things fail

When you hit an error:

- Read the full error message and trace
- Fix the script and retest (if it uses paid API calls or credits, check with me before running again)
- Document what you learned in the workflow (rate limits, timing quirks, unexpected behavior)

Example: You get rate-limited on the Creatomate API, so you dig into the docs, discover a polling endpoint for render status, refactor the tool to use it, verify it works, then update the workflow so this never happens again.

### 4. Keep workflows current

Workflows should evolve as you learn. When you find better methods, discover constraints, or encounter recurring issues, update the workflow. That said, don't create or overwrite workflows without asking unless I explicitly tell you to. These are your instructions and need to be preserved and refined, not tossed after one use.

---

## The Self-Improvement Loop

Every failure is a chance to make the system stronger:

1. Identify what broke
2. Fix the tool
3. Verify the fix works
4. Update the workflow with the new approach
5. Move on with a more robust system

This loop is how the framework improves over time.

---

## File Structure

### What goes where

- **Agent outputs:** Content Queue rows, Performance Log entries, generated images → Google Sheets and Drive
- **Code:** Agent runners, tools, tests, config → local repo, committed to GitHub
- **Intermediates:** Temporary processing files → `.tmp/`, regenerated as needed

### Directory layout

```
agents/             # Agent runner scripts (strategist.py, drafter.py, critic.py, etc.)
tools/              # Shared utilities (config_loader, drive_helpers, sheets_helpers, etc.)
tests/              # Test scripts — per agent and per tool
skills/             # Local cache of skill files (canonical copies live in Drive)
.tmp/               # Temporary files. Regenerated as needed. Gitignored.

business_config.yaml  # Instance config — all business-specific values (gitignored if contains secrets)
.env                  # API keys and environment variables (NEVER commit)
credentials_*.json    # Google OAuth credentials (gitignored)
token_*.json          # Google OAuth tokens (gitignored)
```

### Key files in config

The business config references these external resources (all in Google Drive):

- **Workflows (SOPs):** `workflows.strategist`, `workflows.drafter`, `workflows.critic`, etc.
- **Skills:** `skills.brand_voice`, `skills.hook_creation`, `skills.content_types`, etc.
- **Data sheets:** `drive.content_queue_sheet_id`, `drive.performance_log_sheet_id`, `catalog.spec_sheet_id`
- **Templates:** `creatomate.equipment_post_image.templates.*`, `creatomate.review_video.templates.*`, etc.

---

## Version Control

This project is a git repository. Follow these rules:

**Commit at logical checkpoints.** After completing a major build step, a working feature, or a passing test suite, commit. Use descriptive commit messages that reference the session and step (e.g., `Session 2A: config_loader and skill_loader complete, tests passing`).

**Never commit secrets.** The `.gitignore` file is the first line of defense. Before the first commit of any session, verify that `.gitignore` includes all credential and secret files. If you create a new credential file or secret-bearing config that isn't already covered, add it to `.gitignore` before doing anything else.

**Branch strategy.** Work on `main` unless I tell you otherwise. If I ask you to branch, name branches by session (e.g., `session-2a-shared-tools`).

---

## Credential and Secret Protection

This is non-negotiable. Every rule here applies at all times, no exceptions.

- `.env` is the single source for API keys and environment variables. Never hardcode secrets in scripts, configs, or comments.
- Never stage or commit `.env`, `credentials_*.json`, `token_*.json`, service account key files, or any file containing API keys, passwords, or tokens.
- `.gitignore` must include at minimum: `.env`, `credentials*.json`, `token*.json`, `*-service-account*.json`, `.tmp/`, and `__pycache__/`.
- Verify before every commit. Run `git status` and review staged files. If any secret-bearing file appears in the staged list, unstage it and fix `.gitignore` before proceeding.
- If a secret is accidentally committed, tell me immediately. Do not try to fix it silently with a follow-up commit — the secret is already in git history.

### API keys this project uses

All stored in `.env`, never elsewhere:

- `ANTHROPIC_API_KEY` — Drafter LLM
- `OPENAI_API_KEY` — Critic LLM + image generation (gpt-image-2)
- `CREATOMATE_API_KEY` — Template rendering (text overlay + video)
- `SOCIALBU_API_KEY` — Publishing to FB/IG/GBP
- `SLACK_BOT_TOKEN` — Approval cards, error notifications, health reports
- Google OAuth credentials — Drive, Sheets, GBP Performance API, GBP Reviews API

---

## Agent Inventory

| Agent | Script | SOP | LLM Provider | Purpose |
|-------|--------|-----|--------------|---------|
| Asset Indexer | `agents/asset_indexer.py` | `workflows.asset_indexer` | — | Sync equipment catalog with Drive images |
| Strategist | `agents/strategist.py` | `workflows.strategist` | — | Plan 7-day content calendar |
| Drafter | `agents/drafter.py` | `workflows.drafter` | Anthropic | Write captions, generate media |
| Critic | `agents/critic.py` | `workflows.critic` | OpenAI | QA check drafts against checklist |
| Learning | `agents/learning.py` | `workflows.learning` | — | Weekly performance analysis |
| Systems Health | `agents/systems_health.py` | `workflows.systems_health` | — | Pipeline monitoring + weekly report |

---

## Bottom Line

You sit between what I want (workflows) and what actually gets done (tools). Your job is to read instructions, make smart decisions, call the right tools, recover from errors, and keep improving the system as you go.

Config is the source of truth. Skills load from Drive. Agents work standalone before they work in a pipeline. Every business reference comes from config — never from memory.

Stay pragmatic. Stay reliable. Keep learning.
