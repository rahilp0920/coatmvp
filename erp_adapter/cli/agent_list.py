"""`coat agent list` / `coat agent show` / `coat agent revoke`.

Read-only and admin operations against the AGENTS + CAPABILITY_GRANTS
tables. Used by the demo's audit moments and by ongoing administration.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "erp.db"

console = Console()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def list_agents(show_scopes: bool = False) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM AGENTS ORDER BY REGISTERED_AT DESC"
        ).fetchall()

        if not rows:
            console.print("[dim]no agents registered. Use `coat agent onboard` to add one.[/dim]")
            return

        table = Table(title="REGISTERED AGENTS", title_justify="left", box=box.SIMPLE_HEAD)
        table.add_column("AGENT", style="cyan", no_wrap=True)
        table.add_column("STATUS")
        table.add_column("PROVIDER", style="dim")
        table.add_column("MODEL", style="dim")
        table.add_column("TRIAL")
        table.add_column("REGISTERED", style="dim")
        if show_scopes:
            table.add_column("SCOPES")

        for r in rows:
            manifest = json.loads(r["MANIFEST_JSON"]) if r["MANIFEST_JSON"] else {}
            granted = manifest.get("granted_scopes", []) or []
            status = r["STATUS"] or "?"
            status_style = {
                "trial": "yellow",
                "enforced": "green",
                "revoked": "red",
            }.get(status, "default")
            trial = (
                f"{r['TRIAL_CALLS_USED'] or 0}/{r['TRIAL_CALLS_MAX'] or 0}"
                + (f" • exp {r['TRIAL_EXPIRES_AT'][:10]}" if r["TRIAL_EXPIRES_AT"] else "")
            )
            row = [
                r["AGENT_ID"],
                Text(status, style=status_style),
                r["PROVIDER"] or "—",
                r["MODEL"] or "—",
                trial,
                (r["REGISTERED_AT"] or "")[:19],
            ]
            if show_scopes:
                row.append(", ".join(granted) if granted else "—")
            table.add_row(*row)

        console.print(table)
    finally:
        conn.close()


def show_agent(agent_id: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM AGENTS WHERE AGENT_ID=?", (agent_id,)
        ).fetchone()
        if not row:
            console.print(f"[red]No agent with id {agent_id!r}[/red]")
            sys.exit(1)

        manifest = json.loads(row["MANIFEST_JSON"]) if row["MANIFEST_JSON"] else {}

        console.print()
        console.print(Text(f"AGENT — {row['AGENT_ID']}", style="bold"))
        console.print(Text(row["DESCRIPTION"] or "", style="italic"))

        meta = (
            f"status: {row['STATUS']}   "
            f"provider: {row['PROVIDER'] or '—'}   "
            f"model: {row['MODEL'] or '—'}   "
            f"trial: {row['TRIAL_CALLS_USED'] or 0}/{row['TRIAL_CALLS_MAX'] or 0}"
        )
        console.print(Text(meta, style="dim"))

        granted = manifest.get("granted_scopes", []) or []
        denied = manifest.get("denied_scopes", []) or []
        bundles = manifest.get("bundles", []) or []

        console.print()
        console.print(Text("Granted scopes", style="bold green"))
        for s in granted:
            console.print(f"  ✓ {s}")
        if not granted:
            console.print("  [dim]none[/dim]")

        if bundles:
            console.print()
            console.print(Text("Bundles", style="bold cyan"))
            for b in bundles:
                console.print(f"  ⊕ {b}")

        if denied:
            console.print()
            console.print(Text("Denied", style="bold red"))
            for d in denied:
                console.print(f"  ✗ {d['scope']}     [dim]{d['reason']}[/dim]")

        # Capability grants log
        grants = conn.execute(
            "SELECT * FROM CAPABILITY_GRANTS WHERE AGENT_ID=? ORDER BY GRANTED_AT",
            (agent_id,),
        ).fetchall()
        if grants:
            console.print()
            console.print(Text("Capability grant log", style="bold"))
            for g in grants:
                state = "active" if g["REVOKED_AT"] is None else f"revoked {g['REVOKED_AT'][:19]}"
                console.print(
                    f"  • {g['SCOPE']:48s} origin={g['ORIGIN']:14s} "
                    f"granted={g['GRANTED_AT'][:19]}  {state}"
                )

        console.print()
    finally:
        conn.close()


def revoke_agent(agent_id: str, reason: str = "manual revoke") -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            "UPDATE AGENTS SET STATUS='revoked' WHERE AGENT_ID=?", (agent_id,)
        )
        if cur.rowcount == 0:
            console.print(f"[red]No agent with id {agent_id!r}[/red]")
            sys.exit(1)
        conn.execute(
            "UPDATE CAPABILITY_GRANTS SET REVOKED_AT=? "
            "WHERE AGENT_ID=? AND REVOKED_AT IS NULL",
            (_now_iso(), agent_id),
        )
        conn.commit()
        console.print(
            Panel.fit(
                f"[bold]{agent_id}[/bold] revoked. All capability grants closed.\n"
                f"[dim]reason: {reason}[/dim]",
                border_style="red",
            )
        )
    finally:
        conn.close()
