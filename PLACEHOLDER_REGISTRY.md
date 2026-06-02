# Placeholder & Config Registry
# ============================================================================
# READ THIS BEFORE BUILDING OR MODIFYING ANY AGENT, TOOL, OR SKILL.
#
# This document is the single source of truth for:
#   1. All {{PLACEHOLDER}} tokens used in skill files and SOPs
#   2. All business_config.yaml paths and what they resolve to
#   3. All Google Drive file IDs and their purposes
#   4. All naming conventions for sheets, folders, and templates
#
# If a value is not in this document, it does not exist. Do not assume or
# invent IDs, paths, or placeholder names.
# ============================================================================


# ============================================================================
# SECTION 1: SKILL FILE PLACEHOLDERS ({{TOKEN}} format)
# ============================================================================
# Skill files are loaded from Google Drive by skill_loader.py.
# Before returning skill text to an agent, skill_loader replaces these
# tokens with values from business_config.yaml.
#
# IMPORTANT: Tokens use SCREAMING_SNAKE_CASE wrapped in double curly braces.
# The mapping from token to config path is defined in skill_loader.py.
# ============================================================================

# --- Business Identity ---
# {{BUSINESS_NAME}}        → business.name              → "T.E.S. Rentals"
# {{BUSINESS_SHORT_NAME}}  → business.short_name         → "TES"
# {{BUSINESS_DESCRIPTION}} → business.description        → "local equipment rental company in North Florida"
# {{INDUSTRY}}             → business.industry           → "equipment rental"
# {{SERVICE_AREA}}         → business.service_area        → "North Florida — Bradford, Union, Clay, Baker, Alachua, Columbia, Duval, Putnam counties"
# {{SERVICE_AREA_SHORT}}   → business.service_area_short  → "North Florida"
# {{TARGET_CUSTOMER}}      → business.target_customer     → "contractors, homeowners, and property managers..."
# {{DIFFERENTIATOR}}       → business.differentiator      → "correct machine fit, maintained equipment..."

# --- Owner ---
# {{OWNER_NAME}}           → owner.name                  → "Zeb"
# {{OWNER_ROLE}}           → owner.role                  → "owner and operator"
# {{OWNER_EXPERIENCE}}     → owner.experience_summary    → "15+ years in equipment rental across North Florida"

# --- Contact ---
# {{PHONE}}                → contact.phone               → "(904) 452-0888"
# {{WEBSITE}}              → contact.website             → "https://tesrents.com"
# {{BOOKING_URL}}          → contact.booking_url         → "https://tesrents.com"
# {{EMAIL}}                → contact.email               → "admin@tesrents.com"
# {{GOOGLE_MAPS_URL}}      → contact.google_maps_url     → "https://www.google.com/maps/place/?q=place_id:ChIJyZ4d37L95YgR9yi3YNwP2nQ"

# --- Strategy ---
# {{POSTS_PER_WEEK_PER_PLATFORM}} → strategy.posts_per_week_per_platform → 7
# {{LEAD_TIME_HOURS}}      → strategy.lead_time_hours    → 36
# {{MIN_GAP_HOURS}}        → strategy.min_gap_hours      → 4
# {{PRICING_POLICY}}       → strategy.pricing_in_posts   → "never"

# --- Approval / Slack ---
# {{MAX_QUEUE_DEPTH}}      → approval.max_queue_depth    → 7
# {{SLACK_APPROVALS_CHANNEL}} → approval.slack_channel   → "#approvals"
# {{SLACK_ERROR_CHANNEL}}  → approval.error_channel      → "#system-errors"
# {{SLACK_HEALTH_CHANNEL}} → health.slack_channel        → "#system-health"

# --- Brand Visuals ---
# {{BRAND_FEEL}}           → brand_visuals.feel          → "Practical, rugged, reliable..."
# {{BRAND_VISUAL_FEEL}}    → brand_visuals.feel          → "Practical, rugged, reliable..."  (alias of {{BRAND_FEEL}})
# {{VISUAL_CONTEXT}}       → brand_visuals.visual_context → "real jobsite photos with equipment..."
# {{TYPOGRAPHY_STYLE}}     → brand_visuals.typography_style → "Bold, rugged typography..."

