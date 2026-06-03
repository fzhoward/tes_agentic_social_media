"""Tests for tools/executor.py — the async /run/strategist route.

Native accumulator harness (NOT pytest), mirroring tests/test_strategist.py:
module-level _PASSED / _FAILURES, a non-raising _check, plain test_* functions,
and a run_tests() entrypoint.

This uses Flask's test client, so it requires Flask — which lives in the repo
venv. Run with the venv interpreter:

    .venv/bin/python -m tests.test_executor
    # or
    .venv/bin/python tests/test_executor.py

Chosen approach: Option A (tiny Flask test-client check). It is low-friction —
Flask is already a dependency, app.test_client() needs no server, and
_dispatch_strategist / _check_bearer monkeypatch cleanly (both are looked up as
module globals inside the route, so reassigning them on the module takes effect
at call time). No subprocess runs, no API calls, no Slack posts.
"""

from __future__ import annotations

import sys
import threading
import traceback
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import executor  # noqa: E402


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


def test_strategist_route_returns_202_and_dispatches() -> None:
    # Fake the background dispatch (so no subprocess runs) and force the bearer
    # check to pass. The route spawns a daemon thread targeting
    # _dispatch_strategist; the Event lets us wait for it deterministically.
    recorded: list = []
    fired = threading.Event()
    real_dispatch = executor._dispatch_strategist
    real_bearer = executor._check_bearer

    def _fake_dispatch() -> None:
        recorded.append(True)
        fired.set()

    executor._dispatch_strategist = _fake_dispatch  # type: ignore[assignment]
    executor._check_bearer = lambda: True  # type: ignore[assignment]
    try:
        client = executor.app.test_client()
        resp = client.post("/run/strategist")
        dispatched = fired.wait(timeout=5)
        status = resp.status_code
        body = resp.get_json()
    finally:
        executor._dispatch_strategist = real_dispatch
        executor._check_bearer = real_bearer

    _check(
        "1. /run/strategist — 202 {accepted: true} and dispatch thread invoked",
        status == 202
        and body == {"accepted": True}
        and dispatched
        and recorded == [True],
        f"status={status}, body={body!r}, dispatched={dispatched}, "
        f"recorded={recorded}",
    )


def test_strategist_route_unauthorized() -> None:
    # Bad/missing bearer → 401 and the dispatch must never fire.
    called: list = []
    real_dispatch = executor._dispatch_strategist
    real_bearer = executor._check_bearer
    executor._dispatch_strategist = (
        lambda: called.append(True)  # type: ignore[assignment]
    )
    executor._check_bearer = lambda: False  # type: ignore[assignment]
    try:
        client = executor.app.test_client()
        resp = client.post("/run/strategist")
        status = resp.status_code
        body = resp.get_json()
    finally:
        executor._dispatch_strategist = real_dispatch
        executor._check_bearer = real_bearer

    _check(
        "2. /run/strategist — 401 when bearer check fails, no dispatch",
        status == 401 and called == [] and (body or {}).get("success") is False,
        f"status={status}, body={body!r}, called={called}",
    )


def run_tests() -> int:
    print()
    print("Deterministic tests (no API calls, no subprocess, no Slack):")
    test_strategist_route_returns_202_and_dispatches()
    test_strategist_route_unauthorized()

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
