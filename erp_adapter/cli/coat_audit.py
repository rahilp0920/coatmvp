"""`coat audit --entity` — entity timeline view.

Closes scene 6 of the demo runbook. Renders every event Coat saw or did
on a given business entity (item, vendor, invoice, agent), in
chronological order, with the capability that authorized each action.

Provenance is the point. Every action chains back through the
capability that authorized it → the manifest or pattern that derived
the capability → the human who ratified it.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "erp.db"

console = Console()


def _parse_since(spec: str) -> datetime:
    """Accept '1h', '24h', '7d', '60m'. Returns a UTC datetime cutoff."""
    m = re.match(r"^\s*(\d+)\s*([hdmw])\s*$", spec or "", re.IGNORECASE)
    if not m:
        # default to 24h
        return datetime.now(timezone.utc) - timedelta(hours=24)
    n, unit = int(m.group(1)), m.group(2).lower()
    delta = {
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
        "w": timedelta(weeks=n),
    }[unit]
    return datetime.now(timezone.utc) - delta


def _matches_entity(args_json: str | None, result_json: str | None, entity: str) -> bool:
    if entity is None:
        return True
    if args_json and entity in args_json:
        return True
    if result_json and entity in result_json:
        return True
    return False


def audit_entity(entity: str, since: str = "24h") -> None:
    cutoff = _parse_since(since).isoformat(timespec="seconds")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT * FROM WORKFLOW_OBS
            WHERE TS >= ?
            ORDER BY OBS_ID
            """,
            (cutoff,),
        ).fetchall()

        # Filter by entity match (in args or result)
        events = [
            r for r in rows
            if _matches_entity(r["ARGS_JSON"], r["RESULT_JSON"], entity)
        ]

        if not events:
            console.print(
                f"[dim]No events matching entity {entity!r} since {since}.[/dim]"
            )
            return

        title = Text.assemble(
            Text("ENTITY TIMELINE — ", style="bold"),
            Text(entity, style="bold cyan"),
            Text(f"   (since {since})", style="dim"),
        )
        table = Table(title=title, title_justify="left", box=box.SIMPLE_HEAD)
        table.add_column("WHEN", style="dim", no_wrap=True)
        table.add_column("ACTOR", style="cyan", no_wrap=True)
        table.add_column("TOOL", no_wrap=True)
        table.add_column("OUTCOME", no_wrap=True)
        table.add_column("CAPABILITY / NOTE", overflow="fold")

        for r in events:
            outcome = (r["OUTCOME"] or "?").upper()
            color = {
                "OK": "green",
                "DENIED": "bold red",
                "ERROR": "red",
                "FEEDBACK": "yellow",
                "SCOPE_REQUEST": "yellow",
            }.get(outcome, "default")

            args = json.loads(r["ARGS_JSON"]) if r["ARGS_JSON"] else {}
            result = json.loads(r["RESULT_JSON"]) if r["RESULT_JSON"] else {}

            # Build the capability/note column from result/args
            audit = (result.get("_audit") or {}) if isinstance(result, dict) else {}
            cap_line = ""
            if outcome == "DENIED":
                cap_line = f"missing: {result.get('missing_scope', '?')} • reason: {result.get('reason', '?')}"
            elif outcome == "FEEDBACK":
                cap_line = f"feedback: {r['FEEDBACK'][:80] if r['FEEDBACK'] else ''}"
            elif "scope" in audit and audit["scope"]:
                cap_line = f"scope: {audit['scope']}"
            elif outcome == "OK":
                cap_line = "✓"

            ts = (r["TS"] or "")[11:19]
            table.add_row(
                ts,
                r["ACTOR"] or "—",
                r["TOOL"] or "—",
                Text(outcome, style=color),
                cap_line,
            )

        console.print(table)

        # Audit chain summary — show capability_grants relevant to actors in this window
        actors = sorted(set(r["ACTOR"] for r in events if r["ACTOR"]))
        if actors:
            console.print()
            console.print(Text("Capability provenance for actors in this window", style="bold"))
            for actor in actors:
                grants = conn.execute(
                    """
                    SELECT SCOPE, ORIGIN, GRANTED_AT, GRANTED_BY
                    FROM CAPABILITY_GRANTS
                    WHERE AGENT_ID=? AND REVOKED_AT IS NULL
                    ORDER BY GRANTED_AT
                    """,
                    (actor,),
                ).fetchall()
                if not grants:
                    continue
                console.print(f"  [cyan]{actor}[/cyan]")
                for g in grants:
                    console.print(
                        f"    • {g['SCOPE']:48s} origin={g['ORIGIN']:14s} "
                        f"granted {g['GRANTED_AT'][:19]} by {g['GRANTED_BY']}"
                    )
    finally:
        conn.close()
