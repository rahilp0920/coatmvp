"""`coat agent grant` — grant a scope to an existing agent (audit-tracked).

Used in two ways:
  1. From the demo runbook scene 5: when Atlas hits cap.denied, the admin
     runs `coat agent grant atlas@coat.io/v1 coat:procurement:write` to
     ratify the new capability mid-flight.
  2. From admin operations: any time a manifest needs to expand or
     contract for a registered agent.

This is the runtime correlate of the manifest-derivation flow. The
manifest derivation (`coat agent onboard`) sets the initial scope set
deterministically. `coat agent grant` adds to it, with explicit human
sign-off.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "erp.db"

console = Console()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def grant(
    agent_id: str,
    scope: str,
    granted_by: str = "u_mgr_c",
    reason: str | None = None,
    auto_yes: bool = False,
) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        agent = conn.execute(
            "SELECT * FROM AGENTS WHERE AGENT_ID=?", (agent_id,)
        ).fetchone()
        if not agent:
            console.print(f"[red]No such agent: {agent_id!r}[/red]")
            sys.exit(1)
        if agent["STATUS"] == "revoked":
            console.print(
                f"[red]{agent_id!r} is revoked — re-register before granting.[/red]"
            )
            sys.exit(1)

        manifest = json.loads(agent["MANIFEST_JSON"]) if agent["MANIFEST_JSON"] else {}
        granted = list(manifest.get("granted_scopes") or [])
        if scope in granted:
            console.print(
                f"[dim]{agent_id} already holds {scope}. No change.[/dim]"
            )
            return

        # Clear from denied list if present (this scope is now granted)
        denied = list(manifest.get("denied_scopes") or [])
        denied_other = [d for d in denied if d.get("scope") != scope]
        was_denied = len(denied) != len(denied_other)

        # Confirmation surface — the runbook calls this "scope expansion request"
        console.print()
        console.print(
            Panel.fit(
                Text.assemble(
                    Text("scope-expansion request\n", style="bold yellow"),
                    Text(f"  agent:   ", style="dim"), Text(agent_id, style="bold"),
                    Text("\n  scope:   ", style="dim"), Text(scope, style="bold cyan"),
                    Text("\n  granter: ", style="dim"), Text(granted_by),
                    Text(("\n  was previously denied" if was_denied else ""), style="dim"),
                    Text(("\n  reason:  " + reason if reason else ""), style="dim"),
                ),
                border_style="yellow",
            )
        )

        if not auto_yes:
            console.print("[bold]Approve grant? [y/n][/bold]")
            try:
                choice = input("> ").strip().lower()
            except EOFError:
                choice = "n"
            if choice not in ("y", "yes"):
                console.print("[yellow]Grant cancelled.[/yellow]")
                return

        granted.append(scope)
        granted = sorted(set(granted))
        manifest["granted_scopes"] = granted
        manifest["denied_scopes"] = denied_other

        conn.execute(
            "UPDATE AGENTS SET MANIFEST_JSON=? WHERE AGENT_ID=?",
            (json.dumps(manifest, default=str), agent_id),
        )
        conn.execute(
            """
            INSERT INTO CAPABILITY_GRANTS
                (AGENT_ID, SCOPE, ORIGIN, GRANTED_AT, GRANTED_BY, NOTE)
            VALUES (?, ?, 'admin', ?, ?, ?)
            """,
            (
                agent_id,
                scope,
                _now_iso(),
                granted_by,
                reason or "scope expansion (admin)",
            ),
        )
        conn.commit()

        console.print(
            Panel.fit(
                Text.assemble(
                    Text("✓ ", style="bold green"),
                    Text(f"granted {scope} to {agent_id}", style="bold"),
                    Text("\n  audit chain: capability_grants row + manifest update", style="dim"),
                    Text(
                        f"\n  the agent will pick this up on its next call",
                        style="dim",
                    ),
                ),
                border_style="green",
            )
        )
    finally:
        conn.close()


def revoke_scope(agent_id: str, scope: str, reason: str | None = None) -> None:
    """Revoke a single scope from an agent (the agent itself stays active)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            """
            UPDATE CAPABILITY_GRANTS
            SET REVOKED_AT=?, NOTE=COALESCE(NOTE, '') || ' | revoked: ' || ?
            WHERE AGENT_ID=? AND SCOPE=? AND REVOKED_AT IS NULL
            """,
            (_now_iso(), reason or "manual", agent_id, scope),
        )
        if cur.rowcount == 0:
            console.print(f"[yellow]{agent_id} did not hold {scope!r} (or already revoked)[/yellow]")
            return

        # Update manifest JSON
        row = conn.execute(
            "SELECT MANIFEST_JSON FROM AGENTS WHERE AGENT_ID=?", (agent_id,)
        ).fetchone()
        if row and row[0]:
            manifest = json.loads(row[0])
            granted = [s for s in (manifest.get("granted_scopes") or []) if s != scope]
            manifest["granted_scopes"] = granted
            denied = list(manifest.get("denied_scopes") or [])
            denied.append({"scope": scope, "reason": "revoked: " + (reason or "manual")})
            manifest["denied_scopes"] = denied
            conn.execute(
                "UPDATE AGENTS SET MANIFEST_JSON=? WHERE AGENT_ID=?",
                (json.dumps(manifest, default=str), agent_id),
            )
        conn.commit()
        console.print(
            f"[red]✗[/red] revoked [bold]{scope}[/bold] from [bold]{agent_id}[/bold]"
        )
    finally:
        conn.close()
