"""`coat sim` — inject simulated activity for live demos.

Used during recordings to show that Coat is a *living* layer: a piece
of news lands, an employee logs an action, a manager corrects a
routing call — and the next agent run reflects it without any
re-deployment.

Subcommands:

  coat sim news     inject an EXTERNAL_SIGNALS row (news / weather / sanctions)
  coat sim feedback attach human feedback to a prior observation; learner re-mines

Both write a row to WORKFLOW_OBS so the watch pane lights up in real
time. The next `atlas` (or any bundle-consuming agent) call sees the
new state on its next bundle assembly. No restart, no re-init.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "erp.db"

console = Console()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _expires_iso(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")


def _log_obs(conn: sqlite3.Connection, *, actor: str, tool: str,
             args: dict[str, Any], result: dict[str, Any], outcome: str = "OK") -> None:
    conn.execute(
        """INSERT INTO WORKFLOW_OBS
           (TS, ACTOR, TOOL, ARGS_JSON, RESULT_JSON, OUTCOME, FEEDBACK)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (_now_iso(), actor, tool,
         json.dumps(args, default=str),
         json.dumps(result, default=str),
         outcome, None),
    )


def _render_news_panel(*, source: str, entity_kind: str, entity_key: str,
                       risk_band: str, risk_score: float, summary: str) -> None:
    band_color = {"high": "bold red", "medium": "yellow", "low": "green"}.get(risk_band, "default")
    console.print(
        Panel.fit(
            Text.assemble(
                Text("✓ external signal ingested\n", style="bold green"),
                Text(f"  source : ", style="dim"), Text(source, style="bold"),
                Text(f"\n  entity : ", style="dim"),
                Text(f"{entity_kind}={entity_key}", style="bold cyan"),
                Text(f"\n  risk   : ", style="dim"),
                Text(f"{risk_band} ({risk_score:.2f})", style=band_color),
                Text(f"\n  summary: ", style="dim"), Text(summary, style="default"),
                Text(
                    f"\n\n  Atlas's next call will reflect this — re-run "
                    f"[bold]atlas[/bold] (or [bold]atlas --scripted[/bold]) "
                    f"to see the forecast shift.",
                    style="dim",
                ),
            ),
            title="Coat — real-time context update",
            border_style="green",
        )
    )


def news(
    *,
    sku: str | None = None,
    warehouse: str | None = None,
    summary: str,
    risk_band: str = "medium",
    risk_score: float = 0.5,
    horizon_days: int = 7,
    source: str = "shipping_news",
) -> None:
    """Inject an external news/weather/sanctions signal targeting a SKU
    or warehouse. Writes to EXTERNAL_SIGNALS + WORKFLOW_OBS."""
    if not (sku or warehouse):
        console.print("[red]Provide --sku or --warehouse[/red]")
        sys.exit(1)
    if sku and warehouse:
        console.print("[red]Pick one of --sku or --warehouse, not both[/red]")
        sys.exit(1)
    if risk_band not in {"low", "medium", "high"}:
        console.print(f"[red]--risk must be low/medium/high (got {risk_band!r})[/red]")
        sys.exit(1)

    entity_kind = "item" if sku else "warehouse"
    entity_key = sku or warehouse  # type: ignore[assignment]

    payload = {
        "summary": summary,
        "risk_band": risk_band,
        "risk_score": risk_score,
        "horizon_days": horizon_days,
    }
    provenance = f"{source}:simulated:{uuid.uuid4().hex[:8]}"

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """INSERT INTO EXTERNAL_SIGNALS
               (SOURCE, ENTITY_KIND, ENTITY_KEY, AS_OF, EXPIRES_AT, PAYLOAD_JSON, PROVENANCE)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (source, entity_kind, entity_key, _now_iso(),
             _expires_iso(horizon_days), json.dumps(payload), provenance),
        )
        # Light up the watch pane with an explicit ingest event
        _log_obs(
            conn,
            actor="world",
            tool="external_signal_ingest",
            args={
                "source": source,
                "entity_kind": entity_kind,
                "entity_key": entity_key,
                "risk_band": risk_band,
            },
            result={"summary": summary, "provenance": provenance},
            outcome="OK",
        )
        conn.commit()
    finally:
        conn.close()

    _render_news_panel(
        source=source, entity_kind=entity_kind, entity_key=entity_key,
        risk_band=risk_band, risk_score=risk_score, summary=summary,
    )


def feedback(*, obs_id: int, note: str, actor: str = "u_mgr_c") -> None:
    """Attach human feedback to a prior observation. The learner re-mines
    automatically (per adapter._log_obs's FEEDBACK trigger)."""
    # Use the adapter directly so we get the same code path the MCP tool
    # would use — feedback triggers a re-mine inside _log_obs.
    sys.path.insert(0, str(ROOT))
    from mcp_server import adapter
    result = adapter.submit_feedback(obs_id=obs_id, feedback=note, actor=actor)
    if result.get("ok"):
        console.print(
            Panel.fit(
                Text.assemble(
                    Text("✓ feedback recorded; learner re-mining\n", style="bold green"),
                    Text(f"  obs_id: {obs_id}\n", style="dim"),
                    Text(f"  note  : {note}\n", style="dim"),
                    Text(f"  actor : {actor}\n", style="dim"),
                    Text(
                        "\n  Run [bold]coat audit --entity <id>[/bold] or re-run [bold]atlas[/bold] "
                        "to see the new pattern surface.",
                        style="dim",
                    ),
                ),
                title="Coat — human correction",
                border_style="green",
            )
        )
    else:
        console.print(f"[red]feedback failed: {result}[/red]")
