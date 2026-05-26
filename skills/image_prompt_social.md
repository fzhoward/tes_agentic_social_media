# Image Prompt — Social Posts (Portable)

*V1 — unified image generation prompt for all social post types. Appends after the universal preamble.*

## Purpose

Generate the image prompt sent to the image generation model (OpenAI Image 2 `/images/edits` endpoint) for every social media post across all platforms and content types. This file replaces the separate advisory post and link post image prompts with a single prompt that branches based on whether the Strategist assigned text overlay for the post.

This is a portable skill. It contains no brand, industry, or business assumptions. Business-specific values are injected via placeholder syntax (e.g. `BUSINESS_NAME`) from `business_config.yaml`.

## How This File Is Used

- **Drafter:** Constructs the full image prompt by concatenating the universal preamble + this file's output. Passes the assembled prompt and the source photo to the image generation API.
- **Critic:** Does not evaluate the image prompt directly. The Critic checks for image readiness as a warning (W1) but does not review prompt quality.

## What This Prompt Controls

- Photo enhancement direction (clarity, contrast, composition, lighting)
- Text overlay content and placement (when assigned)
- Typography style (when text overlay is present)
- Visual tone and mood appropriate to the post's content type and objective

## What This Prompt Does NOT Control

The following are handled downstream in the code pipeline, not in this prompt:

- **Output dimensions and aspect ratio.** The code pipeline passes the `size` parameter to the API and handles final resizing to target dimensions (4:5 for FB/IG, 4:3 for GBP).
- **Logo overlay.** The logo is composited onto the final image by the code pipeline after generation. The prompt must not instruct the model to add a logo.
- **GBP cropping.** The code pipeline produces the GBP variant from the primary asset. The prompt does not need to account for GBP safe zones — that is a code concern.
- **File format and compression.** The code pipeline converts and saves the final output.

---

## Prompt Assembly

The Drafter assembles the full prompt in this order:

```
[Universal Preamble — from image_prompt_universal_preamble_portable.md]

[Social Prompt — from this file, using either the TEXT OVERLAY or CLEAN PHOTO section]
```

The Drafter selects the correct section based on the `text_overlay` field in the Content Queue row, which the Strategist sets when planning the post.

---

## Section A: Text Overlay (when text_overlay = true)

Append this section after the universal preamble when the Strategist has assigned text overlay for the post.

```
PURPOSE: This image accompanies a social media post on {{PLATFORM}}. The hook text overlaid on the image is the primary scroll-stopping element. The text and the photo work together — the photo provides context and credibility, the text provides the hook.

TEXT OVERLAY:

1. Add this exact hook text: "[HOOK_TEXT]"

2. Bold text, strong readability on mobile at feed scale. The text must be legible at phone-screen size without zooming.

3. {{TYPOGRAPHY_STYLE}}

4. Place the text in negative space or a low-detail area of the image. Do not cover the main subject.

5. Keep the {{CATALOG_PRIMARY_SUBJECT}} visible and unobstructed by text.

6. Add a subtle dark overlay or gradient only where the text needs readability. The photo should remain visible behind it. Do not darken the entire image.

7. Use subtle shadow, stroke, or translucent backing behind text only if needed for contrast against a busy background.

8. The hook should feel like the primary message, but not like a cheap meme, stock ad graphic, or social media template.

9. Do not add any text beyond the hook text specified above. No taglines, no business name, no phone number, no URL, no hashtags.

OUTPUT: A finished image with the hook text overlaid cleanly and professionally. No logo — that is added separately by the code pipeline.
```

## Section B: Clean Photo (when text_overlay = false)

Append this section after the universal preamble when the Strategist has assigned no text overlay for the post.

