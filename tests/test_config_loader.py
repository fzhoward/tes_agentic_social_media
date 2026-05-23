"""Tests for tools/config_loader.py.

Validates the loader against the real business config file in the project root
(business_config_tes_rentals.yaml).

Run from the project root:
    python -m tests.test_config_loader
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CONFIG_FILE = PROJECT_ROOT / "business_config_tes_rentals.yaml"
os.environ["BUSINESS_CONFIG_PATH"] = str(CONFIG_FILE)

from tools.config_loader import Config, load_config  # noqa: E402


_FAILURES: list[tuple[str, str]] = []
_PASSED = 0


def _check(name: str, condition: bool, detail: str = "") -> None:
    global _PASSED
    if condition:
        _PASSED += 1
        print(f"  PASS  {name}")
    else:
        _FAILURES.append((name, detail))
        print(f"  FAIL  {name} — {detail}")


def _expect_raises(name: str, exc_type: type, fn) -> None:
    try:
        result = fn()
    except exc_type as e:
        _check(name, True, str(e))
        return
    except Exception as e:
        _check(
            name,
            False,
            f"expected {exc_type.__name__}, got {type(e).__name__}: {e}",
        )
        return
    _check(
        name,
        False,
        f"expected {exc_type.__name__}, got result: {result!r}",
    )


def run_tests() -> int:
    print(f"Loading config from: {CONFIG_FILE}")
    config = load_config()

    _check(
        "1. File loading — load_config() returns a Config object",
        isinstance(config, Config),
        f"got {type(config).__name__}",
    )

    business = config.get("business")
    _check(
        "2. Top-level access — config.get('business') returns a dict",
        isinstance(business, dict),
        f"got {type(business).__name__}",
    )

    name = config.get("business.name")
    _check(
        "3. Dot-notation access — business.name == 'T.E.S. Rentals'",
        name == "T.E.S. Rentals",
        f"got {name!r}",
    )

    diagonal_id = config.get(
        "creatomate.equipment_post_image.templates.diagonal_slash.id"
    )
    _check(
        "4. Deep nesting — diagonal_slash.id matches expected UUID",
        diagonal_id == "5f7e02d4-5e52-4d4a-96f1-41a8f55c19ac",
        f"got {diagonal_id!r}",
    )

    posts_per_week = config.get("strategy.posts_per_week_per_platform")
    _check(
        "5. Integer preservation — posts_per_week_per_platform == 7 (int)",
        posts_per_week == 7 and isinstance(posts_per_week, int)
        and not isinstance(posts_per_week, bool),
        f"got {posts_per_week!r} ({type(posts_per_week).__name__})",
    )

    first_person = config.get("owner.first_person")
    _check(
        "6. Boolean preservation — owner.first_person is True (bool)",
        first_person is True,
        f"got {first_person!r} ({type(first_person).__name__})",
    )

    budget = config.get("health.monthly_budget_limit")
    _check(
        "7. None preservation — health.monthly_budget_limit is None",
        budget is None,
        f"got {budget!r}",
    )

    active = config.get("platforms.active")
    _check(
        "8. List access — platforms.active == ['facebook','instagram','gbp']",
        active == ["facebook", "instagram", "gbp"],
        f"got {active!r}",
    )

    templates = config.get("creatomate.equipment_post_image.templates")
    _check(
        "9. Dict access — equipment_post_image.templates is a dict with 5 keys",
        isinstance(templates, dict) and len(templates) == 5,
        f"got {type(templates).__name__} with "
        f"{len(templates) if isinstance(templates, dict) else 'N/A'} keys",
    )

    _expect_raises(
        "10. Missing key raises KeyError — config.get('nonexistent.path')",
        KeyError,
        lambda: config.get("nonexistent.path"),
    )

    fallback = config.get("nonexistent.path", default="fallback")
    _check(
        "11. Missing key with default — returns 'fallback'",
        fallback == "fallback",
        f"got {fallback!r}",
    )

    none_default = config.get("nonexistent.path", default=None)
    _check(
        "12. Missing key with default=None — returns None (no raise)",
        none_default is None,
        f"got {none_default!r}",
    )

    _check(
        "13a. Contains check — 'business.name' in config is True",
        "business.name" in config,
    )
    _check(
        "13b. Contains check — 'fake.path' in config is False",
        "fake.path" not in config,
    )

    raw = config.raw
    expected_keys = {
        "business", "owner", "contact", "platforms", "publishing",
        "approval", "catalog", "strategy", "brand_visuals", "drive",
        "skills", "creatomate", "apis", "metrics", "health", "workflows",
    }
    missing = expected_keys - set(raw.keys() if isinstance(raw, dict) else [])
    _check(
        "14. Raw access — config.raw contains all expected top-level keys",
        isinstance(raw, dict) and not missing,
        f"missing: {missing}" if missing else f"got {type(raw).__name__}",
    )

    _expect_raises(
        "15. File not found — load_config('nonexistent_file.yaml') raises",
        FileNotFoundError,
        lambda: load_config("nonexistent_file.yaml"),
    )

    skills = config.get("skills")
    bad_skills = [
        k for k, v in skills.items()
        if not isinstance(v, str) or not v.strip()
    ]
    _check(
        "16. All skill IDs are non-empty strings",
        not bad_skills,
        f"bad entries: {bad_skills}",
    )

    workflows = config.get("workflows")
    bad_workflows = [
        k for k, v in workflows.items()
        if not isinstance(v, str) or not v.strip()
    ]
    _check(
        "17. All workflow IDs are non-empty strings",
        not bad_workflows,
        f"bad entries: {bad_workflows}",
    )

    total = _PASSED + len(_FAILURES)
    print()
    print(f"Results: {_PASSED}/{total} passed")
    if _FAILURES:
        print()
        print("Failures:")
        for fname, detail in _FAILURES:
            print(f"  - {fname}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run_tests())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
