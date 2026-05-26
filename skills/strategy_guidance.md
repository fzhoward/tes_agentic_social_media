# Strategy Guidance
## Last updated: 2026-05-20 by initial setup (no performance data yet)
## Data basis: 0 posts — all recommendations are defaults

## Content Type Rankings

Ranked by recommended usage frequency. The Strategist should draw from the top of this list more often, but still use all types for variety.

1. **Equipment Spotlight / Product Feature** — Anchor content type. Showcases the catalog directly. TES has deep spec data for most machines — lean into specificity. Use for both objectives.
2. **Use-Case Scenario** — Strong lead generation framing. Name the contractor or homeowner's situation (land clearing, foundation dig, tight-access backyard), connect to the right machine.
3. **Educational Tip** — Builds authority. Zeb's 15+ years of field experience is the source. Tips about machine selection, site conditions, transport considerations, and project planning are natural fits.
4. **Behind-the-Scenes** — Authenticity driver. Maintenance routines, delivery prep, equipment inspections, yard operations. Strong on FB and IG. Use sparingly on GBP.
5. **Local Connection** — North Florida seasonal context: rainy season prep, hurricane cleanup, spring building season, county-specific conditions. Strong on FB and GBP.
6. **Job Story / Field Report** — Real project narratives from TES jobs. Requires real job material from Zeb. High engagement when available.
7. **Comparison / Decision Helper** — Mini excavator vs. skid steer, zero tail swing vs. conventional, machine size selection guides. High value for GBP searchers deciding what to rent.
8. **Social Proof / Customer Story** — High trust signal. Only assign when Zeb provides real customer feedback, reviews, or completed project outcomes. Never fabricate.
9. **Promotional / Offer** — Use only when a real availability window, seasonal promotion, or new machine addition exists. Do not generate promotional content from thin inputs.

**Default weighting guidance:** Types 1-5 should make up roughly 70% of the content mix. Types 6-9 are used when inputs are available and for variety.

**Note:** These rankings are initial defaults. The Learning Agent will reorder based on actual engagement data after 5 posts per content type are published and measured.

## Media Format Recommendations

**Default split for posts requiring media:**
- `image2_enhanced` (clean photo, no text): ~30% of posts — best for Behind-the-Scenes, equipment in context
- `image2_text_overlay` (Image 2 photo + hook text): ~25% of posts — hook-driven scroll-stoppers
- `creatomate_text_overlay` (Creatomate template + hook text): ~25% of posts — alternate with Image 2 for feed variety
- `creatomate_video` (motion video from source still): ~20% of posts — equipment in motion, project reveals

**Text overlay alternation:** Alternate between `image2_text_overlay` and `creatomate_text_overlay` to keep the feed visually varied. Track the last 3 text-overlay posts per platform. If the last 2 used one tool, switch to the other.

**Video frequency:** Maximum 2 `creatomate_video` posts per platform per week. This cap is adjustable by the Learning Agent based on video engagement data.

**Note:** These defaults assume both Image 2 and Creatomate pipelines are operational. If either is unavailable, the other covers both slots.

## Platform-Specific Notes

**Facebook:**
- All content types work. Longer captions are acceptable.
- Equipment Spotlight, Use-Case Scenario, and Behind-the-Scenes are the natural strengths for TES.
- Social Proof and Job Story tend to drive comments and shares from local contractors.
- Link posts use first-comment placement (SocialBu `first_comment` field).

**Instagram:**
- Image quality is critical. Every post requires media (SocialBu API rejects posts without an image).
- Equipment on dirt, gravel, or cleared land — the rugged visual context fits the feed naturally.
- Behind-the-Scenes and Equipment Spotlight are native formats.
- Educational Tips and Comparisons work when the visual is strong.

**GBP (Google Business Profile):**
- Searchers are in decision mode — they're looking for "equipment rental near me." Use-Case Scenario, Comparison, and Local Connection are strongest.
- Keep captions shorter — GBP truncates at ~750 characters in most views.
- Behind-the-Scenes is weak here. Use sparingly.
- Promotional / Offer maps to GBP's native "Offer" post type when applicable.
- Directions CTA uses the Google Maps CID URL (pending — owner needs to provide).

## Timing Recommendations

**Default posting windows (no data yet):**
- Facebook: 9-11 AM ET and 6-8 PM ET
- Instagram: 11 AM-1 PM ET and 7-9 PM ET
- GBP: 8-10 AM ET (catches morning searchers)

**Day of week:** Distribute evenly across the week. No clustering. Respect 4-hour minimum gap between posts on the same platform.

**Note:** These are generic best-practice defaults. The Learning Agent will refine timing based on actual engagement patterns after sufficient data.

## CTA Effectiveness

**No data yet.** Default CTA assignment rules from the CTA Skill apply:
- Brand awareness → `comment`, `save`, or `none`
- Lead generation → `call` (primary for TES — phone is the main conversion point) or `dm` (FB/IG)
- Link post → `click` (first comment)
- GBP → `call`, `visit` (website), `directions` (pending CID URL), `book` (website)

The Learning Agent will track CTA conversion rates per content type and platform, and adjust recommendations once patterns emerge.

## Objective Ratio

**Target:** 60% brand awareness / 40% lead generation
**Current actual:** No data yet.
**Correction:** None needed. Apply target ratio directly to the new batch.

## Active Experiments

None active. The Learning Agent may introduce controlled experiments (e.g., testing video vs. static for Equipment Spotlight) once baseline performance data is established.

## Owner Overrides

No active overrides.

## Data Confidence

**Confidence level:** No data — defaults only.

The system is in the data collection phase. All recommendations above are starting assumptions, not learned patterns. Recommendations will improve and become data-backed after approximately 5 posts per content type are published and measured at both T+24h and T+7d snapshots.

---

*Strategy Guidance — T.E.S. Rentals*
*Initial instance created: 2026-05-20*
*Next scheduled rewrite: After first Learning Agent run (Monday following first week of published posts)*