```
PURPOSE: This image accompanies a social media post on {{PLATFORM}}. The photo itself is the scroll-stopping element. There is no text overlay. The image should feel like a polished, professional version of a real photo — not advertising material.

COMPOSITION:

1. Let the {{CATALOG_PRIMARY_SUBJECT}} and its environment tell the story. The image should feel authentic and grounded.

2. Enhance the composition to draw the eye to the main subject. Use subtle adjustments to lighting, contrast, and color to make the subject stand out without making the image look artificially processed.

3. If the source photo has distracting elements in the background (clutter, partial objects, overexposed areas), subtly minimize them without fabricating a new background.

4. The final image should look like the best version of this photo a skilled photographer would produce — sharp, well-composed, good contrast, natural color.

5. Do not add any text, graphics, overlays, borders, frames, or watermarks.

OUTPUT: A finished image with no text overlay and no logo. Logo is added separately by the code pipeline.
```

---

## Content Type Guidance

The Drafter may optionally append a one-line content type hint after either section above to help the image model understand the intended tone. This is not a separate prompt section — it is a single sentence the Drafter writes based on the assigned content type.

| Content Type | Suggested Hint |
|-------------|---------------|
| Equipment Spotlight / Product Feature | "The image should showcase the {{CATALOG_PRIMARY_SUBJECT}} as the clear hero — make it look capable and ready to work." |
| Use-Case Scenario | "The image should evoke the job site or project situation described in the post." |
| Educational Tip | "The image should feel informative and grounded — like a field reference, not an advertisement." |
| Behind-the-Scenes | "The image should feel candid and authentic — real work happening, not staged." |
| Local Connection | "The image should feel rooted in the local environment and recognizable to someone from the area." |
| Promotional / Offer | "The image should feel direct and clear — the subject should be front and center with no ambiguity." |
| Social Proof / Customer Story | "The image should feel real and trustworthy — the result or the job speaks for itself." |
| Job Story / Field Report | "The image should feel like a snapshot from the field — honest conditions, real work." |
| Comparison / Decision Helper | "The image should clearly feature the {{CATALOG_PRIMARY_SUBJECT}} in a way that highlights its distinguishing characteristics." |

The Drafter includes the hint only when it adds useful direction. If the source photo and universal preamble already communicate the right tone, the hint can be omitted.

---

## Placeholder Reference

| Placeholder | Source in business_config.yaml | Example (TES Rentals) |
|-------------|-------------------------------|----------------------|
| {{PLATFORM}} | Injected by Drafter from Content Queue row | "Facebook/Instagram" or "Google Business Profile" |
| {{TYPOGRAPHY_STYLE}} | `brand_visuals.typography_style` | "Bold, rugged typography with texture or grit..." |
| {{CATALOG_PRIMARY_SUBJECT}} | `catalog.primary_subject` | "equipment" |
| {{BUSINESS_NAME}} | `business.name` | "T.E.S. Rentals" |
| {{BUSINESS_DESCRIPTION}} | `business.description` | "local equipment rental company in North Florida" |
| {{BRAND_VISUAL_FEEL}} | `brand_visuals.feel` | "Practical, rugged, reliable..." |
| [HOOK_TEXT] | Hook Creation Skill output (recommended hook for the assigned channel) | "Most people rent the wrong size excavator" |

Note: {{BUSINESS_NAME}}, {{BUSINESS_DESCRIPTION}}, and {{BRAND_VISUAL_FEEL}} are injected in the universal preamble, not in this file. They are listed here for completeness.

---

## Relationship to Other Files

| File | Relationship |
|------|-------------|
| image_prompt_universal_preamble_portable.md | Always prepended before this file's output. Establishes source photo rules, subject handling, and brand feel. |
| image_prompt_advisory_post_portable.md | **Superseded by this file.** This file covers all social post types including advisory posts. |
| image_prompt_link_post_portable.md | **Superseded by this file.** This file covers all social post types including link posts. |
| Hook Creation Skill | Produces the [HOOK_TEXT] value used in Section A. |
| Content Type Definitions | Informs the Drafter's choice of content type hint. |
| Platform Style Skill | Defines target dimensions and aspect ratios. The code pipeline uses these, not this prompt. |

---

*Image Prompt — Social Posts (Portable) v1*
*Last updated: 2026-05-20*
