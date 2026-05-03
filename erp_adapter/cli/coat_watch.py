"""`coat watch` — the Coat supervisor.

This is Coat's main operation. It runs continuously, observing the
ERP's change boundary, surfacing every employee action and every agent
call into a single live view, and **automatically refining the concept
catalog** when enough evidence has accumulated. The customer's normal
day-to-day looks like: `coat watch` is on; everything else just
benefits.

Three responsibilities:

  1. Tail WORKFLOW_OBS in real time so the operator/admin can see
     what's happening (the visible side of the supervisor).
  2. Track recent observation volume + elapsed time since last refine,
     and invoke `coat_refine.refine()` automatically when either
     threshold is crossed. The refine result is logged back to
     WORKFLOW_OBS so the audit chain is complete.
  3. Render the refine event with extra emphasis in the live panel —
     when Coat's understanding of the schema sharpens, the operator
     sees it land in real time.

Defaults:
  --refine-on-obs 20    auto-refine after 20 fresh observations
  --refine-every 24h    auto-refine at least every 24 hours
  --poll 0.6            poll the obs log every 600 ms

Stop with Ctrl-C.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "erp.db"

console = Console()


# Refine-trigger defaults — overridable from the CLI.
DEFAULT_REFINE_ON_OBS = 20
DEFAULT_REFINE_INTERVAL_HOURS = 24.0


def _outcome_style(outcome: str) -> str:
    return {
        "OK": "green",
        "DENIED": "bold red",
        "ERROR": "red",
        "FEEDBACK": "yellow",
        "SCOPE_REQUEST": "bold yellow",
        "REFINED": "bold magenta",
    }.get(outcome.upper() if outcome else "", "default")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log_refine_event(conn: sqlite3.Connection, *, trigger: str, result: dict[str, Any]) -> None:
    """Write an auto-refine event to WORKFLOW_OBS so the audit chain
    captures every confidence update with provenance."""
    summary = {
        "bumped":     result.get("bumped", 0),
        "total_obs":  result.get("total_obs", 0),
        "concepts":   [c["name"] for c in result.get("concepts") or []],
    }
    conn.execute(
        """INSERT INTO WORKFLOW_OBS
           (TS, ACTOR, TOOL, ARGS_JSON, RESULT_JSON, OUTCOME, FEEDBACK)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (_now_iso(), "coat", "auto_refine",
         json.dumps({"trigger": trigger}, default=str),
         json.dumps(summary, default=str),
         "REFINED", None),
    )
    conn.commit()


