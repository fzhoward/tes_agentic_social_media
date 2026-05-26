# CTA Skill — Portable

*V1 — portable framework for call-to-action selection and phrasing across platforms*

## Purpose

Select and phrase the correct call-to-action for each social post based on the post objective, platform, and desired customer action. This is a portable skill. It contains no brand, industry, or business assumptions. Supply the business context, CTA destinations, and brand voice rules on top of this framework.

## Core Principle

A CTA tells the reader exactly what to do next and makes it feel like a natural extension of the post, not a sales pitch bolted on at the end. The CTA must match both the post objective and the platform's capabilities.

## CTA Types

Each CTA type maps to a specific customer action. The Strategist assigns the CTA type when planning the post. The Drafter phrases it. The Critic verifies it matches the assigned type and follows all rules.

| CTA Type | Customer Action | Typical Destinations |
|----------|----------------|---------------------|
| call | Phone call to the business | Phone number in post or GBP button |
| dm / message | Direct message on the platform | FB Messenger, IG DM |
| click / link | Tap through to a URL | First comment link (FB/IG), GBP button URL |
| comment | Engage in comments | Comment section on the post |
| visit | Visit website or profile | Website URL, GBP "Visit" button |
| book | Schedule or reserve | Booking URL, GBP "Book" button |
| save | Save the post for later | Platform save feature |
| directions | Get directions to a location | Google Maps URL, GBP "Directions" button |
| none | No CTA (brand awareness) | N/A |

## CTA Type by Post Objective

Not every CTA type fits every objective. These are the defaults.

### Brand Awareness Posts

- **Allowed:** comment, save, none
- **Not allowed:** call, dm, click, visit, book, directions
- **Rationale:** Brand awareness builds trust and familiarity. Conversion CTAs undermine the casual, trust-building tone. Engagement CTAs are optional.

### Lead Generation Posts

- **Required:** One of: call, dm
- **Not allowed:** comment, save, none, click
- **Rationale:** Lead generation must drive a direct, measurable action. Call and DM are the highest-signal lead actions. Comment and save do not generate leads reliably.

### Link / Click-Through Posts

- **Required:** click
- **Not allowed:** call, dm, book (these compete with the link)
- **Rationale:** The entire post exists to drive a click. Do not introduce competing actions.

### Advisory / Educational Posts

- **Allowed:** comment, save, none
- **Optionally allowed:** call, dm (only if the advice naturally leads to a consultation)
- **Rationale:** Advisory posts deliver standalone value. A CTA is optional and should not feel forced.

### GBP Posts (Any Objective)

- **Allowed:** call, visit, book, directions, click
- **Mapped to GBP button types** (see Platform Behavior section)
- **Not allowed:** dm, save, comment (GBP does not support these natively)

## Phrasing Rules

### General Rules

1. The CTA is always the last element of the caption. Nothing follows it.
2. One CTA per post. Never stack multiple actions (e.g., "Call us or visit our website or DM us").
3. The CTA must be direct. Use imperative phrasing: "Call us at..." not "Feel free to reach out if..."
4. The CTA must name the action specifically. "Contact us" is too vague. "Call us at {{PHONE}}" is specific.
5. For lead generation posts, the post must name the customer situation or problem before the CTA. Never lead with the ask.
6. Do not use urgency language unless the post is about a genuinely time-limited situation (seasonal availability, event deadline, limited slots).

### Call CTA Phrasing

Structure: [Reason to call] + [Phone number] + [Optional: what happens when they call]

The reason to call should connect to the post content. Do not use a generic reason when the post gives a specific one.

Weak: "Give us a call."
Strong: "Call us at {{PHONE}}. Tell us what you're working on and we'll help you figure out the right {{CATALOG_PRIMARY_SUBJECT}}."

### DM / Message CTA Phrasing

Structure: [Action verb] + [What to include in the message]

Tell the reader what to say. An empty "DM us" gets fewer responses than "Send us a message with your project details."

Weak: "DM us for more info."
Strong: "Send us a message with your project details."

### Click / Link CTA Phrasing

