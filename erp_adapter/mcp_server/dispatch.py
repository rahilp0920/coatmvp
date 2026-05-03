"""Scope-aware tool dispatch.

Every tool call routed through this module is checked against the
calling agent's granted scopes (read from the AGENTS / CAPABILITY_GRANTS
tables that the onboarding flow populates). Calls outside the granted
set return a structured `cap.denied` payload — they do not raise, they
do not silently succeed, and they always write a row to WORKFLOW_OBS so
the audit chain captures the attempt.

This is the runtime side of the Coat Agent Protocol. The onboarding side
(`coat agent onboard`) defines the contract; this side enforces it on
every call.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "erp.db"


# ---------------------------------------------------------------------------
# Tool → required scope mapping
# ---------------------------------------------------------------------------

TOOL_SCOPE_MAP: dict[str, str] = {
    "list_concepts":            "coat:concepts:read",
    "find_item":                "coat:inventory:read",
    "get_stock":                "coat:inventory:read",
    "suggest_source_warehouse": "coat:inventory:read",
    "move_stock":               "coat:inventory:write",
    "post_invoice":             "coat:invoice:post",
    "request_approval":         "coat:invoice:approve",
    "list_pending_invoices":    "coat:invoice:read",
    "submit_feedback":          "coat:patterns:read",  # any read-shape scope
    "get_inventory_context":    "coat:context:inventory:read",
}

# Anonymous / system actor — always allowed. Used by the legacy scripted
# demo and by adapter-internal calls. Real agents always pass an agent_id.
SYSTEM_ACTORS = {"agent", "system", "u_admin", "u_clerk_a", "u_clerk_b",
                 "u_mgr_c", "u_cfo", "user"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scope check
# ---------------------------------------------------------------------------

def _agent_record(conn: sqlite3.Connection, agent_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM AGENTS WHERE AGENT_ID=?", (agent_id,)
    ).fetchone()
    if not row:
        return None
    rec = dict(row)
    rec["MANIFEST"] = json.loads(rec["MANIFEST_JSON"]) if rec["MANIFEST_JSON"] else {}
    return rec


def _granted_scopes(conn: sqlite3.Connection, agent_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT SCOPE FROM CAPABILITY_GRANTS WHERE AGENT_ID=? AND REVOKED_AT IS NULL",
        (agent_id,),
    ).fetchall()
    return [r["SCOPE"] for r in rows]


def check_scope(agent_id: str, tool_name: str) -> dict[str, Any]:
    """Return {allowed, granted, missing_scope, reason, agent_status}.

    A SYSTEM actor (no real agent identity) is allowed unconditionally —
    that preserves the legacy scripted demo path."""
    if not agent_id or agent_id in SYSTEM_ACTORS:
        return {"allowed": True, "agent_status": "system"}

    required = TOOL_SCOPE_MAP.get(tool_name)
    if not required:
        return {
            "allowed": False,
            "missing_scope": "coat:unknown",
            "reason": f"tool {tool_name!r} has no scope mapping registered",
            "agent_status": "unknown_tool",
        }

    with _db() as conn:
        rec = _agent_record(conn, agent_id)
        if rec is None:
            return {
                "allowed": False,
                "missing_scope": required,
                "reason": f"agent {agent_id!r} not registered — run `coat agent onboard`",
                "agent_status": "unregistered",
            }
        if rec["STATUS"] == "revoked":
            return {
                "allowed": False,
                "missing_scope": required,
                "reason": f"agent {agent_id!r} is revoked",
                "agent_status": "revoked",
            }
        granted = _granted_scopes(conn, agent_id)
        if required in granted:
            return {"allowed": True, "agent_status": rec["STATUS"], "granted_scope": required}
        return {
            "allowed": False,
            "missing_scope": required,
            "reason": f"agent {agent_id!r} does not hold {required}",
            "agent_status": rec["STATUS"],
            "granted_scopes": granted,
        }


# ---------------------------------------------------------------------------
# WORKFLOW_OBS rows for denials and successes
# ---------------------------------------------------------------------------

def _log_obs(
    conn: sqlite3.Connection,
    *,
    actor: str,
    tool: str,
    args: dict[str, Any],
    result: dict[str, Any],
    outcome: str,
    audit_id: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO WORKFLOW_OBS
           (TS, ACTOR, TOOL, ARGS_JSON, RESULT_JSON, OUTCOME, FEEDBACK)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            _now_iso(),
            actor,
            tool,
            json.dumps(args, default=str),
            json.dumps({**result, "audit_id": audit_id} if audit_id else result, default=str),
            outcome,
            None,
        ),
    )
    return cur.lastrowid


def _bump_trial_call_count(conn: sqlite3.Connection, agent_id: str) -> None:
    conn.execute(
        "UPDATE AGENTS SET TRIAL_CALLS_USED = TRIAL_CALLS_USED + 1 WHERE AGENT_ID=?",
        (agent_id,),
    )


# ---------------------------------------------------------------------------
# Dispatch wrapper
# ---------------------------------------------------------------------------

def dispatch(
    agent_id: str,
    tool_name: str,
    args: dict[str, Any],
    impl: Callable[..., Any],
) -> dict[str, Any]:
    """Run `impl(**args)` after a scope check. Returns the impl's result, or a
    cap.denied envelope when scope is missing. Either way, an obs row lands.

    The caller is expected to be either the MCP `call_tool` shim or an
    in-process agent like Atlas — both pass their agent identity.
    """
    audit_id = "aud_" + uuid.uuid4().hex
    check = check_scope(agent_id, tool_name)

    if not check.get("allowed"):
        denial = {
            "error": "cap.denied",
            "tool": tool_name,
            "agent_id": agent_id,
            "missing_scope": check.get("missing_scope"),
            "reason": check.get("reason"),
            "advise": (
                f"Run `coat agent grant {agent_id} {check.get('missing_scope')}` "
                "if this scope is appropriate for this agent."
            ),
            "audit_id": audit_id,
        }
        # Log the denial
        with _db() as conn:
            _log_obs(
                conn,
                actor=agent_id or "anonymous",
                tool=tool_name,
                args=args,
                result=denial,
                outcome="DENIED",
                audit_id=audit_id,
            )
        return denial

    # Allowed — run impl, capture result, log success.
    # Thread the agent identity through to the underlying adapter so its own
    # WORKFLOW_OBS row carries the agent_id rather than the default "agent".
    started = time.time()
    impl_args = dict(args)
    impl_args.setdefault("actor", agent_id or "agent")
    try:
        result = impl(**impl_args)
    except TypeError:
        # impl doesn't accept `actor` (e.g., the bundle assembler) — retry
        impl_args.pop("actor", None)
        try:
            result = impl(**impl_args)
        except Exception as e:  # noqa: BLE001
            result = {"error": str(e), "type": type(e).__name__}
            outcome = "ERROR"
        else:
            outcome = "OK"
    except Exception as e:  # noqa: BLE001
        result = {"error": str(e), "type": type(e).__name__}
        outcome = "ERROR"
    else:
        outcome = "OK"

    # Most adapter functions already write their own obs row. Only emit our
    # own row here for the bundle / read-only tools that don't.
    with _db() as conn:
        if outcome == "ERROR" or tool_name in {"get_inventory_context", "list_concepts"}:
            _log_obs(
                conn,
                actor=agent_id or "anonymous",
                tool=tool_name,
                args=args,
                result={
                    "ok": outcome == "OK",
                    "duration_ms": round((time.time() - started) * 1000),
                    "scope": check.get("granted_scope"),
                },
                outcome=outcome,
                audit_id=audit_id,
            )
        if agent_id and agent_id not in SYSTEM_ACTORS:
            _bump_trial_call_count(conn, agent_id)

    # Attach audit_id to the result so it threads through the agent's view
    if isinstance(result, dict):
        result.setdefault("_audit", {"audit_id": audit_id, "scope": check.get("granted_scope")})
    return result
