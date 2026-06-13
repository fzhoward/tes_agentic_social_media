"""Deploy the orchestration workflows to n8n (cloud) via the public REST API.

Replaces the Make.com scenarios documented in docs/orchestration_contract.md.
Each scenario is a single Schedule trigger -> HTTP Request (POST to the executor
with bearer auth). This tool is idempotent: it upserts one n8n credential
(httpHeaderAuth holding the EXECUTOR_TOKEN) and one workflow per scenario,
matching by name so re-runs update rather than duplicate.

SAFETY: workflows are created/updated INACTIVE. Nothing fires until you
activate it explicitly (one at a time, during cutover) with --activate.

Reads from .env (never committed):
    N8N_BASE_URL        e.g. https://yourname.app.n8n.cloud   (no trailing /api)
    N8N_API_KEY         n8n Settings -> n8n API -> Create API Key
    EXECUTOR_BASE_URL   e.g. https://executor.tessys.org      (the Make target)
    EXECUTOR_TOKEN      shared secret the executor's /run/* endpoints require

Usage:
    python -m tools.n8n_deploy deploy            # upsert credential + all workflows (INACTIVE)
    python -m tools.n8n_deploy list              # show workflows + active state
    python -m tools.n8n_deploy activate --name "Scenario 1 - Indexer"
    python -m tools.n8n_deploy deactivate --name "Scenario 1 - Indexer"

The credential id created on first deploy is cached in .tmp/n8n_deploy_state.json
so re-runs reuse it (the n8n API never returns credential data once stored).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / ".tmp" / "n8n_deploy_state.json"

CREDENTIAL_NAME = "Executor Bearer Token"

# --- Workflow specs: one per Make scenario being migrated --------------------
# schedule_cron times are LOCAL to the workflow timezone (set below).
TIMEZONE = "America/New_York"

WORKFLOWS = [
    {
        "name": "Scenario 1 - Indexer - Daily Asset Index",
        "path": "/run/indexer",
        "cron": "0 2 * * *",          # daily 02:00
        "note": "Daily asset index",
    },
    {
        "name": "Scenario 2 - Strategist",
        "path": "/run/strategist",
        "cron": "0 3 * * 0",          # weekly Sunday 03:00
        "note": "Weekly 7-day content plan (async 202)",
    },
    {
        "name": "Scenario 3 - Drafter Cycle",
        "path": "/run/draft-cycle",
        "cron": "0 4 * * *",          # daily 04:00
        "note": "Server-side draft->critic->redraft loop (async 202)",
    },
    {
        "name": "Scenario 5 - Approval Card Poster",
        "path": "/run/approval-card",
        "cron": "*/30 * * * *",       # every 30 min
        "note": "Post approval cards for awaiting_approval rows",
    },
    {
        "name": "Scenario 7 - Missed Approval Rescheduler",
        "path": "/run/approval-card-reschedule",
        "cron": "0 1 * * *",          # daily 01:00 (recommended)
        "note": "Reschedule overdue awaiting_approval rows +24h / auto-reject",
    },
]


# --- env + api helpers -------------------------------------------------------
def _env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        sys.exit(f"ERROR: {name} not set in .env")
    return val


def _api_base() -> str:
    return _env("N8N_BASE_URL").rstrip("/") + "/api/v1"


def _headers() -> dict:
    return {
        "X-N8N-API-KEY": _env("N8N_API_KEY"),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _request(method: str, path: str, body: dict | None = None) -> dict:
    url = _api_base() + path
    resp = requests.request(method, url, headers=_headers(),
                            json=body, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(
            f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:500]}"
        )
    return resp.json() if resp.text else {}


def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


# --- credential --------------------------------------------------------------
def ensure_credential() -> dict:
    """Create the httpHeaderAuth credential holding the executor bearer token.

    Cached in state so re-runs reuse the same credential id (the API never
    returns credential data after creation, so we cannot look it up by content).
    """
    state = _load_state()
    if state.get("credential_id"):
        print(f"  credential: reusing id={state['credential_id']} "
              f"({CREDENTIAL_NAME})")
        return {"id": state["credential_id"], "name": CREDENTIAL_NAME}

    token = _env("EXECUTOR_TOKEN")
    # n8n.cloud requires header-auth credentials to be scoped to specific
    # domains so the bearer token can't be sent to arbitrary hosts. Scope it
    # to the executor's host (derived from EXECUTOR_BASE_URL).
    host = _env("EXECUTOR_BASE_URL").split("://", 1)[-1].split("/", 1)[0]
    created = _request("POST", "/credentials", {
        "name": CREDENTIAL_NAME,
        "type": "httpHeaderAuth",
        "data": {
            "name": "Authorization",
            "value": f"Bearer {token}",
            "allowedHttpRequestDomains": "domains",
            "allowedDomains": host,
        },
    })
    cred_id = created.get("id")
    state["credential_id"] = cred_id
    _save_state(state)
    print(f"  credential: created id={cred_id} ({CREDENTIAL_NAME})")
    return {"id": cred_id, "name": CREDENTIAL_NAME}


# --- workflow construction ---------------------------------------------------
def _build_workflow(spec: dict, executor_base: str, cred: dict) -> dict:
    url = executor_base.rstrip("/") + spec["path"]
    schedule_node = {
        "id": "schedule",
        "name": "Schedule",
        "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1.2,
        "position": [260, 300],
        "parameters": {
            "rule": {
                "interval": [
                    {"field": "cronExpression", "expression": spec["cron"]}
                ]
            }
        },
    }
    http_node = {
        "id": "http",
        "name": "POST executor",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [520, 300],
        "parameters": {
            "method": "POST",
            "url": url,
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            # The executor's async endpoints answer 202; treat any 2xx as ok and
            # never fail the run on a non-2xx body we don't read.
            "options": {"response": {"response": {"neverError": False}}},
        },
        "credentials": {
            "httpHeaderAuth": {"id": cred["id"], "name": cred["name"]}
        },
    }
    return {
        "name": spec["name"],
        "nodes": [schedule_node, http_node],
        "connections": {
            "Schedule": {
                "main": [[{"node": "POST executor", "type": "main", "index": 0}]]
            }
        },
        "settings": {"timezone": TIMEZONE, "executionOrder": "v1"},
    }


def _list_workflows() -> list[dict]:
    out: list[dict] = []
    cursor = None
    while True:
        path = "/workflows?limit=100" + (f"&cursor={cursor}" if cursor else "")
        page = _request("GET", path)
        out.extend(page.get("data", []))
        cursor = page.get("nextCursor")
        if not cursor:
            break
    return out


# --- commands ----------------------------------------------------------------
def cmd_deploy() -> None:
    executor_base = _env("EXECUTOR_BASE_URL")
    print(f"n8n: {_env('N8N_BASE_URL')}")
    print(f"executor: {executor_base}")
    cred = ensure_credential()

    existing = {w["name"]: w for w in _list_workflows()}
    for spec in WORKFLOWS:
        wf = _build_workflow(spec, executor_base, cred)
        if spec["name"] in existing:
            wid = existing[spec["name"]]["id"]
            _request("PUT", f"/workflows/{wid}", wf)
            print(f"  updated  [{wid}] {spec['name']}  ({spec['cron']} {TIMEZONE}) -> {spec['path']}")
        else:
            created = _request("POST", "/workflows", wf)
            print(f"  created  [{created.get('id')}] {spec['name']}  ({spec['cron']} {TIMEZONE}) -> {spec['path']}")
    print("\nAll workflows deployed INACTIVE. Activate one at a time during "
          "cutover:\n  python -m tools.n8n_deploy activate --name \"<name>\"")


def cmd_list() -> None:
    for w in _list_workflows():
        state = "ACTIVE " if w.get("active") else "inactive"
        print(f"  [{state}] {w['id']}  {w['name']}")


def _find_by_name(name: str) -> dict:
    matches = [w for w in _list_workflows() if name.lower() in w["name"].lower()]
    if not matches:
        sys.exit(f"No workflow matching {name!r}")
    if len(matches) > 1:
        names = ", ".join(w["name"] for w in matches)
        sys.exit(f"Ambiguous {name!r} matches: {names}")
    return matches[0]


def cmd_activate(name: str) -> None:
    w = _find_by_name(name)
    _request("POST", f"/workflows/{w['id']}/activate")
    print(f"  ACTIVATED [{w['id']}] {w['name']}")


def cmd_deactivate(name: str) -> None:
    w = _find_by_name(name)
    _request("POST", f"/workflows/{w['id']}/deactivate")
    print(f"  deactivated [{w['id']}] {w['name']}")


def main() -> None:
    p = argparse.ArgumentParser(description="Deploy orchestration workflows to n8n")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("deploy", help="upsert credential + all workflows (INACTIVE)")
    sub.add_parser("list", help="list workflows + active state")
    a = sub.add_parser("activate", help="activate one workflow by name")
    a.add_argument("--name", required=True)
    d = sub.add_parser("deactivate", help="deactivate one workflow by name")
    d.add_argument("--name", required=True)
    args = p.parse_args()

    if args.cmd == "deploy":
        cmd_deploy()
    elif args.cmd == "list":
        cmd_list()
    elif args.cmd == "activate":
        cmd_activate(args.name)
    elif args.cmd == "deactivate":
        cmd_deactivate(args.name)


if __name__ == "__main__":
    main()
