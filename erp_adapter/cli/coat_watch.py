"""`coat watch` — live tail of WORKFLOW_OBS + agent activity.

Run this in a side pane during the demo. Every adapter call, every
denied attempt, every grant, every learner tick streams here in real
time. It's the ambient presence of Coat — the customer (or the
recording) sees Coat is on, watching, without having to ask.

Streaming uses rich's Live display, polling the SQLite WAL every
~600ms. Stop with Ctrl-C.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "erp.db"

console = Console()


def _outcome_style(outcome: str) -> str:
    return {
        "OK": "green",
        "DENIED": "bold red",
        "ERROR": "red",
        "FEEDBACK": "yellow",
        "SCOPE_REQUEST": "bold yellow",
    }.get(outcome.upper() if outcome else "", "default")


def _short(value: object, max_len: int = 60) -> str:
    s = str(value)
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _render_panel(rows: list[sqlite3.Row], grants: list[sqlite3.Row]) -> Panel:
    title = Text.assemble(
        Text("coat watch  ", style="bold"),
        Text("• live tail of WORKFLOW_OBS + capability_grants", style="dim"),
    )

    obs_table = Table(box=None, show_header=True, header_style="bold dim", padding=(0, 1))
    obs_table.add_column("when", style="dim", no_wrap=True)
    obs_table.add_column("actor", style="cyan", no_wrap=True)
    obs_table.add_column("tool", no_wrap=True)
    obs_table.add_column("outcome", no_wrap=True)
    obs_table.add_column("detail", overflow="fold")

    for r in rows[-15:]:
        outcome = r["OUTCOME"] or "?"
        result = json.loads(r["RESULT_JSON"]) if r["RESULT_JSON"] else {}
        if outcome.upper() == "DENIED":
            detail = f"missing: {result.get('missing_scope', '?')}"
        elif outcome.upper() == "FEEDBACK":
            detail = _short(r["FEEDBACK"])
        else:
            audit = (result.get("_audit") or {}) if isinstance(result, dict) else {}
            scope = audit.get("scope")
            doc = result.get("doc") or result.get("belnr") or result.get("count")
            detail = " ".join(
                p for p in [
                    f"scope={scope}" if scope else "",
                    f"doc={doc}" if doc else "",
                ]
                if p
            ) or "✓"
        obs_table.add_row(
            (r["TS"] or "")[11:19],
            r["ACTOR"] or "—",
            r["TOOL"] or "—",
            Text(outcome, style=_outcome_style(outcome)),
            _short(detail, 70),
        )

    body_parts: list = [obs_table]

    if grants:
        body_parts.append(Text("\nrecent capability grants", style="bold dim"))
        gtable = Table(box=None, show_header=True, header_style="bold dim", padding=(0, 1))
        gtable.add_column("when", style="dim", no_wrap=True)
        gtable.add_column("agent", style="cyan", no_wrap=True)
        gtable.add_column("scope", no_wrap=True)
        gtable.add_column("origin", style="dim", no_wrap=True)
        gtable.add_column("by", style="dim", no_wrap=True)
        for g in grants[-6:]:
            gtable.add_row(
                (g["GRANTED_AT"] or "")[11:19],
                g["AGENT_ID"],
                g["SCOPE"],
                g["ORIGIN"],
                g["GRANTED_BY"] or "—",
            )
        body_parts.append(gtable)

    from rich.console import Group
    body = Group(*body_parts)
    return Panel(body, title=title, title_align="left", border_style="cyan")


def watch(poll_seconds: float = 0.6) -> None:
    if not DB_PATH.exists():
        console.print(
            f"[red]No database at {DB_PATH}. Run [bold]coat init[/bold] first.[/red]"
        )
        return

    last_obs_id = 0
    last_grant_id = 0
    obs_buffer: list[sqlite3.Row] = []
    grants_buffer: list[sqlite3.Row] = []

    def _refresh() -> Panel:
        nonlocal last_obs_id, last_grant_id
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            new = conn.execute(
                "SELECT * FROM WORKFLOW_OBS WHERE OBS_ID > ? ORDER BY OBS_ID",
                (last_obs_id,),
            ).fetchall()
            if new:
                last_obs_id = new[-1]["OBS_ID"]
                obs_buffer.extend(new)

            try:
                new_grants = conn.execute(
                    "SELECT * FROM CAPABILITY_GRANTS WHERE GRANT_ID > ? ORDER BY GRANT_ID",
                    (last_grant_id,),
                ).fetchall()
            except sqlite3.OperationalError:
                # CAPABILITY_GRANTS not yet present
                new_grants = []
            if new_grants:
                last_grant_id = new_grants[-1]["GRANT_ID"]
                grants_buffer.extend(new_grants)
        finally:
            conn.close()
        return _render_panel(obs_buffer, grants_buffer)

    console.print(
        Panel.fit(
            Text.assemble(
                Text("coat watch", style="bold"),
                Text(" — Ctrl-C to stop", style="dim"),
            ),
            border_style="cyan",
        )
    )

    try:
        with Live(_refresh(), console=console, refresh_per_second=2, screen=False) as live:
            while True:
                time.sleep(poll_seconds)
                live.update(_refresh())
    except KeyboardInterrupt:
        console.print("\n[dim]coat watch stopped.[/dim]")


if __name__ == "__main__":
    watch()