# --- Catalog ---
# {{PRIMARY_SUBJECT}}      → catalog.primary_subject     → "equipment"
# {{CATALOG_PRIMARY_SUBJECT}} → catalog.primary_subject  → "equipment"  (alias of {{PRIMARY_SUBJECT}})

# --- Platform Account IDs ---
# {{FB_ACCOUNT_ID}}        → platforms.accounts.facebook.account_id   → "173903"
# {{IG_ACCOUNT_ID}}        → platforms.accounts.instagram.account_id  → "173904"
# {{GBP_ACCOUNT_ID}}       → platforms.accounts.gbp.account_id         → "173906"

# --- Learning Agent ---
# {{LEARNING_AGENT_DAY}}         → metrics.learning_agent_day          → "monday"
# {{MAJOR_SHIFT_THRESHOLD}}      → metrics.major_shift_threshold       → 20
# {{MIN_DATA_POINTS_FOR_PATTERN}} → metrics.min_data_points_for_pattern → 5

# --- Systems Health Agent ---
# {{SYSTEMS_HEALTH_DAY}}   → health.report_day            → "monday"
# {{MONTHLY_BUDGET_LIMIT}} → health.monthly_budget_limit  → null  (owner sets once baseline established)

# --- Runtime tokens (NOT in PLACEHOLDER_MAP — filled by agent at execution time) ---
# These pass through skill_loader unchanged and are substituted by the calling agent
# from Content Queue row data or as literal documentation examples.
# {{TOPIC}}        — gbp_post: per-post topic supplied by Drafter
# {{PLATFORM}}     — image_prompt_social: target platform, injected by Drafter from Content Queue row
# {{PLACEHOLDER}}  — image_prompt_social: literal documentation example, NOT a real token


# ============================================================================
# SECTION 2: BUSINESS CONFIG PATHS → VALUES
# ============================================================================
# These are the dot-notation paths used in config_loader.py.
# Agents and tools access values via: config.get("dotted.path")
# ============================================================================

# --- Google Drive File IDs ---
# drive.root_folder_id                → 1hTsUK9-ufyQSRSMrDCp9_guEUV2iatQQ
# drive.content_queue_sheet_id        → 1nrqzf9Y8_nOdx7S0lP9CILV_tDz9RAHqbJqkQx9Bgeo
# drive.performance_log_sheet_id      → 1t5OfF7_EvXYNtWR9L1vcRMm-Oyo1DfzDpminTZt4mEs
# drive.strategy_guidance_file_id     → 1rEDcIkcp1ZZsJsT2_SqJQ_VOwdY6AwxE
# drive.local_calendar_sheet_id       → "" (excluded from v1)
# drive.brand_voice_file_id           → 1HKBeCVpqTdh_J4MNepG5q39CmjBRcY0MD4D_kHggpAQ
# drive.generated_images_folder_id    → 1HMg1wlo6g3a4WEt0mSywl2f2btqWX897

# --- Catalog ---
# catalog.spec_sheet_id               → 15-q8d_D6XZrKAOaz3CBqUjurSMoZW6cBAcL3XmKduYk
# catalog.image_folder_id             → 11tRhJWZaxijJsrsOlr1ukvW1jYaGYRLJOu6wkYVwi12jIZ1sjzwr9cbz4aA9yz0pb7h4sHtR
# catalog.image_metadata_sheet_id     → 1mxuYmfs5bmp4wWECvp6zImns2U3EqaFLPagmZKiHCsM

