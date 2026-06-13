# Asset Indexer Agent — Workflow SOP

*V1 — keeps the catalog sheet in sync with the business's source-of-truth asset folders*

## Role

The Asset Indexer is the system's inventory manager. It reads the business's source asset folders in Google Drive, discovers new or changed items, and writes structured catalog records to the Catalog Google Sheet. Other agents never read the raw Drive folders — they read the Catalog sheet the Asset Indexer maintains.

## Trigger

| Trigger | Source | Frequency |
|---------|--------|-----------|
| Scheduled | n8n cron | Daily 02:00 ET (managed via `tools/n8n_deploy.py`) |
| On-demand | Drive change webhook → executor `/run/indexer` | When files are added/modified in the asset folders |

## Inputs

| Input | Source | Purpose |
|-------|--------|---------|
| Equipment/product image folders | Google Drive (folder ID from `business_config.yaml` → `catalog.image_folder_id`) | Source of truth for what items exist and what images are available |
| Image metadata sheet | Google Sheets (optional, sheet ID from `business_config.yaml` → `catalog.image_metadata_sheet_id`) | If the owner maintains a separate metadata sheet with specs, descriptions, or tags per item |
| Current Catalog sheet | Google Sheets (sheet ID from `business_config.yaml` → `catalog.spec_sheet_id`) | Current state of the index — used to detect new vs. existing items and avoid overwriting owner edits |
| Business config | `business_config.yaml` | Folder IDs, spec field definitions, catalog settings |

## Outputs

| Output | Destination | Description |
|--------|-------------|-------------|
| Updated Catalog sheet | Google Sheets | New rows for discovered items, updated `image_count` and `primary_image_id` for existing items |
| Slack notification (new items) | Slack `#approvals` channel | "Asset Indexer found 3 new items: [names]. Review the catalog sheet to confirm categories and fill in missing specs." |
| Slack notification (asset gaps) | Slack `#system-errors` channel | "Asset Indexer found items with missing or unusable assets: [names]. Action needed." |

## Processing Steps

### 1. Read Current Catalog State

Load all rows from the Catalog sheet. Build a lookup by `item_id` so the agent can distinguish new items from existing ones.

### 2. Scan Source Folders

Read the contents of the asset folder(s) from Google Drive. For each subfolder or file grouping that represents a catalog item:

- Identify the item (by folder name, file naming convention, or metadata sheet mapping — this is business-specific and defined in the onboarding process)
- Count available images
- Select a primary image (highest resolution, best composition — or the one flagged as primary in metadata if available)
- Extract any available metadata (file names, descriptions, EXIF data)

### 3. Match Against Existing Catalog

For each discovered item, check if it already exists in the Catalog sheet by `item_id`:

**If new (no matching `item_id`):**
- Create a new row with core fields populated:
  - `item_id`: Generate using the business prefix + next incrementing number
  - `item_name`: Inferred from folder name or metadata
  - `category`: Inferred from folder structure or flagged as "uncategorized" for owner review
  - `status`: Default to "active"
  - `description`: Leave blank (owner or operator fills in)
  - `primary_image_id`: Drive file ID of the selected primary image
  - `image_count`: Count of available images
  - `last_posted`: Blank
  - `post_count`: 0
  - `tags`: Blank
  - `notes`: "Auto-indexed [date]. Owner review recommended."
- Populate spec fields from metadata sheet if available. Leave blank if data is unavailable — do not invent values.

**If existing (matching `item_id`):**
- Update `image_count` if it has changed
- Update `primary_image_id` only if the current primary is missing or a clearly better option exists
- Do NOT overwrite: `item_name`, `category`, `description`, `tags`, `status`, `notes`, or any spec fields the owner has manually edited
- If the item's folder is empty or all images are unusable, set `status` to "inactive" and flag in the asset gaps notification

### 4. Detect Asset Gaps

Flag items where:
- Zero usable images exist
- All images are below minimum quality thresholds (too small, corrupted, wrong format)
- The item exists in the catalog but its source folder has been deleted

### 5. Write Updates

Write all changes to the Catalog sheet in a single batch update (not row-by-row) to minimize API calls.

### 6. Send Notifications

**If new items were discovered:**
Post to `{{SLACK_APPROVALS_CHANNEL}}`: "Asset Indexer found [N] new items: [list of item_names]. Review the catalog sheet to confirm categories, descriptions, and specs."

**If asset gaps were detected:**
Post to `{{SLACK_ERROR_CHANNEL}}`: "Asset Indexer flagged [N] items with missing or unusable assets: [list]. These items will not be eligible for content planning until resolved."

**If no changes:**
No notification. Silent success.

## Autonomous Decisions

The Asset Indexer makes these decisions without human input:

- Which image to select as primary (when no metadata flag exists)
- How to categorize new items based on folder structure (can be overridden by owner)
- Whether an image is usable (resolution, format, corruption checks)
- When to flag an item as having asset gaps

## Human-in-Loop

The Asset Indexer does not require approval for its updates. However:

- New items are flagged in Slack for owner review (category, description, specs)
- Asset gaps are flagged in Slack for owner action
- The owner can manually edit any catalog field and the Asset Indexer will not overwrite those edits on subsequent runs

## Error Handling

| Error | Behavior |
|-------|----------|
| Drive folder not found | Log error, post to `{{SLACK_ERROR_CHANNEL}}`, abort run |
| Catalog sheet not accessible | Log error, post to `{{SLACK_ERROR_CHANNEL}}`, abort run |
| Single item fails to process | Log warning, skip item, continue processing remaining items, include in summary notification |
| Drive API rate limit | Exponential backoff, retry up to 3 times, then abort with notification |

## Failure Mode

If the Asset Indexer fails completely, the rest of the system continues to operate using the existing Catalog sheet data. The Strategist can still plan content from whatever is already indexed. The system degrades gracefully — stale catalog, not broken pipeline.

## Output Schema

The Asset Indexer returns structured JSON to the orchestrator (n8n):

```json
{
  "run_timestamp": "2026-05-20T06:00:00Z",
  "items_scanned": 47,
  "new_items_added": 3,
  "items_updated": 12,
  "asset_gaps_flagged": 2,
  "errors": [],
  "new_item_ids": ["EQ-048", "EQ-049", "EQ-050"],
  "gap_item_ids": ["EQ-011", "EQ-023"]
}
```

## Config Dependencies

| Config Path | Purpose |
|-------------|---------|
| `catalog.image_folder_id` | Root folder to scan |
| `catalog.image_metadata_sheet_id` | Optional metadata sheet |
| `catalog.spec_sheet_id` | Catalog sheet to write to |
| `catalog.core_fields` | Expected column structure |
| `catalog.spec_fields` | Business-defined spec columns |
| `approval.slack_channel` | Where to post new item notifications |
| `approval.error_channel` | Where to post asset gap alerts |

---

*Asset Indexer Agent — Workflow SOP v1*
*Last updated: 2026-05-20*
