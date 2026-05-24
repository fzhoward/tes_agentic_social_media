"""Tests for tools/skill_loader.py.

Runs against real skill files and workflow SOPs in the TES Rentals Drive.
Prerequisite: Run `python tools/google_auth.py` first to generate the token.

Run from the project root:
    python -m pytest tests/test_skill_loader.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CONFIG_FILE = PROJECT_ROOT / "business_config_tes_rentals.yaml"
os.environ.setdefault("BUSINESS_CONFIG_PATH", str(CONFIG_FILE))

from tools import skill_loader  # noqa: E402
from tools.config_loader import load_config  # noqa: E402


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    skill_loader.clear_cache()
    yield
    skill_loader.clear_cache()


def _unresolved_known_tokens(text: str) -> list[str]:
    """Return any PLACEHOLDER_MAP tokens still present as {{TOKEN}} in text."""
    return [t for t in skill_loader.PLACEHOLDER_MAP if f"{{{{{t}}}}}" in text]


def test_load_skill_hook_creation(config):
    text = skill_loader.load_skill("hook_creation", config=config)
    assert isinstance(text, str) and len(text) > 0
    # If hook_creation contains {{BUSINESS_NAME}}, it must have been resolved.
    assert "{{BUSINESS_NAME}}" not in text


def test_load_skill_brand_voice(config):
    # brand_voice is a Google Doc — drive_helpers exports it as plain text.
    text = skill_loader.load_skill("brand_voice", config=config)
    assert isinstance(text, str) and len(text) > 0


def test_load_skill_image_prompt_universal(config):
    text = skill_loader.load_skill("image_prompt_universal", config=config)
    assert isinstance(text, str) and len(text) > 0

    # {{BUSINESS_NAME}} appears in this file and must be resolved.
    assert "T.E.S. Rentals" in text, (
        "expected {{BUSINESS_NAME}} to resolve to 'T.E.S. Rentals' in output"
    )
    assert "{{BUSINESS_NAME}}" not in text

    # {{SERVICE_AREA}} is left here as a contractual check: if the live file
    # ever uses it, this assertion guarantees it's resolved. (As of 2026-05-24
    # the live file uses different token names — see drift note in memory.)
    assert "{{SERVICE_AREA}}" not in text

    # No token from PLACEHOLDER_MAP should remain unresolved in the output.
    leftover = _unresolved_known_tokens(text)
    assert not leftover, f"known placeholders still unresolved: {leftover}"


def test_load_workflow_drafter(config):
    text = skill_loader.load_workflow("drafter", config=config)
    assert isinstance(text, str) and len(text) > 0


def test_placeholder_injection_all_tokens(config):
    parts = [f"{token}={{{{{token}}}}}" for token in skill_loader.PLACEHOLDER_MAP]
    synthetic = "; ".join(parts)

    resolved = skill_loader._inject_placeholders(synthetic, config)

    for token in skill_loader.PLACEHOLDER_MAP:
        assert f"{{{{{token}}}}}" not in resolved, (
            f"token {{{{{token}}}}} was not replaced"
        )
        expected_value = str(config.get(skill_loader.PLACEHOLDER_MAP[token]))
        assert expected_value, f"config value for {token} is empty"
        assert expected_value in resolved, (
            f"expected value {expected_value!r} for {token} not found in output"
        )

    # No {{...}} pattern of a known token should remain. (Unknown tokens would
    # not be present in this synthetic input.)
    import re
    leftover = re.findall(r"\{\{[A-Z][A-Z0-9_]*\}\}", resolved)
    assert not leftover, f"unexpected unresolved placeholders: {leftover}"


def test_unknown_placeholder_preserved(config, capsys):
    text = "Hello {{UNKNOWN_TOKEN}}, meet {{BUSINESS_NAME}}"
    resolved = skill_loader._inject_placeholders(text, config)

    assert "{{UNKNOWN_TOKEN}}" in resolved
    assert "{{BUSINESS_NAME}}" not in resolved
    assert "T.E.S. Rentals" in resolved

    captured = capsys.readouterr()
    assert "UNKNOWN_TOKEN" in captured.err
    assert "Unknown placeholder" in captured.err


def test_caching(config):
    first = skill_loader.load_skill("hook_creation", config=config)
    second = skill_loader.load_skill("hook_creation", config=config)
    assert first is second or first == second
    assert "skill:hook_creation" in skill_loader._cache_snapshot()

    skill_loader.clear_cache()
    assert skill_loader._cache_snapshot() == {}

    third = skill_loader.load_skill("hook_creation", config=config)
    assert third == first
    assert "skill:hook_creation" in skill_loader._cache_snapshot()


def test_missing_skill_key(config):
    with pytest.raises(KeyError):
        skill_loader.load_skill("nonexistent_skill_name", config=config)


def test_load_workflow_vs_skill_no_collision(config):
    # 'strategy_guidance' exists under skills.* but not workflows.* in the
    # TES config. Verify the cache namespaces skills and workflows separately,
    # and that a name collision (if one ever existed) wouldn't cross-pollute.
    skill_text = skill_loader.load_skill("strategy_guidance", config=config)
    assert isinstance(skill_text, str) and len(skill_text) > 0

    snapshot = skill_loader._cache_snapshot()
    assert "skill:strategy_guidance" in snapshot
    assert "workflow:strategy_guidance" not in snapshot

    # Loading a workflow by the same name must hit the workflows.* config path,
    # which doesn't exist here — proving the two namespaces are independent.
    with pytest.raises(KeyError):
        skill_loader.load_workflow("strategy_guidance", config=config)

    # The failed workflow load must not have polluted the skill cache entry.
    assert skill_loader._cache_snapshot().get("skill:strategy_guidance") == skill_text


def test_numeric_placeholder_conversion(config):
    text = (
        "posts={{POSTS_PER_WEEK_PER_PLATFORM}} "
        "lead={{LEAD_TIME_HOURS}} "
        "gap={{MIN_GAP_HOURS}} "
        "queue={{MAX_QUEUE_DEPTH}}"
    )
    resolved = skill_loader._inject_placeholders(text, config)

    assert "posts=7" in resolved
    assert "lead=36" in resolved
    assert "gap=4" in resolved
    assert "queue=7" in resolved

    for token in (
        "POSTS_PER_WEEK_PER_PLATFORM",
        "LEAD_TIME_HOURS",
        "MIN_GAP_HOURS",
        "MAX_QUEUE_DEPTH",
    ):
        assert f"{{{{{token}}}}}" not in resolved