# --- Skill File IDs (REFERENCE ONLY — canonical copies now live locally) ---
# Skills and workflows are loaded from local .md files in skills/ and workflows/
# by skill_loader.py. The Drive IDs below are retained for traceability to the
# original documents but are no longer read by the loader.
# skills.hook_creation                → 1eoqYP036gKFU3ZktZzg3wpISZfwI12eD
# skills.gbp_post                     → 1836-0OdS-CxrQXqygCcoJM9-qoebJmx4
# skills.cta                          → 1bgLqZhb4RPcPI07-EEchGJ78k25kR7vZ
# skills.content_types                → 1bC2Gs28A4GKCQPAGqmQlkDlKij2iR5T3
# skills.platform_style               → 109OhTQDBpimJf3EvX36ZiaBPfdxM6l1x
# skills.critic_checklist             → 1rMnX99R75yJJPZe4HoXcKaCv4zH-3jnl
# skills.image_prompt_universal       → 1IV61Q6EjjjxK1zDuTEMP--pRWnmcw5XO
# skills.image_prompt_social          → 1_9HzTGdWOIN3qAhaQmg_3PORCB0T-YPA
# skills.brand_voice                  → 1FrDMG_dqT2iXANnoUe_tiuY6ltUUS9xuG0PkmBbRh1w
# skills.strategy_guidance            → 1rEDcIkcp1ZZsJsT2_SqJQ_VOwdY6AwxE
# skills.few_shot_library             → 16vGjPbd9fWz4GjDFBpr4o5TH-wdzYT_GOhvwXeqrvQg

# --- Workflow SOP File IDs (loaded by skill_loader.py) ---
# workflows.asset_indexer             → 12fWfs2VbO2hoA0KpgeErr39sruNX_5GY
# workflows.strategist                → 19WMHT5JyRKlc4XPScJ4IolI4QvsfoIp1
# workflows.drafter                   → 1pkPwas5VGjyDOA7dpN9t-cs0-5LwtIIH
# workflows.critic                    → 1HnGHIwXrOyChHFzJmVW3Rd-qOLL3oFGH
# workflows.learning                  → 1T4B2K9wxGAGgXE-8pA7Qe3wYwUcSgN9i
# workflows.systems_health            → 1Yj39msVaHk3tai1fm_dJu_nAmNO4AeT_

# --- Creatomate Template IDs ---
# creatomate.equipment_post_image.templates.diagonal_slash.id      → 5f7e02d4-5e52-4d4a-96f1-41a8f55c19ac
# creatomate.equipment_post_image.templates.bottom_bar_takeover.id → 5aedba31-bbb4-46c5-b196-77b3a302daba
# creatomate.equipment_post_image.templates.corner_punch.id        → 8d03dab0-f72d-4474-825c-633f205c4733
# creatomate.equipment_post_image.templates.split_frame.id         → 7db3efe5-8b14-43d2-b4b9-86f51b83f6d4
# creatomate.equipment_post_image.templates.stencil_stamp.id       → 96607f36-08e9-4ed9-b334-4c5920c3abf0
# creatomate.equipment_post_video.templates.slow_push.id           → 627ca53b-bb63-473a-b9ee-ef84687b38e2
# creatomate.equipment_post_video.templates.pan_right.id           → d93cc698-b2e2-4d33-a9b2-aeb0d4f2e30d
# creatomate.equipment_post_video.templates.zoom_out_reveal.id     → 6c397952-b4ac-432f-99e7-e46a3571c142
# creatomate.equipment_post_video.templates.diagonal_sweep.id      → 31860113-3710-44e4-bd16-62f4b7ac13ba
# creatomate.equipment_post_video.templates.cinematic_letterbox.id → dba2aec0-10a9-4f6f-8027-95d55c2852a9
# creatomate.review_image.templates.bold_quote_card.id             → 214dbe64-d3c2-4206-a703-fbd5370effc8
# creatomate.review_image.templates.photo_testimonial.id           → 58a402c4-2a9e-4866-9d80-8fee6f1c2e30
# creatomate.review_image.templates.split_review.id                → 2fc1d8eb-6c5e-4bfd-94f3-b73c5486d642
# creatomate.review_image.templates.star_burst.id                  → 921075d1-a61b-448e-8ddb-d9d50b6bf229
# creatomate.review_image.templates.stamp_card.id                  → a07cdafe-b646-4336-aef9-5538e3e3d545
# creatomate.review_video.templates.star_cascade.id                → 8ba71360-06ff-4fbb-99f7-2df4e4763d4c
# creatomate.review_video.templates.photo_reveal.id                → 6923474b-06ed-4f06-85ae-f1840dea4a8d
# creatomate.review_video.templates.split_slide.id                 → 3e7498d0-2adf-42ae-89e2-1e43abb0b960
# creatomate.review_video.templates.pulse_star.id                  → 887dc134-01db-4eab-a24c-746585de15c8
# creatomate.review_video.templates.stamp_slam.id                  → 95eb14a8-5418-4565-8558-0372813ee35a

