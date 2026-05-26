# Platform Style Skill — Portable

*V1 — factual platform documentation for Facebook, Instagram, and Google Business Profile*

## Purpose

Document the technical constraints, content behaviors, and publishing mechanics of each social platform the system publishes to. This is a portable, factual reference. It contains no brand, industry, or business assumptions. Agents use this to ensure content fits each platform's rules before publishing.

## How This File Is Used

- **Strategist:** References platform fit when assigning content types and platforms.
- **Drafter:** Checks character limits, link behavior, and media specs before finalizing a draft.
- **Critic:** Verifies the draft does not violate any platform constraint (length, link placement, unsupported features).
- **Image pipeline:** References aspect ratios and dimension targets when generating or cropping media.

---

## Facebook (Pages)

### Character Limits

| Element | Target Length | Truncation Behavior |
|---------|-------|-------------------|
| Post caption | 500-1500 characters | Truncated at ~477 characters in feed with "See more" link. First 1-2 lines must carry the hook. |
| First comment | 200 characters | Not truncated in most views. |
| Link preview title | ~88 characters | Truncated with ellipsis. |
| Link preview description | ~300 characters | Truncated. |

### Media Specifications

| Format | Recommended Size | Aspect Ratio | Max File Size |
|--------|-----------------|--------------|---------------|
| Photo (feed) | 1200 x 630 px (landscape) or 1080 x 1350 px (portrait) | 1.91:1 (landscape) or 4:5 (portrait) | 30 MB |
| Video (feed) | 1280 x 720 px minimum | 16:9 (landscape) or 4:5 / 9:16 (portrait) | 10 GB / 240 min |
| Carousel | 1080 x 1080 px per card | 1:1 | 30 MB per image |

Recommended default for social posts: **4:5 vertical (1080 x 1350 px)** for maximum feed real estate.

### Media Requirements

- Facebook allows text-only posts (no image or video required).
- Photo and video posts perform significantly better in the algorithm than text-only posts.
- SocialBu will accept a Facebook scheduling call with or without a media attachment.

### Link Behavior

- A URL in the caption body triggers Facebook's link preview card, which overrides any attached image with the URL's OG image.
- To preserve a branded image, do not place the URL in the caption. Place the URL in the first comment instead.
- First comment links do not trigger a link preview.
- Facebook does not natively support scheduling first comments. SocialBu supports the `first_comment` field in the `POST /posts` payload (validated in Dependency Tests).

### Hashtag Behavior

- Hashtags are clickable but have minimal algorithmic impact on Facebook Pages.
- Policy is set per business in the brand voice file. This skill does not prescribe a hashtag policy.

### Post Types

Facebook Pages support: text, photo, video, link (with preview), carousel, event, and live video. The agentic system primarily uses photo posts and link posts (via first comment).

---

## Instagram (Business/Creator)

### Character Limits

| Element | Target Length | Truncation Behavior |
|---------|-------|-------------------|
| Caption | 800-1500 characters | Truncated at ~125 characters in feed with "...more" link. First line must carry the hook. |
| First comment | 300 characters | Not truncated. |
| Bio link | 1 URL in bio (or link-in-bio tool) | N/A |

### Media Specifications

| Format | Recommended Size | Aspect Ratio | Max File Size |
|--------|-----------------|--------------|---------------|
| Photo (feed) | 1080 x 1350 px (portrait) | 4:5 | 30 MB |
| Photo (square) | 1080 x 1080 px | 1:1 | 30 MB |
| Reel / Video | 1080 x 1920 px | 9:16 | 650 MB / 90 sec (Reels), 60 min (feed video) |
| Carousel | 1080 x 1350 px per card | 4:5 (all cards must match first card ratio) | 30 MB per image |
| Story | 1080 x 1920 px | 9:16 | 30 MB (image), 250 MB (video / 60 sec) |

Recommended default for social posts: **4:5 vertical (1080 x 1350 px)**.

### Media Requirements

**Instagram feed posts require an image or video. There is no text-only post format.**

This is enforced at the API level: SocialBu will reject the `POST /posts` scheduling call for an Instagram account if no media attachment is included in the payload. The call will fail, and the post will not be scheduled.

