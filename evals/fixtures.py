"""Frozen Critic fixtures for the variance harness.

Each fixture is a snapshot of a draft as it would appear in the Content
Queue at the moment the Critic is invoked. Captions are stored verbatim
(triple-quoted strings) so the bytes the LLM sees are identical across
K runs and reproducible across weeks.

Do NOT pull these from the live Content Queue at run time. Variance is
defined on byte-identical input across K runs; a moving target makes the
metric meaningless. If a fixture stops representing a real-world case we
care about, replace it explicitly rather than letting it drift.

The four seed fixtures span:

  1. ``str_20260531_ig_01`` — equipment spotlight with focus_equipment_id
     set, catalog model named in the caption. Expected STABLE-PASS on C4.
  2. ``str_20260602_fb_01`` — educational tip with no focus_equipment_id.
     Known noisy: the Session 27 pass <-> hard_fail flip case on C4.
  3. ``clean_high_quality`` — short, specific, well-formed FB caption with
     no obvious issues. Expected STABLE-PASS across the board.
  4. ``weak_generic`` — vague, low-specificity caption. Expected stable
     FAIL on at least C-family specificity checks.
  5. ``zx135_reduced_tail_swing`` — equipment post with focus_equipment_id
     set, catalog ``tail_swing="Reduced"``. The real A3-vs-G1 misfire case:
     "reduced tail swing" is catalog-grounded. Expected STABLE-PASS on A3.

Add new fixtures by appending to ``FIXTURES``. Keep them small — a few
focused cases beat a sprawling list because each fixture costs K API
calls in real mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agents import critic


@dataclass(frozen=True)
class Fixture:
    """A frozen Critic input case.

    Attributes:
        name: Short slug used as the report row label and JSON key.
        description: One-line human-readable note about what this case
            tests. Shown in the report header.
        row: Content Queue row dict in the shape ``evaluate_draft``
            expects (CQ_* keys from ``agents.critic``).
        catalog_item: Catalog row dict (CAT_* keys) or ``None`` when the
            row has no focus equipment.
        review_text: Featured review text for Social Proof rows; empty
            string otherwise.
        revision_round: Critic revision round (1 on first pass).
        previous_critic_output: Previous Critic JSON when
            ``revision_round > 1``; ``None`` otherwise.
        expected_stable_pass: Optional set of check IDs that *should*
            land in passed_checks unanimously across K runs. Used by the
            report to call out regressions, not by the variance math.
        expected_stable_fail: Optional set of check IDs that *should*
            land in failed_checks unanimously. Same note as above.
    """

    name: str
    description: str
    row: dict[str, Any]
    catalog_item: dict[str, Any] | None
    review_text: str = ""
    revision_round: int = 1
    previous_critic_output: dict[str, Any] | None = None
    expected_stable_pass: frozenset[str] = field(default_factory=frozenset)
    expected_stable_fail: frozenset[str] = field(default_factory=frozenset)


# --- Caption 1 — set-focus, model named, expected STABLE-PASS on C4 ---
# Lifted verbatim from the Content Queue. Do not edit phrasing without
# noting it explicitly: the whole point of a frozen fixture is byte
# stability across K runs and across weeks.
_CAPTION_STR_20260531_IG_01 = (
    "June rain does not slow down the right machine.\n"
    "\n"
    "North Florida's rainy season hits hard in early summer.\n"
    "\n"
    "Steamy mornings. Afternoon downpours. Jobsites that stay wet all "
    "day.\n"
    "\n"
    "Most operators are fighting the conditions instead of working "
    "through them.\n"
    "\n"
    "The John Deere 325G with enclosed cab changes that calculation.\n"
    "\n"
    "Climate-controlled.\n"
    "\n"
    "74 HP and high-flow hydraulics mean this machine handles "
    "demanding attachment work without hesitation.\n"
    "\n"
    "ROC of 2,690 lbs. Tipping load of 7,700 lbs. Lift height of 10 ft "
    "6 in.\n"
    "\n"
    "Those numbers hold up whether you are running a breaker, a power "
    "rake, or a grader attachment.\n"
    "\n"
    "The enclosed cab keeps the operator focused and productive.\n"
    "\n"
    "Not just comfortable. Productive.\n"
    "\n"
    "That distinction matters on long summer days when heat and "
    "humidity drain focus before noon.\n"
    "\n"
    "If your schedule does not pause for weather, your machine choice "
    "should reflect that.\n"
    "\n"
    "Save this for when you are planning a summer project that cannot "
    "wait out the rain."
)

# --- Caption 2 — no-focus educational, the known C4 flip case ---
_CAPTION_STR_20260602_FB_01 = (
    "Bigger equipment does not always mean a faster job.\n"
    "\n"
    "On tight residential lots, it often means the opposite.\n"
    "\n"
    "I see it regularly. A homeowner or landscaper assumes a larger "
    "excavator will get the job done quicker.\n"
    "\n"
    "Tight access.\n"
    "\n"
    "Then the machine spends half the day repositioning.\n"
    "\n"
    "Zero tail swing compact excavators exist for exactly this "
    "scenario. Pool excavations with fence lines on three sides. "
    "Drainage work between a house and a retaining wall. Utility "
    "trenching in a side yard with no room to swing.\n"
    "\n"
    "North Florida clay compounds the problem. Wet clay limits "
    "maneuverability even further. A bigger machine stuck in saturated "
    "soil costs you time, not saves it.\n"
    "\n"
    "Small lot. Wrong machine.\n"
    "\n"
    "The right fit depends on access width, swing clearance, and soil "
    "condition. Not just bucket capacity.\n"
    "\n"
    "What site conditions have changed your machine choice on a job? "
    "Drop it in the comments."
)

# --- Caption 3 — clean high-quality FB draft, expected stable pass ---
_CAPTION_CLEAN_HIGH_QUALITY = (
    "Two months out from a residential dig and the schedule keeps "
    "slipping.\n"
    "\n"
    "Every reroute around the existing fence costs the crew a "
    "morning.\n"
    "\n"
    "The Kubota KX040 with zero tail swing closes that gap.\n"
    "\n"
    "Compact swing radius.\n"
    "\n"
    "Bucket capacity of 0.13 cu yd handles the trench backfill "
    "without staging a second machine on site.\n"
    "\n"
    "Operators clear the lot, finish the dig, and move on with the "
    "day.\n"
    "\n"
    "No torn-up grass.\n"
    "\n"
    "No callbacks the next week from a frustrated homeowner.\n"
    "\n"
    "Save this for the next residential dig you scope."
)

# --- Caption 4 — deliberately weak/generic, expected stable fail ---
_CAPTION_WEAK_GENERIC = (
    "We have the best equipment in the area.\n"
    "\n"
    "Our rental fleet covers everything you might need on any "
    "jobsite.\n"
    "\n"
    "Quality machines.\n"
    "\n"
    "Trusted service.\n"
    "\n"
    "Reach out today to learn more about our offerings and find the "
    "right solution for your project needs.\n"
    "\n"
    "Call us anytime."
)

# --- Caption — ZX135 reduced-tail-swing, the A3 vs G1 misfire case ---
# Real Content Queue caption that was terminally hard_failed on A3 for the
# sentence "The ZX135 runs reduced tail swing", which is catalog-grounded
# (TES-017 tail_swing = "Reduced"). G2/G3 passed in the same run. Frozen
# here to verify A3 no longer fires after the re-scope. Do not edit phrasing.
_CAPTION_ZX135_REDUCED_TAIL_SWING = (
    "Digging near an existing structure changes everything about machine "
    "selection.\n"
    "\n"
    "A standard excavator swings wide on a tight job.\n"
    "\n"
    "That tail swing can put you into a wall, a footer, or a fence line.\n"
    "\n"
    "The ZX135 runs reduced tail swing.\n"
    "\n"
    "You get full digging force without the clearance problem.\n"
    "\n"
    "Close quarters.\n"
    "\n"
    "23,380 lb bucket digging force. 19 ft 11 in of dig depth. 28 ft 7 in "
    "of reach.\n"
    "\n"
    "That is a serious machine for serious footer and utility work.\n"
    "\n"
    "Hydraulic thumb and backfill blade are included.\n"
    "\n"
    "No upsizing to get the right setup.\n"
    "\n"
    "If you are working tight to a building, the machine has to fit the "
    "constraint.\n"
    "\n"
    "Not the other way around.\n"
    "\n"
    "Tell us what you are working on. Call (904) 452-0888 and we will help "
    "you confirm the right machine before you schedule."
)


def _make_row(
    row_id: str,
    platform: str,
    objective: str,
    content_type: str,
    focus_equipment_id: str,
    cta_type: str,
    caption: str,
    creative_hook_text: str,
    hook_text: str,
    cta_text: str,
    media_url: str = "drive-id-fixture",
) -> dict[str, Any]:
    """Assemble a Content Queue row dict with the shape evaluate_draft expects."""
    return {
        critic.CQ_ROW_ID: row_id,
        critic.CQ_STATUS: "drafted",
        critic.CQ_PLATFORM: platform,
        critic.CQ_OBJECTIVE: objective,
        critic.CQ_CONTENT_TYPE: content_type,
        critic.CQ_FOCUS_EQUIPMENT: focus_equipment_id,
        critic.CQ_CTA_TYPE: cta_type,
        critic.CQ_MEDIA_FORMAT: "image2_enhanced",
        critic.CQ_MEDIA_FORMAT_USED: "image2_enhanced",
        critic.CQ_MEDIA_URL: media_url,
        critic.CQ_REVIEW_ID: "",
        critic.CQ_CAPTION: caption,
        critic.CQ_CREATIVE_HOOK_TEXT: creative_hook_text,
        critic.CQ_FIRST_COMMENT: "",
        critic.CQ_CTA_TEXT: cta_text,
        critic.CQ_HOOK_TEXT: hook_text,
        critic.CQ_IMAGE_OVERLAY_TEXT: "",
    }


# Catalog entry for TES-004 (John Deere 325G). The spec numbers match
# the caption verbatim so the deterministic G3 spec-rounding check does
# not false-fire in real mode. Adjust to live catalog values when the
# fixture is refreshed.
_CATALOG_TES_004: dict[str, Any] = {
    critic.CAT_ITEM_ID: "TES-004",
    critic.CAT_STATUS: "active",
    critic.CAT_ITEM_NAME: "Compact Track Loader",
    critic.CAT_MODEL: "John Deere 325G",
    critic.CAT_WEIGHT: "",
    critic.CAT_DIG_DEPTH: "",
    critic.CAT_REACH: "10 ft 6 in",
    critic.CAT_CAPACITY: "2,690 lbs",
    critic.CAT_HORSEPOWER: "74 HP",
    critic.CAT_TAIL_SWING: "",
}

# Catalog entry for the clean-high-quality fixture. Matches the caption's
# stated bucket capacity to avoid G3 false-positives.
_CATALOG_KUBOTA_KX040: dict[str, Any] = {
    critic.CAT_ITEM_ID: "TES-013",
    critic.CAT_STATUS: "active",
    critic.CAT_ITEM_NAME: "Mini Excavator",
    critic.CAT_MODEL: "Kubota KX040",
    critic.CAT_WEIGHT: "",
    critic.CAT_DIG_DEPTH: "",
    critic.CAT_REACH: "",
    critic.CAT_CAPACITY: "0.13 cu yd",
    critic.CAT_HORSEPOWER: "",
    critic.CAT_TAIL_SWING: "zero",
}

# Catalog entry for TES-017 (Hitachi ZX135). Values verbatim from the live
# Equipment_Catalog_TES_Rentals sheet. tail_swing="Reduced" is the field that
# makes "reduced tail swing" a supported claim (must not A3-fail). The three
# caption numbers (23,380 lb / 19 ft 11 in / 28 ft 7 in) match these spec
# strings exactly so the deterministic G3 rounding check does not false-fire.
_CATALOG_TES_017: dict[str, Any] = {
    critic.CAT_ITEM_ID: "TES-017",
    critic.CAT_STATUS: "active",
    critic.CAT_ITEM_NAME: "Excavator - 30K - ZX135",
    critic.CAT_MODEL: "ZX 135",
    critic.CAT_WEIGHT: "30,500 lbs",
    critic.CAT_DIG_DEPTH: "Dig Depth: 19' 11\"",
    critic.CAT_REACH: "Reach: 28 ft 7 in",
    critic.CAT_CAPACITY: "Digging Force: 23,380 lb bucket",
    critic.CAT_HORSEPOWER: "100 HP",
    critic.CAT_TAIL_SWING: "Reduced",
}


FIXTURES: tuple[Fixture, ...] = (
    Fixture(
        name="str_20260531_ig_01",
        description=(
            "Equipment Spotlight (IG), focus_equipment_id=TES-004, "
            "John Deere 325G named in caption — expected STABLE-PASS "
            "on C4."
        ),
        row=_make_row(
            row_id="STR-20260531-IG-01",
            platform="instagram",
            objective="education",
            content_type="Equipment Spotlight / Product Feature",
            focus_equipment_id="TES-004",
            cta_type="save",
            caption=_CAPTION_STR_20260531_IG_01,
            creative_hook_text="Rain ready",
            hook_text="June rain does not slow down the right machine.",
            cta_text=(
                "Save this for when you are planning a summer project "
                "that cannot wait out the rain."
            ),
        ),
        catalog_item=_CATALOG_TES_004,
        expected_stable_pass=frozenset({"C4"}),
    ),
    Fixture(
        name="str_20260602_fb_01",
        description=(
            "Educational Tip (FB), no focus_equipment_id — known C4 "
            "flip case from Session 27 (pass <-> hard_fail on identical "
            "input)."
        ),
        row=_make_row(
            row_id="STR-20260602-FB-01",
            platform="facebook",
            objective="education",
            content_type="Educational Tip",
            focus_equipment_id="",
            cta_type="comment",
            caption=_CAPTION_STR_20260602_FB_01,
            creative_hook_text="Wrong machine",
            hook_text="Bigger equipment does not always mean a faster job.",
            cta_text=(
                "What site conditions have changed your machine choice "
                "on a job? Drop it in the comments."
            ),
        ),
        catalog_item=None,
    ),
    Fixture(
        name="clean_high_quality",
        description=(
            "Clean, specific Educational Tip with named machine and "
            "exact catalog spec — expected stable pass."
        ),
        row=_make_row(
            row_id="EVAL-CLEAN-01",
            platform="facebook",
            objective="education",
            content_type="Educational Tip",
            focus_equipment_id="TES-013",
            cta_type="save",
            caption=_CAPTION_CLEAN_HIGH_QUALITY,
            creative_hook_text="Zero swing wins",
            hook_text=(
                "Two months out from a residential dig and the schedule "
                "keeps slipping."
            ),
            cta_text="Save this for the next residential dig you scope.",
        ),
        catalog_item=_CATALOG_KUBOTA_KX040,
    ),
    Fixture(
        name="weak_generic",
        description=(
            "Deliberately vague brand-awareness draft — expected stable "
            "fail on C-family specificity checks."
        ),
        row=_make_row(
            row_id="EVAL-WEAK-01",
            platform="facebook",
            objective="brand_awareness",
            content_type="Educational Tip",
            focus_equipment_id="",
            cta_type="call",
            caption=_CAPTION_WEAK_GENERIC,
            creative_hook_text="Best equipment",
            hook_text="We have the best equipment in the area.",
            cta_text="Call us anytime.",
        ),
        catalog_item=None,
        expected_stable_fail=frozenset({"C4"}),
    ),
    Fixture(
        name="zx135_reduced_tail_swing",
        description=(
            "Equipment post (FB), focus_equipment_id=TES-017, catalog "
            "tail_swing='Reduced'. The real A3-vs-G1 misfire case: "
            "'reduced tail swing' is catalog-grounded and must NOT fire "
            "A3 (hard_fail). Expected STABLE-PASS on A3."
        ),
        row=_make_row(
            row_id="STR-ZX135-FB-01",
            platform="facebook",
            objective="lead_generation",
            content_type="Equipment Spotlight / Product Feature",
            focus_equipment_id="TES-017",
            cta_type="call",
            caption=_CAPTION_ZX135_REDUCED_TAIL_SWING,
            creative_hook_text="Fit the constraint",
            hook_text=(
                "Digging near an existing structure changes everything "
                "about machine selection."
            ),
            cta_text=(
                "Tell us what you are working on. Call (904) 452-0888 and "
                "we will help you confirm the right machine before you "
                "schedule."
            ),
        ),
        catalog_item=_CATALOG_TES_017,
        expected_stable_pass=frozenset({"A3"}),
    ),
)


def get_fixture(name: str) -> Fixture:
    """Look up a fixture by its name slug. Raises KeyError on miss."""
    for fx in FIXTURES:
        if fx.name == name:
            return fx
    raise KeyError(
        f"unknown fixture {name!r}; available: "
        f"{[fx.name for fx in FIXTURES]}"
    )