# --- Creatomate Asset UUIDs (in-platform media library, NOT Drive IDs) ---
# creatomate.assets.logo_uuid                        → 388939ec-c087-4cbc-9448-978ba434abdd
# creatomate.assets.placeholder_equipment_photo_uuid → bc53a14d-ed71-4a7b-9741-46769cb3fd88

# --- Creatomate Dynamic Field Names (element names in templates) ---
# Equipment post templates use:
#   "Equipment-Photo"  → source image URL (modifications key)
#   "Hook-Text"        → hook text overlay (modifications key)
#
# Review templates use:
#   "Review-Text"      → review excerpt (modifications key)
#   "Reviewer-Name"    → reviewer first name (modifications key)
#   "Star-Rating"      → "★★★★★" string (modifications key, image templates only)
#
# Templates with extra_dynamic_fields: ["Equipment-Photo"]:
#   equipment_post_image.bottom_bar_takeover (T2)
#   equipment_post_video.pan_right (T7)
#   review_image.photo_testimonial (T12)
#   review_video.photo_reveal (T17)

# --- Platform Account IDs (SocialBu) ---
# platforms.accounts.facebook.account_id    → "173903"
# platforms.accounts.instagram.account_id   → "173904"
# platforms.accounts.gbp.account_id         → "173906"

# --- GBP API Identifiers ---
# platforms.accounts.gbp.gbp_account_id     → "accounts/108109713594635511816"
# platforms.accounts.gbp.gbp_location_id    → "locations/7215686684266711326"

# --- Brand Colors (for reference, not injected as placeholders) ---
# Primary orange:   #E8601C
# Dark base:        #1A1A1A
# Black:            #000000
# White text:       #FFFFFF
# Logo backing:     rgba(255,255,255,0.85)
# Dark overlay:     rgba(26,26,26,0.3) to rgba(26,26,26,0.95)
# Orange overlay:   rgba(232,96,28,0.92)


# ============================================================================
# SECTION 3: GOOGLE SHEETS — COLUMN STRUCTURES
# ============================================================================
# These are the expected column headers for each Google Sheet.
# Tools that read/write sheets must use these exact column names.
# ============================================================================

# --- Equipment Catalog (catalog.spec_sheet_id) ---
# Sheet ID: 15-q8d_D6XZrKAOaz3CBqUjurSMoZW6cBAcL3XmKduYk
# Updated: Session 10 — switched to populated sheet (40 items, uploaded from Session 6 XLSX)
#
# Core fields (in order):
#   item_id, item_name, category, model, status, description,
#   primary_image_id, image_count, last_posted, post_count, tags, notes
#
# Spec fields (in order, after core):
#   weight, dig_depth, reach, capacity, tail_swing, horsepower,
#   transport_width, rental_rate_note, availability, common_jobs, best_for
#
# Total: 22 columns. 40 data rows.

# --- Content Queue (drive.content_queue_sheet_id) ---
# Updated: Session 16 — Critic write-back schema consolidated to two
#   columns (critic_score + critic_notes). Live sheet migrated to match.
#
# Strategist-written fields (set when row is created):
#   row_id, status, platform, scheduled_datetime, objective,
#   content_type, focus_equipment_id, angle, cta_type, media_format,
#   text_overlay, source_image_id, draft_notes, review_id
#
# Drafter-written fields (set during drafting):
#   caption, creative_hook_text, first_comment, cta_text, hook_text,
#   image_overlay_text, media_url, media_format_used, draft_rationale,
#   revision_round
#
# Critic-written fields (set during QA):
#   critic_score, critic_notes
#
#   critic_score   — verdict string: pass | soft_fail | hard_fail
#   critic_notes   — JSON-serialized full Critic output:
#                    {revision_round, failed_checks, warnings,
#                     passed_checks, notes}
#
# Rationale for the 2-column shape: the Learning agent reads the
# Performance Log + Content Queue content metadata but does NOT consume
# Critic columns — those are an audit trail for the Slack approval card
# and human review only. Keeping the structured fields (failed_checks,
# warnings, passed_checks) inside critic_notes JSON avoids sheet bloat
# while preserving every detail. The Slack approval card unpacks the JSON
# at render time so the human sees the structured breakdown.
#
# Approval/publishing fields (set during approval and publishing):
#   approved_datetime, published_datetime, socialbu_post_id, rejection_reason
#
# Total: 30 columns.
#
# Column purpose notes:
#   creative_hook_text — distinct ≤7-word hook generated by the Drafter using
#     the hook creation skill. Used as the Hook-Text modification value when
#     rendering Creatomate equipment post templates (equipment_post_image,
#     equipment_post_video). NOT a truncation of the caption hook.