def _short(value: object, max_len: int = 60) -> str:
    s = str(value)
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _render_panel(
    rows: list[sqlite3.Row],
    grants: list[sqlite3.Row],
    *,
    obs_since_refine: int,
    refine_threshold: int,
    seconds_to_next_periodic: float,
    last_refine_summary: str | None,
) -> Panel:
    progress = f"{obs_since_refine}/{refine_threshold}"
    next_periodic = ""
    if seconds_to_next_periodic > 0 and seconds_to_next_periodic < 1e9:
        if seconds_to_next_periodic >= 3600:
            next_periodic = f" • next periodic in {seconds_to_next_periodic/3600:.1f}h"
        else:
            next_periodic = f" • next periodic in {seconds_to_next_periodic/60:.0f}m"
    title = Text.assemble(
        Text("coat watch  ", style="bold"),
        Text("• supervisor on", style="dim"),
        Text(f"  •  refine in {progress} obs", style="dim"),
        Text(next_periodic, style="dim"),
        (Text(f"\n   {last_refine_summary}", style="bold magenta")
         if last_refine_summary else Text("")),
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
        elif outcome.upper() == "REFINED":
            bumped = result.get("bumped", 0)
            total_obs = result.get("total_obs", 0)
            concepts = result.get("concepts") or []
            preview = ", ".join(concepts[:3])
            if len(concepts) > 3:
                preview += f", +{len(concepts) - 3}"
            detail = f"refined {bumped} concept(s) from {total_obs} obs · {preview}"
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


def watch(
    poll_seconds: float = 0.6,
    refine_on_obs: int = DEFAULT_REFINE_ON_OBS,
    refine_every_hours: float = DEFAULT_REFINE_INTERVAL_HOURS,
) -> None:
    if not DB_PATH.exists():
        console.print(
            f"[red]No database at {DB_PATH}. Run [bold]coat init[/bold] first.[/red]"
        )
        return

    # Lazy import — avoids circular dep when refine imports its own helpers.
    from cli.coat_refine import refine as run_refine

    last_obs_id = 0
    last_grant_id = 0
    obs_buffer: list[sqlite3.Row] = []
    grants_buffer: list[sqlite3.Row] = []

    obs_since_refine = 0
    last_refine_at = datetime.now(timezone.utc)
    last_refine_summary: str | None = None

    def _maybe_refine(reason: str) -> None:
        nonlocal obs_since_refine, last_refine_at, last_refine_summary
        # Window covers the time since the last refine (with a 5-minute floor)
        elapsed = max(timedelta(minutes=5),
                      datetime.now(timezone.utc) - last_refine_at)
        elapsed_h = max(1, int(elapsed.total_seconds() / 3600) or 1)
        result = run_refine(window=f"{elapsed_h}h", silent=True)
        bumped = result.get("bumped", 0)
        if bumped > 0:
            last_refine_summary = (
                f"auto-refine ({reason}) — "
                f"{bumped} concept(s) sharpened from {result.get('total_obs', 0)} obs"
            )
            conn = sqlite3.connect(DB_PATH)
            try:
                _log_refine_event(conn, trigger=reason, result=result)
            finally:
                conn.close()
        else:
            last_refine_summary = (
                f"auto-refine ({reason}) — no concept reached threshold; "
                "Coat is steady"
            )
        obs_since_refine = 0
        last_refine_at = datetime.now(timezone.utc)

    def _refresh() -> Panel:
        nonlocal last_obs_id, last_grant_id, obs_since_refine
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
                # Count observations excluding our own auto-refine events,
                # otherwise we'd retrigger ourselves on the next tick.
                obs_since_refine += sum(
                    1 for r in new
                    if (r["TOOL"] or "") != "auto_refine"
                )

            try:
                new_grants = conn.execute(
                    "SELECT * FROM CAPABILITY_GRANTS WHERE GRANT_ID > ? ORDER BY GRANT_ID",
                    (last_grant_id,),
                ).fetchall()
            except sqlite3.OperationalError:
                new_grants = []
            if new_grants:
                last_grant_id = new_grants[-1]["GRANT_ID"]
                grants_buffer.extend(new_grants)
        finally:
            conn.close()

        # Refine triggers
        elapsed_since_refine = (datetime.now(timezone.utc) - last_refine_at).total_seconds()
        if obs_since_refine >= refine_on_obs:
            _maybe_refine("obs threshold")
        elif elapsed_since_refine >= refine_every_hours * 3600:
            _maybe_refine(f"{int(refine_every_hours)}h interval")

        seconds_to_next_periodic = max(
            0.0, refine_every_hours * 3600 - elapsed_since_refine,
        )
        return _render_panel(
            obs_buffer, grants_buffer,
            obs_since_refine=obs_since_refine,
            refine_threshold=refine_on_obs,
            seconds_to_next_periodic=seconds_to_next_periodic,
            last_refine_summary=last_refine_summary,
        )

    console.print(
        Panel.fit(
            Text.assemble(
                Text("coat watch — supervisor on\n", style="bold"),
                Text(
                    f"  refines automatically: every {refine_on_obs} fresh obs "
                    f"OR every {refine_every_hours:g}h.\n  Ctrl-C to stop.",
                    style="dim",
                ),
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
