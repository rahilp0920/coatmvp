"""Scope-aware tool dispatch.

Every tool call routed through this module is checked against the
calling agent's granted scopes (read from the AGENTS / CAPABILITY_GRANTS
tables that the onboarding flow populates). Calls outside the granted
set return a structured `cap.denied` payload — they do not raise, they
do not silently succeed, and they always write a row to WORKFLOW_OBS so
the audit chain captures the attempt.

When an interactive admin is sitting at the terminal where the agent is
running, the dispatcher will surface the denial *inline* — printing a
scope-expansion request panel and prompting the admin to approve or
reject. On approve, the missing capability is granted (with full audit),
the call retries automatically, and the agent gets the result it asked
for. The admin never types a scope string.

This is the runtime side of the Coat Agent Protocol. The onboarding side
(`coat agent onboard`) defines the contract; this side enforces it on
every call and offers mid-flight ratification when the contract needs to
expand.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

_console = Console()

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

def _interactive_ratify_enabled() -> bool:
    """The mid-flight ratification UI is on by default in TTY contexts.

    Disable explicitly with COAT_NO_INLINE_RATIFY=1 (e.g., for non-
    interactive scripts or test runs that want to see the raw cap.denied
    envelope). Force on with COAT_FORCE_INLINE_RATIFY=1 (useful when
    stdin is piped but you still want the prompt to fire)."""
    if os.environ.get("COAT_NO_INLINE_RATIFY") == "1":
        return False
    if os.environ.get("COAT_FORCE_INLINE_RATIFY") == "1":
        return True
    return sys.stdin.isatty()


def _surface_request_and_prompt(
    agent_id: str,
    tool_name: str,
    args: dict[str, Any],
    missing_scope: str,
    reason: str,
    audit_id: str,
) -> bool:
    """Print the scope-expansion request panel and prompt admin for y/n.

    Returns True if the admin approved.
    """
    summary_arg = ""
    for k in ("matnr", "vendor", "belnr", "warehouse"):
        if k in args:
            summary_arg = f"{k}={args[k]}"
            break

    _console.print(
        Panel.fit(
            Text.assemble(
                Text("scope-expansion request\n", style="bold yellow"),
                Text(f"  agent  : ", style="dim"), Text(agent_id, style="bold"),
                Text(f"\n  asks   : ", style="dim"), Text(missing_scope, style="bold cyan"),
                Text(f"\n  to do  : ", style="dim"),
                Text(f"{tool_name}({summary_arg})", style="default"),
                Text(f"\n  reason : ", style="dim"), Text(reason, style="default"),
                Text(f"\n  audit  : {audit_id}", style="dim"),
            ),
            title="Coat",
            border_style="yellow",
        )
    )
    _console.print("[bold]Approve this scope for this agent? [y/n][/bold]")
    try:
        answer = input("> ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _grant_inline(
    agent_id: str,
    scope: str,
    *,
    granted_by: str = "u_mgr_c",
    note: str = "inline ratification (mid-flight)",
) -> None:
    """Add a CAPABILITY_GRANTS row + update the agent's manifest in place."""
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO CAPABILITY_GRANTS
                (AGENT_ID, SCOPE, ORIGIN, GRANTED_AT, GRANTED_BY, NOTE)
            VALUES (?, ?, 'admin', ?, ?, ?)
            """,
            (agent_id, scope, _now_iso(), granted_by, note),
        )
        row = conn.execute(
            "SELECT MANIFEST_JSON FROM AGENTS WHERE AGENT_ID=?", (agent_id,)
        ).fetchone()
        if row and row["MANIFEST_JSON"]:
            manifest = json.loads(row["MANIFEST_JSON"])
            granted = list(manifest.get("granted_scopes") or [])
            if scope not in granted:
                granted.append(scope)
                manifest["granted_scopes"] = sorted(granted)
            denied = [d for d in (manifest.get("denied_scopes") or []) if d.get("scope") != scope]
            manifest["denied_scopes"] = denied
            conn.execute(
                "UPDATE AGENTS SET MANIFEST_JSON=? WHERE AGENT_ID=?",
                (json.dumps(manifest, default=str), agent_id),
            )


def dispatch(
    agent_id: str,
    tool_name: str,
    args: dict[str, Any],
    impl: Callable[..., Any],
    *,
    allow_inline_ratify: bool | None = None,
) -> dict[str, Any]:
    """Run `impl(**args)` after a scope check.

    Returns the impl's result, or a cap.denied envelope when scope is
    missing AND mid-flight ratification was unavailable / declined.

    If `allow_inline_ratify` is None (default), the function consults the
    TTY: when an interactive admin is at the terminal, it prints the
    scope-expansion request panel and prompts y/n. On approve, the
    capability is granted (audited) and the call retries automatically.
    """
    audit_id = "aud_" + uuid.uuid4().hex
    check = check_scope(agent_id, tool_name)

    if not check.get("allowed"):
        # Mid-flight ratification path — the agent asks, the admin decides.
        if (allow_inline_ratify if allow_inline_ratify is not None else _interactive_ratify_enabled()) \
                and check.get("missing_scope") and check.get("agent_status") not in ("revoked", "unregistered"):
            approved = _surface_request_and_prompt(
                agent_id=agent_id,
                tool_name=tool_name,
                args=args,
                missing_scope=check["missing_scope"],
                reason=check.get("reason") or "",
                audit_id=audit_id,
            )
            if approved:
                _grant_inline(agent_id, check["missing_scope"])
                _console.print(
                    Panel.fit(
                        Text.assemble(
                            Text("✓ ", style="bold green"),
                            Text(f"granted ", style="default"),
                            Text(check["missing_scope"], style="bold"),
                            Text(f" to {agent_id}", style="default"),
                            Text(f"\n  retrying {tool_name}…", style="dim"),
                        ),
                        border_style="green",
                    )
                )
                # Retry — but disable inline ratify on the recursive call so
                # we don't loop on a different missing scope.
                return dispatch(agent_id, tool_name, args, impl, allow_inline_ratify=False)
            else:
                _console.print(
                    "[yellow]✗ admin declined the scope expansion. Returning cap.denied.[/yellow]"
                )

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
