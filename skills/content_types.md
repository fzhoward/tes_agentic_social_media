# Content Type Definitions — Portable

*V1 — defines the content types the Strategist can assign when planning posts*

## Purpose

Define each content type the Strategist can select when planning social media posts. For each type: what it is, what objective it typically serves, which platforms it fits, and what inputs the Drafter needs to produce it. This is a portable skill. It contains no brand, industry, or business assumptions.

## How This File Is Used

- **Strategist:** Reads this file to select the right content type for each planned post. Uses the objective lean, platform fit, and required inputs to make informed selections.
- **Drafter:** Receives the assigned content type and reads the definition to understand the expected output shape, tone, and constraints.
- **Critic:** Verifies the draft matches the assigned content type definition and that the objective alignment is correct.

## Objective Definitions

Two post objectives exist. The Strategist assigns one per post. The objective determines which voice rules, CTA rules, and success metrics apply.

**Brand Awareness:** Build trust, familiarity, and local presence. The reader should walk away thinking the business knows what it is doing or remembering the brand positively. No conversion CTA. Engagement CTA optional.

**Lead Generation:** Move the reader toward a specific next action: call, message, book, or request a quote. Direct CTA required. Must name the customer situation before the ask.

Each content type has a native objective lean (the objective it most naturally serves), but the Strategist can override the lean when the framing supports it. For example, an equipment spotlight is naturally brand awareness, but framed around a specific customer problem with a call CTA, it becomes lead generation.

---

## Content Types

### 1. Equipment Spotlight / Product Feature

**Definition:** Showcase a single catalog item with its key specs, use cases, or distinguishing characteristics. The item is the subject. The post makes the reader understand what it does, when it is the right choice, and why it matters for their situation.

**Objective lean:** Brand awareness

**Platform fit:**
- Facebook: Strong. Photo + specs + use-case angle.
- Instagram: Strong. Visual-first, spec details in caption.
- GBP: Moderate. Works when framed as answering a searcher's question about what the business offers.

**Required inputs:**
- Catalog item record (item_name, description, category, relevant spec fields)
- Primary image or image selection criteria
- Angle or hook direction from the Strategist (e.g., "focus on dig depth for residential jobs")

**Optional inputs:**
- Firsthand experience note about the item
- Comparison context (e.g., "versus the larger model")

---

### 2. Use-Case Scenario

**Definition:** Frame a specific customer situation, project type, or problem and show how the right product or service addresses it. The customer's situation is the subject, not the product. The post makes the reader recognize themselves and understand the path to a solution.

**Objective lean:** Lead generation

**Platform fit:**
- Facebook: Strong. Narrative framing works well in feed.
- Instagram: Strong. Visual of the scenario + caption that names the situation.
- GBP: Strong. Directly matches the searcher's decision-mode mindset.

**Required inputs:**
- Customer situation or problem description
- Relevant catalog item(s)
- Desired customer action (call, DM, book)
- CTA destination (phone, booking URL)

**Optional inputs:**
- Specific local, seasonal, or condition context
- Firsthand experience note

---

### 3. Educational Tip

**Definition:** Deliver one practical, useful piece of advice the reader can apply to their own situation. Standalone value — the reader walks away knowing something they did not know before. The advice should come from real expertise, not generic internet knowledge.

**Objective lean:** Brand awareness

**Platform fit:**
- Facebook: Strong. Longer educational caption is acceptable in feed.
- Instagram: Strong. Tip + visual context.
- GBP: Moderate. Must be condensed to GBP length. Works best as a "one thing to know" format.

**Required inputs:**
- Topic or question being answered
- The actual advice or tip (specific, not generic)
- Source of the advice (operator experience, industry knowledge, local conditions)

**Optional inputs:**
- Relevant catalog item connection
- Local or seasonal context that affects the advice

---

### 4. Behind-the-Scenes

**Definition:** Show the real work, preparation, maintenance, or day-to-day operations that the customer normally does not see. Builds trust by demonstrating competence, care, and authenticity. Not a polished marketing piece — it should feel genuine.

**Objective lean:** Brand awareness

**Platform fit:**
- Facebook: Strong. Casual, personality-driven content performs well.
- Instagram: Strong. BTS is a native Instagram format.
- GBP: Weak. Searchers in decision mode are less interested in BTS. Use sparingly.

**Required inputs:**
- What is being shown (maintenance routine, delivery prep, equipment inspection, etc.)
- Photo or visual description
- Brief context about why this matters to the customer

**Optional inputs:**
- Owner or operator voice note
- Specific detail that shows expertise (e.g., "checking hydraulic lines before every rental")

---

### 5. Local Connection

**Definition:** Reference a specific local event, condition, season, weather pattern, community activity, or geographic reality that connects the business to its service area. Makes the business feel like a neighbor, not a faceless provider.

**Objective lean:** Brand awareness

**Platform fit:**
- Facebook: Strong. Local content gets organic engagement.
- Instagram: Moderate. Works when the visual is strong.
- GBP: Strong. Reinforces local relevance for searchers.

**Required inputs:**
- The local reference (event, season, weather, condition, community connection)
- How it connects to the business or its customers
- Service area context

**Optional inputs:**
- Relevant catalog item tie-in
- Firsthand local experience

---

### 6. Promotional / Offer

**Definition:** Announce a specific offer, availability window, seasonal promotion, or new service. Direct and factual — not hype-driven. The offer must be real and include any relevant limitations, dates, or conditions.

**Objective lean:** Lead generation

**Platform fit:**
- Facebook: Strong. Promotional posts are expected in feed.
- Instagram: Moderate. Must be visually interesting, not just text about a deal.
- GBP: Strong. GBP "Offer" post type is built for this.

