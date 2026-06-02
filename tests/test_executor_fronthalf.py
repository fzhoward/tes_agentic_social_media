"""Tests for the front-half executor endpoints in tools/executor.py.

Uses Flask's test client. The runner helper (`_run_cli`) and `threading.Thread`
are patched so no real subprocess spawns and the async draft-cycle endpoint does
not actually launch the loop. Covers bearer auth, the sync indexer/strategist
wrapping, the async 202 draft-cycle dispatch + its longer timeout, and a
regression guard that /run/approval-card still behaves after the runner helper
grew a `timeout` parameter.

Run from the project root:
    python -m pytest tests/test_executor_fronthalf.py -v
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

from tools import executor  # noqa: E402


_TOKEN = "test-executor-token"


@pytest.fixture
def client(monkeypatch):
    """Flask test client with a known EXECUTOR_TOKEN."""
    monkeypatch.setenv("EXECUTOR_TOKEN", _TOKEN)
    executor.app.config["TESTING"] = True
    return executor.app.test_client()


def _auth() -> dict:
    return {"Authorization": f"Bearer {_TOKEN}"}


# --- Auth ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "path", ["/run/indexer", "/run/strategist", "/run/draft-cycle"]
)
def test_endpoints_require_bearer(client, path) -> None:
    """1. Each front-half endpoint returns 401 without a valid bearer token."""
    # No header at all.
    assert client.post(path).status_code == 401
    # Wrong token.
    resp = client.post(path, headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


# --- Sync indexer / strategist ------------------------------------------------

@pytest.mark.parametrize(
    "path,module",
    [
        ("/run/indexer", "agents.asset_indexer"),
        ("/run/strategist", "agents.strategist"),
    ],
)
def test_sync_endpoint_success(client, monkeypatch, path, module) -> None:
    """2a. Success runner result -> 200 with wrapped {exit_code, result}."""
    calls = []

    def fake_run_cli(args, timeout=executor._SUBPROCESS_TIMEOUT):
        calls.append((args, timeout))
        return 0, {"status": "success"}

    monkeypatch.setattr(executor, "_run_cli", fake_run_cli)
    resp = client.post(path, headers=_auth())

    assert resp.status_code == 200
    assert resp.get_json() == {"exit_code": 0, "result": {"status": "success"}}
    assert calls[0][0] == [module]


@pytest.mark.parametrize("path", ["/run/indexer", "/run/strategist"])
def test_sync_endpoint_nonzero_is_500(client, monkeypatch, path) -> None:
    """2b. Non-zero runner exit -> 500 (still wrapped)."""
    monkeypatch.setattr(
        executor, "_run_cli",
        lambda args, timeout=executor._SUBPROCESS_TIMEOUT: (
            1, {"status": "error", "error": "boom"}
        ),
    )
    resp = client.post(path, headers=_auth())
    assert resp.status_code == 500
    assert resp.get_json()["exit_code"] == 1


# --- Async draft-cycle (202) --------------------------------------------------

def test_draft_cycle_is_async_202(client, monkeypatch) -> None:
    """3a. draft-cycle returns 202 {accepted:true} and spawns a bg thread."""
    started = {}

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None, **kw):
            started["target"] = target
            started["daemon"] = daemon
            started["started"] = False

        def start(self):
            started["started"] = True

    monkeypatch.setattr(executor.threading, "Thread", FakeThread)
    # Guard: the runner must NOT be invoked synchronously in the request path.
    monkeypatch.setattr(
        executor, "_run_cli",
        lambda *a, **k: pytest.fail("draft-cycle must not run synchronously"),
    )

    resp = client.post("/run/draft-cycle", headers=_auth())

    assert resp.status_code == 202
    assert resp.get_json() == {"accepted": True}
    # A background thread targeting the draft-cycle worker was started.
    assert started["target"] is executor._dispatch_draft_cycle
    assert started["started"] is True
    assert started["daemon"] is True


def test_draft_cycle_worker_uses_longer_timeout(monkeypatch) -> None:
    """3b. The draft-cycle worker passes the draft-cycle cap, not the 120s one."""
    recorded = {}

    def fake_run_cli(args, timeout=executor._SUBPROCESS_TIMEOUT):
        recorded["args"] = args
        recorded["timeout"] = timeout
        return 0, {"status": "success"}

    monkeypatch.setattr(executor, "_run_cli", fake_run_cli)
    executor._dispatch_draft_cycle()

    assert recorded["args"] == ["agents.draft_cycle"]
    assert recorded["timeout"] == executor._DRAFT_CYCLE_TIMEOUT
    assert recorded["timeout"] != executor._SUBPROCESS_TIMEOUT


# --- Regression: existing endpoint untouched ----------------------------------

def test_approval_card_still_works(client, monkeypatch) -> None:
    """4. /run/approval-card unchanged after _run_cli grew a timeout param."""
    # 401 without auth.
    assert client.post("/run/approval-card").status_code == 401

    calls = []

    def fake_run_cli(args, timeout=executor._SUBPROCESS_TIMEOUT):
        calls.append((args, timeout))
        return 0, {"status": "success"}

    monkeypatch.setattr(executor, "_run_cli", fake_run_cli)
    resp = client.post("/run/approval-card", headers=_auth())

    assert resp.status_code == 200
    assert resp.get_json() == {"exit_code": 0, "result": {"status": "success"}}
    # Still the default sync timeout (no draft-cycle cap leaked in).
    assert calls[0][0] == ["tools.approval_card"]
    assert calls[0][1] == executor._SUBPROCESS_TIMEOUT
