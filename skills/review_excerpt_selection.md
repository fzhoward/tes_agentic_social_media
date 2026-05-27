# Review Excerpt Selection — Skill Guide

*Lightweight skill for selecting the most impactful portion of a customer review for use in social proof creatives.*

## Purpose

When a 5-star review is used in a Creatomate social proof template, the `Review-Text` field displays a short excerpt — not the full review. The Drafter must select the portion of the review that best communicates *why* the reviewer gave 5 stars. This excerpt appears as the visual centerpiece of the creative, so it needs to hit immediately.

## Selection Criteria

1. **Pick the proof, not the pleasantry.** Skip generic openers ("Great company", "Highly recommend") and find the phrase that names what was actually good — fast delivery, right machine, helpful crew, came through in a pinch.
2. **Must be a verbatim substring.** The excerpt must appear exactly in the original review text. Do not rearrange, combine, or paraphrase. You may trim from either end but cannot alter the interior.
3. **Self-contained readability.** The excerpt must make sense on its own without the surrounding sentences. It should read as a complete thought, not a fragment that trails off or starts mid-idea.
4. **Favor specificity.** A specific detail ("had the skid steer delivered same day") is always stronger than a general compliment ("very professional service").
5. **Favor action and outcome.** Phrases describing what happened or what resulted ("saved us two days on the job") outperform descriptions of feelings ("we were very happy").

## Character Limit

Each Creatomate review template defines its own `max_review_text_chars` in the business config. The Drafter reads this value from the selected template's config entry and passes it to you as the character budget.

Limits vary by template (font size, layout, animation timing all affect how much text fits), so there is no single category-level number. Always use the limit provided for the specific template being rendered.

If the best excerpt exceeds the limit, find the next-best excerpt that fits. Do not truncate a good excerpt to force it under the limit.

## Examples

### Example 1

**Full review:** "Great company to work with. They delivered the mini excavator right on time and it was exactly the machine we needed for our foundation work. Will definitely rent from them again."

- ❌ Bad excerpt: `"Great company to work with. They delivered the mini"` — generic opener + truncated mid-sentence
- ✅ Good excerpt: `"delivered the mini excavator right on time and it was exactly the machine we needed"` — names the equipment, highlights punctuality and correct fit

### Example 2

**Full review:** "Zeb went above and beyond helping us figure out which machine would work best for clearing our back lot. Ended up with the forestry mulcher and it chewed through everything in half a day."

- ❌ Bad excerpt: `"Zeb went above and beyond helping us figure out which"` — cuts off, loses the payoff
- ✅ Good excerpt: `"the forestry mulcher and it chewed through everything in half a day"` — specific machine, vivid result

### Example 3

**Full review:** "Very professional and easy to deal with. Fair pricing too."

- ❌ Bad excerpt: `"Very professional and"` — fragment, no substance
- ✅ Good excerpt: `"Professional and easy to deal with. Fair pricing too."` — short review, take the meaningful portion even if it includes a softer phrase

### Example 4

**Full review:** "Needed a last-minute rental for a weekend project and they came through. Equipment was clean, ran great, and they even helped me load it. Five stars all day."

- ❌ Bad excerpt: `"Needed a last-minute rental for a weekend"` — setup without payoff
- ✅ Good excerpt: `"they came through. Equipment was clean, ran great"` — captures reliability and equipment quality