**Required inputs:**
- Offer details (what, when, conditions, limitations)
- Desired customer action
- CTA destination

**Constraints:**
- If `strategy.pricing_in_posts` is "never", the offer must not include dollar amounts or price ranges. Frame around availability, timing, or bundled value instead.
- All offer claims must be currently accurate. The Critic rejects expired or fabricated offers.

**Optional inputs:**
- Who the offer is best for (customer segment)
- Seasonal or timing context

---

### 7. Social Proof / Customer Story

**Definition:** Use a real customer experience, review, testimonial, completed project, or measurable outcome to build trust. Must be based on real, verifiable information from a system data source — never fabricated.

**Objective lean:** Lead generation

**Platform fit:**
- Facebook: Strong. Social proof is high-engagement content.
- Instagram: Strong. Before/after visuals, project photos.
- GBP: Strong. Reinforces trust signals for searchers evaluating the business.

**Data source:** Reviews Sheet (`catalog.reviews_sheet_id`). The Strategist selects a `review_id` from rows where `usable_for_social=TRUE` and writes it onto the Content Queue row. The Drafter reads the selected row to pull review text, reviewer first name, and excerpts.

**Required inputs:**
- A `review_id` selected by the Strategist from the Reviews Sheet
- The matching Reviews Sheet row (reviewer first name, star rating, review text, excerpts)
- Connection to the business's value proposition

**Media format:** `creatomate_review_image` (default) or `creatomate_review_video`. No other media format is valid for this content type, and the review formats are valid for no other content type.

**Constraints:**
- Do not fabricate customer stories, reviews, statistics, or outcomes. Every Social Proof post must trace back to a real `review_id`.
- Do not paraphrase reviews in a way that changes meaning.
- Only rows with `usable_for_social=TRUE` are eligible (5-star, non-empty text, meets minimum length).
- If the Reviews Sheet is empty or unavailable, skip this content type for the batch.

**Optional inputs:**
- `focus_equipment_id` — pair the review with a catalog item when the review clearly maps to specific equipment or a job type. Enables the `photo_testimonial` (image) and `photo_reveal` (video) templates, which accept `Equipment-Photo`. Leave empty if no clear match.
- Customer situation context
- Before/after comparison

---

### 8. Job Story / Field Report

**Definition:** Tell the story of a specific job, project, or situation the business handled. Grounded in real events. Shows the business in action — the challenge, the approach, and the result. Not a case study — it is a short, engaging narrative.

**Objective lean:** Brand awareness

**Platform fit:**
- Facebook: Strong. Narrative storytelling works well.
- Instagram: Strong. Visual storytelling with project photos.
- GBP: Moderate. Must be condensed. Works when framed as "here is what we did for a customer like you."

**Required inputs:**
- Job or project details (what, where, what was involved)
- The challenge or interesting aspect
- The outcome or current status
- Photo or visual description

**Constraints:**
- Must be based on a real job. Do not fabricate job stories.
- Respect customer privacy — no identifying details unless approved.

**Optional inputs:**
- Operator experience note
- Relevant catalog item connection
- Local or seasonal context

---

### 9. Comparison / Decision Helper

**Definition:** Help the reader compare two or more options, understand tradeoffs, or navigate a decision. Not a product review or ranking — it is practical guidance for someone trying to decide. The business's expertise makes the comparison useful.

**Objective lean:** Brand awareness (with lead generation override when paired with a consultation CTA)

**Platform fit:**
- Facebook: Strong. Decision content gets saved and shared.
- Instagram: Moderate. Needs a strong visual format (side-by-side, carousel concept in caption).
- GBP: Strong. Directly serves the decision-mode searcher.

**Required inputs:**
- The decision or comparison being addressed
- The key factors that determine the right choice
- Enough expertise to make the comparison genuinely useful (not surface-level)

**Optional inputs:**
- Relevant catalog items on each side of the comparison
- Local or situational context that affects the decision
- Firsthand experience with both options

---

## Content Type Selection Logic (For the Strategist)

When planning the next batch of posts, the Strategist should:

1. Check the variety constraint: no content type repeated consecutively on the same platform.
2. Check the item constraint: no catalog item repeated within 7 days.
3. Read the current objective ratio (brand awareness vs. lead generation) and select content types that correct any drift.
4. Match content types to available inputs. Do not assign Social Proof if no `usable_for_social=TRUE` rows exist in the Reviews Sheet. Do not assign Behind-the-Scenes if no BTS material is available.
5. Prefer content types with stronger platform fit for the target platform.
6. Use performance data from Strategy Guidance to weight content types that are performing well.

## Mapping to Brand Voice Objective Rules

When the Drafter receives a content type + objective assignment, it should apply the matching voice rules from the business's brand voice document:

- **Brand awareness objective** → Brand Awareness voice rules (looser tone, no conversion CTA, engagement CTA optional)
- **Lead generation objective** → Lead Generation voice rules (direct tone, call or DM CTA required, CTA last, name the situation first)
- **Advisory / educational content types** (Educational Tip, Comparison) → Advisory Post voice rules
- **All content types** → Default content rules, formatting rules, banned language, trust and safety rules

## Changelog

V1 — May 2026

- Initial 9 content types defined from architecture plan Section 4.
- Renamed "AI illustration" from architecture plan to "Comparison / Decision Helper" (the media type is separate from the content type).
- Added "Job Story / Field Report" as a distinct content type (was implicit in architecture plan).
- Each type includes definition, objective lean, platform fit, required inputs, and optional inputs.
- Strategist selection logic and brand voice mapping documented.