**Workflow implications:**

- The image pipeline must produce a finalized image or video asset before the Instagram post can be sent to SocialBu.
- The Drafter cannot schedule an Instagram post as "caption ready, image pending." The image must exist at scheduling time.
- If the image pipeline has not completed, the Instagram post must be held in the Content Queue at `status=drafted` until the asset is ready. It cannot advance to `status=awaiting_approval` or be published.
- The Critic should flag any Instagram draft that reaches review without a confirmed media asset as a hard failure.
- Facebook and GBP posts for the same content can technically be scheduled ahead of the Instagram version if the image is not yet ready, but for consistency the system should treat image-ready as a prerequisite for all three platforms.

### Link Behavior

- Instagram does not support clickable links in feed post captions.
- Links in captions render as plain text (not tappable).
- For link posts: place the URL in the first comment. The caption CTA directs readers to the first comment.
- SocialBu supports the `first_comment` field for Instagram posts (validated in Dependency Tests).
- Instagram does support link stickers in Stories.

### Hashtag Behavior

- Instagram supports up to 30 hashtags per post (recommended: 3-5 or none).
- Hashtags can go in the caption or first comment.
- Policy is set per business in the brand voice file.

### Post Types

Instagram supports: single image, carousel (up to 20 slides), Reels (short video), Stories (ephemeral), and feed video. The agentic system primarily uses single image posts and carousels.

---

## Google Business Profile (GBP)

### Character Limits

| Element | Target Length | Truncation Behavior |
|---------|-------|-------------------|
| Post body | 150-200 characters | Truncated at ~150-200 characters in the GBP panel with "Read more" link. |
| Post title (Offer/Event only) | 58 characters | Hard limit. |
| Event start/end date | Required for Event posts | Date picker format. |
| Offer terms | 150-200 characters | Truncated similarly to post body. |

### Media Specifications

| Format | Recommended Size | Aspect Ratio | Max File Size |
|--------|-----------------|--------------|---------------|
| Photo | 1200 x 900 px | 4:3 (landscape) | 5 MB |
| Video | 720p minimum | 16:9 or 4:3 | 75 MB / 30 sec |

GBP strongly favors **4:3 landscape (1200 x 900 px)** images. Portrait images may be cropped unpredictably. This differs from the 4:5 portrait used on FB/IG.

### Media Requirements

- GBP posts can be published without an image, but posts with images receive significantly more visibility in the local panel.
- SocialBu will accept a GBP scheduling call with or without a media attachment.
- Image is recommended but not a hard requirement at the platform or API level.

### Link Behavior

- GBP posts support a CTA button with a URL. The URL is attached to the button, not embedded in the post body.
- URLs in the post body are clickable but not recommended as the primary click target. Use the CTA button.

### Post Types

| Post Type | Use Case | CTA Button | Additional Fields |
|-----------|----------|------------|-------------------|
| Update | General post (news, tips, content) | Optional | None |
| Offer | Promotional with deal details | Optional | Title (required), terms, coupon code, start/end date |
| Event | Time-bound event or occurrence | Optional | Title (required), start date (required), end date |

**Update** is the default post type for the agentic system. Offer and Event are used only when the Strategist assigns a promotional or event content type.

### CTA Button Types

GBP posts support one CTA button. The button type determines the label and behavior.

| Button Type | Label Displayed | URL Required | Notes |
|-------------|----------------|-------------|-------|
| CALL | Call now | No (uses GBP phone) | Triggers phone dialer. Uses the phone number on the GBP listing. |
| LEARN_MORE | Learn more | Yes | Opens the provided URL. General-purpose click-through. |
| BOOK | Book | Yes | Opens the provided URL. For booking/scheduling pages. |
| ORDER | Order online | Yes | Opens the provided URL. For e-commerce or order pages. |
| GET_DIRECTIONS | Get directions | No (uses GBP address) | Opens Google Maps directions to the business location. Requires `{{GOOGLE_MAPS_URL}}` in business config if using a custom URL. |
| SIGN_UP | Sign up | Yes | Opens the provided URL. For newsletter or registration pages. |

Only one button per post. The Strategist or Drafter selects the button type based on the post objective and CTA type assigned.

