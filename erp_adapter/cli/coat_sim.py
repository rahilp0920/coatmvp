"""coat sim — inject simulated activity for live demos.

Used during recordings to show that Coat is a *living* layer: an
employee posts something in the ERP, a manager corrects a routing
call, a third-party agent feeds in external signal — and the next
agent run reflects it without any re-deployment.

Subcommands:

  coat sim activity  simulate an employee working in the ERP (the change-
                     boundary beat — the primary 'living layer' demo)
  coat sim feedback  attach a human correction to a prior observation;
                     learner re-mines and surfaces a new pattern
  coat sim news      inject an EXTERNAL_SIGNALS row — represents what
                     ANOTHER agent (news/weather/sanctions monitor) would
                     write to Coat over MCP. Useful as a secondary beat.

All three write to WORKFLOW_OBS so `coat watch` lights up in real
time. The next `atlas` call (or any bundle-consuming agent) sees the
new state on its next bundle assembly. No restart, no re-init.
"""
from __future__ import annotations

import json
import random
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


def activity(
    *,
    sku: str,
    qty: float,
    warehouse: str = "WH02",
    repeat: int = 30,
    over_hours: float = 8.0,
    actor: str = "u_clerk_a",
    kind: str = "consume",
) -> None:
    """Simulate an employee working in the ERP — the change-boundary beat.

    Posts `repeat` consumption events (BWART=261) for `sku` at
    `warehouse`, each consuming `qty` units, with timestamps spread
    over the last `over_hours` hours. Each event:

      • Writes an MSEG row (the SAP material doc)
      • Decrements BIN_DETAIL.QTY (FIFO across OK bins) and WH_STOCK.LABST
      • Writes a WORKFLOW_OBS row (so `coat watch` lights up in real time)

    After this runs, Atlas's next call to `get_inventory_context` sees
    the drained available stock AND the bumped recent-movement velocity
    — exactly the change-boundary architecture from OBSERVABILITY.md
    playing out for the demo. No external commands, no agent restart,
    no re-deploy. Coat just observed and refined.
    """
    if kind != "consume":
        console.print(f"[red]activity --kind only supports 'consume' for now (got {kind!r})[/red]")
        sys.exit(1)
    if repeat < 1:
        console.print("[red]--repeat must be ≥ 1[/red]")
        sys.exit(1)

    base_now = datetime.now(timezone.utc)
    interval_minutes = (over_hours * 60.0) / max(repeat, 1)
    total_qty = qty * repeat

    conn = sqlite3.connect(DB_PATH)
    try:
        # Sanity: where does this item sit?
        wh_row = conn.execute(
            "SELECT LABST FROM WH_STOCK WHERE MATNR=? AND WERKS=?",
            (sku, warehouse),
        ).fetchone()
        if not wh_row:
            console.print(
                f"[red]No WH_STOCK row for {sku} at {warehouse}. Run [bold]coat init[/bold] first.[/red]"
            )
            sys.exit(1)
        starting_avail = float(wh_row[0])

        if total_qty > starting_avail:
            console.print(
                f"[yellow]Heads up: planned consumption ({total_qty:.0f}) > available "
                f"({starting_avail:.0f}). Stock will hit zero before {repeat} events post; "
                f"the rest will land as zero-quantity (visible in MSEG, no further drain).[/yellow]"
            )

        mblnr_seq = 90000 + int(base_now.timestamp()) % 1000
        posted = 0
        zeroed_at: int | None = None

        for i in range(repeat):
            mblnr_seq += 1
            # Stagger timestamps oldest → newest within the window
            ts = (base_now - timedelta(minutes=interval_minutes * (repeat - 1 - i))).isoformat(timespec="seconds")

            # Refresh available, decide actual qty for this event
            cur = conn.execute(
                "SELECT LABST FROM WH_STOCK WHERE MATNR=? AND WERKS=?", (sku, warehouse)
            ).fetchone()
            available_now = float(cur[0]) if cur else 0.0
            this_qty = min(qty, available_now)
            if this_qty <= 0 and zeroed_at is None:
                zeroed_at = i

            # Decrement bins FIFO from any OK bin with stock
            if this_qty > 0:
                bin_row = conn.execute(
                    """
                    SELECT LGORT, BIN_CODE, QTY FROM BIN_DETAIL
                    WHERE MATNR=? AND WERKS=? AND Z_STATUS='OK' AND QTY > 0
                    ORDER BY QTY DESC LIMIT 1
                    """,
                    (sku, warehouse),
                ).fetchone()
                if bin_row:
                    take = min(this_qty, float(bin_row[2]))
                    conn.execute(
                        """
                        UPDATE BIN_DETAIL SET QTY = QTY - ?
                        WHERE MATNR=? AND WERKS=? AND LGORT=? AND BIN_CODE=?
                        """,
                        (take, sku, warehouse, bin_row[0], bin_row[1]),
                    )
                conn.execute(
                    "UPDATE WH_STOCK SET LABST = LABST - ? WHERE MATNR=? AND WERKS=?",
                    (this_qty, sku, warehouse),
                )

            # MSEG row — BWART=261 is consumption / issue-to-cost-center
            conn.execute(
                """
                INSERT INTO MSEG
                    (MBLNR, ZEILE, BWART, MATNR, WERKS_FROM, WERKS_TO,
                     LGORT_FROM, LGORT_TO, MENGE, POSTED_BY, POSTED_AT)
                VALUES (?, 1, '261', ?, ?, NULL, 'MAIN', NULL, ?, ?, ?)
                """,
                (f"DOC{mblnr_seq}", sku, warehouse, this_qty, actor, ts),
            )

            # ~40% of consumes are tied to a sales-order reservation that
            # gets fulfilled. Write the reservation row so Coat sees the
            # Z_RESERVED table being touched in the same operator workflow
            # — that's evidence the inferred 'reservation' concept is real.
            if this_qty > 0 and random.random() < 0.40:
                conn.execute(
                    """
                    INSERT INTO Z_RESERVED
                        (MATNR, WERKS, QTY, REF_DOC, CREATED_AT, EXPIRES_AT)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (sku, warehouse, this_qty,
                     f"SO-FULFILLED-{mblnr_seq}", ts, ts),
                )

            # WORKFLOW_OBS — the change-boundary event Coat observes
            _log_obs(
                conn,
                actor=actor,
                tool="consume_stock",
                args={"sku": sku, "qty": this_qty, "warehouse": warehouse,
                      "movement_type": "261"},
                result={"doc": f"DOC{mblnr_seq}",
                        "remaining_available": max(0.0, available_now - this_qty)},
                outcome="OK",
            )
            posted += 1
        conn.commit()

        ending = conn.execute(
            "SELECT LABST FROM WH_STOCK WHERE MATNR=? AND WERKS=?", (sku, warehouse)
        ).fetchone()[0]
    finally:
        conn.close()

    drained = starting_avail - float(ending)
    console.print(
        Panel.fit(
            Text.assemble(
                Text("✓ employee activity simulated\n", style="bold green"),
                Text(f"  who   : ", style="dim"), Text(actor, style="bold"),
                Text(f"\n  what  : ", style="dim"),
                Text(f"{posted} consume events on {sku} at {warehouse}", style="default"),
                Text(f"\n  span  : ", style="dim"),
                Text(f"last {over_hours:.0f} hours (BWART=261)", style="default"),
                Text(f"\n  stock : ", style="dim"),
                Text(f"{starting_avail:.0f} → {float(ending):.0f}  (−{drained:.0f})",
                     style="bold cyan"),
                (Text(f"\n  note  : stock zeroed at event #{zeroed_at}", style="yellow")
                 if zeroed_at is not None else Text("")),
                Text(
                    "\n\n  Coat saw every event on the change boundary "
                    "(check the watch pane).\n  Re-run [bold]atlas[/bold] — the bundle now "
                    "reflects drained stock + bumped velocity.",
                    style="dim",
                ),
            ),
            title="Coat — change-boundary observation",
            border_style="green",
        )
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