Structure: [Where to find the link] + [What they'll get]

On platforms where links go in the first comment (FB/IG), the CTA must direct the reader there.

Weak: "Check the link."
Strong: "Full breakdown in the first comment."

### Comment CTA Phrasing

Structure: [Question or prompt that invites a real response]

Comment CTAs work best as open-ended questions tied to the post content. Avoid generic engagement bait.

Weak: "Drop a comment below."
Strong: "What would you use here?" / "Seen this on your jobs?"

### Save CTA Phrasing

Structure: [Reason to save] + [When it will be useful]

Weak: "Save this post."
Strong: "Save this for when your project starts."

### Book CTA Phrasing

Structure: [Action] + [Destination]

Weak: "Book now."
Strong: "Check availability at {{BOOKING_URL}}." / "Get on the schedule at {{BOOKING_URL}}."

### Visit CTA Phrasing

Structure: [What they'll find] + [Destination]

Weak: "Visit our website."
Strong: "See the full specs at {{WEBSITE}}."

### Directions CTA Phrasing

Structure: [When to come] + [Implicit or explicit link]

Only for businesses with a physical location where walk-in/drive-up is relevant.

Weak: "Come see us."
Strong: "Stop by during business hours. Directions on our Google listing."

## Platform Behavior

### Facebook

- Links in first comment (not caption) for link posts.
- CTA text in caption directs reader to first comment.
- Call CTAs include the phone number in the caption text.
- DM CTAs rely on Messenger.

### Instagram

- Links in first comment (not caption) for link posts.
- CTA text in caption directs reader to first comment.
- Call CTAs include the phone number in the caption text.
- DM CTAs rely on IG Direct.

### Google Business Profile

GBP posts support a structured button. The button type and URL are set at publishing time.

| GBP Button Type | Maps To CTA Type | Button URL Source |
|----------------|-------------------|-------------------|
| CALL | call | Phone number from business_config |
| LEARN_MORE | click / visit | {{WEBSITE}} or specific page URL |
| BOOK | book | {{BOOKING_URL}} |
| ORDER | book (for applicable businesses) | Order URL |
| GET_DIRECTIONS | directions | {{GOOGLE_MAPS_URL}} |
| SIGN_UP | click | Signup URL |

The GBP post body should still include CTA text that reinforces the button. The button alone is easy to miss.

## What NOT to Do

1. **Do not stack CTAs.** One action per post. "Call or DM or visit our website" dilutes all three.
2. **Do not use a conversion CTA on a brand awareness post.** It breaks the trust-building tone.
3. **Do not omit the CTA on a lead generation post.** That is the entire point of the post.
4. **Do not use "contact us" as a CTA.** It is not an action. Specify the channel: call, message, or visit.
5. **Do not put the CTA before the post body.** The CTA is always last.
6. **Do not use fake urgency.** "Act now" and "limited time" are only acceptable when genuinely true.
7. **Do not use passive phrasing.** "You can reach us at..." is weaker than "Call us at..."
8. **Do not repeat the CTA.** Once, at the end, is enough.
9. **Do not include a URL in the FB/IG caption body.** Links go in the first comment. The CTA text points there.
10. **Do not use pricing language in the CTA** unless `strategy.pricing_in_posts` explicitly allows it. "Call for a free quote" implies pricing discussion, which is fine. "Rent for only $X/day" is a pricing claim.

## CTA Selection Logic (For Agents)

When the Drafter receives a planned post from the Strategist, it should:

1. Read the assigned `objective` and `cta_type` from the Content Queue row.
2. Verify the CTA type is allowed for the objective (see CTA Type by Post Objective).
3. Select the phrasing pattern that matches the CTA type.
4. Customize the phrasing using the post content, business context, and CTA destinations from `business_config.yaml`.
5. Place the CTA as the last element of the caption.

When the Critic reviews a draft, it should:

1. Verify the CTA type matches the assigned objective.
2. Verify the CTA is the last element.
3. Verify no competing CTAs are present.
4. Verify the phrasing is direct and specific.
5. Verify no pricing language appears (if pricing policy is "never").
6. Verify the CTA destination (phone, URL, etc.) is correct per `business_config.yaml`.

## Changelog

V1 — May 2026

- Initial portable CTA skill.
- CTA types mapped to post objectives and platforms.
- Phrasing patterns with strong/weak examples.
- GBP button type mapping.
- Anti-patterns documented.
- Agent selection and verification logic included.