### Hashtag Behavior

- GBP does not use hashtags. They render as plain text and have no discovery function.
- Policy is set per business in the brand voice file.

### Publishing Notes (SocialBu)

- SocialBu publishes to GBP via the `google.location` account type.
- GBP posts created via SocialBu API use the Update post type by default.
- Offer and Event post types may require manual creation in the GBP dashboard if SocialBu does not expose those fields via API.
- The `publish_at` field is required (SocialBu does not support true drafts). Format: `Y-m-d H:i:s`.

---

## Cross-Platform Summary

| Constraint | Facebook | Instagram | GBP |
|-----------|----------|-----------|-----|
| Caption Length Targets | 500-1500 chars | 800-1500 chars | 150-200 chars |
| Feed truncation | ~477 chars | ~125 chars | ~150-200 chars |
| Recommended image size | 1080 x 1350 (4:5) | 1080 x 1350 (4:5) | 1200 x 900 (4:3) |
| Image required to schedule | No | **Yes — SocialBu rejects the API call without media** | No |
| Link in caption | Triggers preview card | Not clickable | Clickable but not primary |
| First comment link | Supported (via SocialBu) | Supported (via SocialBu) | Not applicable |
| CTA button | Not native | Not native | Supported (one per post) |
| Hashtags | Clickable, low impact | Up to 30, moderate impact | No function |
| Video max length | 240 min | 90 sec (Reels) / 60 min (feed) | 30 sec |
| Publishing tool | SocialBu | SocialBu | SocialBu |
| True draft support | No (scheduled only) | No (scheduled only) | No (scheduled only) |

---

## Image Pipeline Implications

When the system generates one image asset shared across platforms, it must account for different aspect ratios:

- **Primary asset:** 4:5 vertical (1080 x 1350 px) for FB and IG feed posts. This is the default output from the image prompt pipeline.
- **GBP crop:** 4:3 landscape (1200 x 900 px). The image pipeline should produce a secondary crop from the primary asset, or the system should generate a separate GBP-optimized image.
- **Safe zone:** When composing text overlays on the primary 4:5 image, keep critical text within the center region that survives a 4:3 crop. This ensures the hook text remains readable if the image is reused for GBP.
- **Carousel consistency:** All carousel cards must share the same aspect ratio (enforced by Instagram). Use 4:5 for all cards.
- **Instagram scheduling dependency:** Because SocialBu will reject an Instagram scheduling call without a media attachment, the image pipeline is a hard blocker for Instagram posts. The Make.com scenario that triggers scheduling must confirm image availability before calling the SocialBu API for Instagram. Facebook and GBP can technically proceed without images, but the system should treat image-ready as a prerequisite for all platforms for consistency.

---

## SocialBu Publishing Reference

All platforms are published through SocialBu. Key API behaviors (from Dependency Tests):

- **Base URL:** `https://socialbu.com/api/v1`
- **Authentication:** `Authorization: Bearer {key}` header. All requests require `Accept: application/json`.
- **`publish_at` format:** `Y-m-d H:i:s` (not ISO 8601). Required on all posts.
- **True drafts:** Not supported via API. All posts must have a `publish_at` datetime.
- **First comment:** Supported via the `first_comment` field in the `POST /posts` payload for both Facebook and Instagram.
- **Instagram media requirement:** SocialBu will reject Instagram post creation if no image or video is attached. This is a hard API-level failure, not a silent degradation.
- **Response shape:** `{"success": true, "posts": [...]}`
- **Safety check:** Verify `published: false` in the response to confirm the post was scheduled, not published immediately.

### SocialBu Account IDs

Account IDs are stored in `business_config.yaml` at `platforms.accounts`. The Drafter and publishing pipeline reference these when constructing API payloads. Example structure:

```
platforms:
  accounts:
    facebook:
      account_id: "{{FB_ACCOUNT_ID}}"
    instagram:
      account_id: "{{IG_ACCOUNT_ID}}"
    gbp:
      account_id: "{{GBP_ACCOUNT_ID}}"
```

---

*Platform Style Skill — Portable v1.2*
*Last updated: 2026-05-20*