# --- Performance Log (drive.performance_log_sheet_id) ---
# Updated: Session 10 — reconciled with live sheet (21 columns)
#
# Identity fields:
#   post_id, queue_row_id, platform
#
# Content metadata (copied from Content Queue for analysis):
#   objective, content_type, media_format, cta_type, focus_equipment_id
#
# Timing fields:
#   posted_datetime, day_of_week, hour
#
# 24-hour snapshot metrics:
#   impressions_24h, reach_24h, engagement_24h, clicks_24h, cta_conversions_24h
#
# 7-day snapshot metrics:
#   impressions_7d, reach_7d, engagement_7d, clicks_7d, cta_conversions_7d
#
# Total: 21 columns.


# ============================================================================
# SECTION 4: NAMING CONVENTIONS
# ============================================================================

# --- File naming ---
# Agent scripts:         agents/<agent_name>.py        (e.g., agents/strategist.py)
# Tool scripts:          tools/<tool_name>.py           (e.g., tools/config_loader.py)
# Test scripts:          tests/test_<module_name>.py    (e.g., tests/test_config_loader.py)
# Skill cache (local):   skills/<skill_key>.md          (e.g., skills/hook_creation.md)

# --- Config access pattern ---
# Always use config_loader.get("dotted.path") — never parse YAML directly in agents.
# Example: config.get("catalog.spec_sheet_id") → "15-q8d_D6XZrKAOaz3CBqUjurSMoZW6cBAcL3XmKduYk"
# Example: config.get("creatomate.equipment_post_image.templates") → dict of all 5 templates

# --- Placeholder injection pattern ---
# skill_loader.load_skill("hook_creation") internally:
#   1. Resolves path: skills/hook_creation.md (relative to project root)
#   2. Reads file content from local filesystem
#   3. Replaces all {{PLACEHOLDER}} tokens using the mapping in this document
#   4. Returns the resolved text string

# --- Environment variable access ---
# Always use: os.environ.get("VARIABLE_NAME") or dotenv.load_dotenv() + os.getenv()
# Never hardcode API keys, tokens, or secrets anywhere.


# ============================================================================
# SECTION 5: SOP PLACEHOLDER TOKENS (used in workflow SOPs)
# ============================================================================
# These appear in workflow SOP files and are replaced by skill_loader.py
# using the same injection mechanism as skill placeholders.
# ============================================================================

# {{MAX_QUEUE_DEPTH}}              → approval.max_queue_depth           → 7
# {{POSTS_PER_WEEK_PER_PLATFORM}} → strategy.posts_per_week_per_platform → 7
# {{MIN_GAP_HOURS}}                → strategy.min_gap_hours             → 4
# {{LEAD_TIME_HOURS}}              → strategy.lead_time_hours           → 36
# {{SLACK_APPROVALS_CHANNEL}}      → approval.slack_channel             → "#approvals"
# {{SLACK_ERROR_CHANNEL}}          → approval.error_channel             → "#system-errors"
# {{SLACK_HEALTH_CHANNEL}}         → health.slack_channel               → "#system-health"
# {{LEARNING_AGENT_DAY}}           → metrics.learning_agent_day         → "monday"
# {{MAJOR_SHIFT_THRESHOLD}}        → metrics.major_shift_threshold      → 20
# {{MIN_DATA_POINTS_FOR_PATTERN}} → metrics.min_data_points_for_pattern → 5
# {{SYSTEMS_HEALTH_DAY}}           → health.report_day                  → "monday"
# {{MONTHLY_BUDGET_LIMIT}}         → health.monthly_budget_limit        → null


# ============================================================================
# END OF REGISTRY
# ============================================================================
