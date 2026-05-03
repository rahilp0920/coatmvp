"""Inventory context bundle.

Atlas (and any other inventory-domain agent) calls `get_inventory_context`
and gets a single business-shaped payload assembled from:

  - ERP layer:               WH_STOCK, BIN_DETAIL, Z_RESERVED, MAT_MASTER
  - Change boundary:         MSEG (recent movements per item)
  - Learner:                 LEARNED_PATTERNS WHERE KIND='ROUTING'
  - External signals:        EXTERNAL_SIGNALS (weather, shipping_news, …)

The agent never sees those source tables. It receives an
`InventoryContext` payload with a mandatory `context_origin` provenance
list so every input is auditable.

This file contains only data composition. No LLM calls inside.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = ROOT / "data" / "erp.db"


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Sub-queries
# ---------------------------------------------------------------------------

def _top_items_by_velocity(conn: sqlite3.Connection, top_n: int, lookback_days: int) -> list[dict[str, Any]]:
    """Items ranked by absolute movement volume in the last `lookback_days`."""
    rows = conn.execute(
        """
        SELECT m.MATNR AS sku,
               COALESCE(SUM(ABS(m.MENGE)), 0) AS volume,
               mm.MAKTX AS name,
               mm.Z_FRAGILE AS fragile,
               mm.Z_HAZMAT AS hazmat
        FROM MSEG m
        LEFT JOIN MAT_MASTER mm ON mm.MATNR = m.MATNR
        WHERE m.POSTED_AT >= datetime('now', ?)
        GROUP BY m.MATNR
        ORDER BY volume DESC
        LIMIT ?
        """,
        (f"-{lookback_days} days", top_n),
    ).fetchall()
    if rows:
        return [dict(r) for r in rows]
    # Fallback: if no MSEG history (e.g., fresh DB), pick top items by stock
    rows = conn.execute(
        """
        SELECT s.MATNR AS sku, SUM(s.LABST) AS volume,
               mm.MAKTX AS name, mm.Z_FRAGILE AS fragile, mm.Z_HAZMAT AS hazmat
        FROM WH_STOCK s LEFT JOIN MAT_MASTER mm ON mm.MATNR = s.MATNR
        GROUP BY s.MATNR ORDER BY volume DESC LIMIT ?
        """,
        (top_n,),
    ).fetchall()
    return [dict(r) for r in rows]


def _stock_breakdown(conn: sqlite3.Connection, sku: str) -> tuple[dict[str, float], dict[str, float], list[str]]:
    """Returns (on_hand_by_warehouse, available_after_reservations, warehouses_touching)."""
    on_hand: dict[str, float] = {}
    available: dict[str, float] = {}
    for r in conn.execute(
        "SELECT WERKS AS wh, LABST AS on_hand FROM WH_STOCK WHERE MATNR=?",
        (sku,),
    ).fetchall():
        on_hand[r["wh"]] = float(r["on_hand"] or 0)
        # Subtract active reservations for true available
        reserved = conn.execute(
            """
            SELECT COALESCE(SUM(QTY), 0) FROM Z_RESERVED
            WHERE MATNR=? AND WERKS=? AND (EXPIRES_AT IS NULL OR EXPIRES_AT > ?)
            """,
            (sku, r["wh"], _now_iso()),
        ).fetchone()[0]
        available[r["wh"]] = float(r["on_hand"] or 0) - float(reserved or 0)
    return on_hand, available, list(on_hand.keys())


def _movement_summary(conn: sqlite3.Connection, sku: str, lookback_days: int) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT POSTED_AT AS ts, MENGE AS qty, BWART AS bwart
        FROM MSEG WHERE MATNR=? AND POSTED_AT >= datetime('now', ?)
        ORDER BY POSTED_AT
        """,
        (sku, f"-{lookback_days} days"),
    ).fetchall()
    if not rows:
        return {"movement_count": 0, "out_avg_per_day": 0.0, "trend": "no_data"}
    total = sum(abs(float(r["qty"] or 0)) for r in rows)
    out_avg = total / max(1, lookback_days)
    # Trend: compare first half vs second half
    half = len(rows) // 2
    first_total = sum(abs(float(r["qty"] or 0)) for r in rows[:half]) if half else 0
    second_total = sum(abs(float(r["qty"] or 0)) for r in rows[half:]) if half else total
    if second_total > first_total * 1.15:
        trend = "rising"
    elif second_total < first_total * 0.85:
        trend = "falling"
    else:
        trend = "stable"
    return {
        "movement_count": len(rows),
        "out_avg_per_day": round(out_avg, 2),
        "trend": trend,
    }


def _learned_routing_for_item(conn: sqlite3.Connection, sku: str, fragile: bool) -> str | None:
    """Surface the routing pattern most relevant to this item, or None."""
    rows = conn.execute(
        "SELECT KEY, VALUE_JSON, SUPPORT, CONFIDENCE FROM LEARNED_PATTERNS WHERE KIND='ROUTING'"
    ).fetchall()
    candidates = []
    for r in rows:
        key = r["KEY"] or ""
        val = json.loads(r["VALUE_JSON"]) if r["VALUE_JSON"] else {}
        if fragile and "fragile" in key:
            candidates.append((key, val, r["SUPPORT"], r["CONFIDENCE"]))
        elif (not fragile) and "nonfragile" in key:
            candidates.append((key, val, r["SUPPORT"], r["CONFIDENCE"]))
    if not candidates:
        return None
    key, val, support, conf = candidates[0]
    wh = val.get("warehouse")
    if not wh:
        return None
    return f"{key}: prefer {wh} (support={support}, confidence={conf:.2f})"


_RISK_RANK = {"low": 1, "medium": 2, "high": 3}


def _bump_risk(out: dict[str, Any], band: str | None, score: float | None) -> None:
    """MAX-aggregate risk band/score across multiple signals so a new
    high-severity event always wins over a stale low-severity one."""
    if band:
        existing = (out.get("supply_chain_risk_band") or "").lower()
        if _RISK_RANK.get(band.lower(), 0) > _RISK_RANK.get(existing, 0):
            out["supply_chain_risk_band"] = band.lower()
    if score is not None:
        out["supply_chain_risk_score"] = max(
            float(out.get("supply_chain_risk_score") or 0.0),
            float(score),
        )


def _external_signals_for_item(conn: sqlite3.Connection, sku: str, warehouses: list[str]) -> dict[str, Any]:
    """Aggregate every relevant external signal into a compact payload.

    Joins on (ENTITY_KIND='item', ENTITY_KEY=sku) plus
    (ENTITY_KIND='warehouse', ENTITY_KEY in warehouses), filtering out
    expired signals. Risk band/score are MAX-aggregated across signals
    so a fresh HIGH-risk event dominates a stale LOW-risk one.
    """
    out: dict[str, Any] = {}
    sources_used: list[str] = []

    # Item-level signals (e.g., shipping_news targeted at a SKU)
    for r in conn.execute(
        """
        SELECT SOURCE, PAYLOAD_JSON FROM EXTERNAL_SIGNALS
        WHERE ENTITY_KIND='item' AND ENTITY_KEY=?
          AND (EXPIRES_AT IS NULL OR EXPIRES_AT > ?)
        """,
        (sku, _now_iso()),
    ).fetchall():
        payload = json.loads(r["PAYLOAD_JSON"])
        if r["SOURCE"] == "shipping_news":
            _bump_risk(out, payload.get("risk_band"), payload.get("risk_score"))
            out.setdefault("news_summary", []).append(payload.get("summary"))
        sources_used.append(r["SOURCE"])

    # Warehouse-level signals — average per-warehouse demand modifier where
    # this item is stocked. (For simplicity the modifier is just the mean.)
    if warehouses:
        modifiers: list[float] = []
        for wh in warehouses:
            for r in conn.execute(
                """
                SELECT SOURCE, PAYLOAD_JSON FROM EXTERNAL_SIGNALS
                WHERE ENTITY_KIND='warehouse' AND ENTITY_KEY=?
                  AND (EXPIRES_AT IS NULL OR EXPIRES_AT > ?)
                """,
                (wh, _now_iso()),
            ).fetchall():
                payload = json.loads(r["PAYLOAD_JSON"])
                if r["SOURCE"] == "weather":
                    mod = payload.get("demand_modifier")
                    if mod is not None:
                        modifiers.append(float(mod))
                    if payload.get("anomaly"):
                        out.setdefault("weather_summary", []).append(
                            f"{wh}: {payload.get('summary')}"
                        )
                if r["SOURCE"] == "shipping_news":
                    _bump_risk(out, payload.get("risk_band"), payload.get("risk_score"))
                    out.setdefault("news_summary", []).append(
                        f"{wh}: {payload.get('summary')}"
                    )
                sources_used.append(r["SOURCE"])
        if modifiers:
            out["weather_demand_modifier"] = round(sum(modifiers) / len(modifiers), 3)

    if "news_summary" in out:
        # Deduplicate while preserving order, then collapse to a string
        seen = []
        for s in out["news_summary"]:
            if s and s not in seen:
                seen.append(s)
        out["news_summary"] = " | ".join(seen) if seen else None
    if "weather_summary" in out:
        seen = []
        for s in out["weather_summary"]:
            if s and s not in seen:
                seen.append(s)
        out["weather_summary"] = " | ".join(seen) if seen else None

    return {"signals": out, "sources_used": sorted(set(sources_used))}


# ---------------------------------------------------------------------------
# Top-level assembler
# ---------------------------------------------------------------------------

def get_inventory_context(
    window_days: int = 7,
    top_n: int = 10,
    lookback_days: int = 60,
) -> dict[str, Any]:
    """Assemble an InventoryContext bundle.

    Args:
        window_days: forecast window the agent should reason over.
        top_n: number of items to include (highest recent velocity first).
        lookback_days: how far back movement history goes.

    Returns a dict with `as_of`, `window`, `items[]`, `context_origin`.
    """
    items_out: list[dict[str, Any]] = []
    sources_seen: set[str] = set()

    with _db() as conn:
        ranked = _top_items_by_velocity(conn, top_n=top_n, lookback_days=lookback_days)
        for it in ranked:
            sku = it["sku"]
            on_hand, available, warehouses = _stock_breakdown(conn, sku)
            movement = _movement_summary(conn, sku, lookback_days)
            routing = _learned_routing_for_item(conn, sku, fragile=bool(it.get("fragile")))
            ext = _external_signals_for_item(conn, sku, warehouses)
            sources_seen.update(ext.get("sources_used", []))

            items_out.append({
                "sku": sku,
                "name": it.get("name"),
                "fragile": bool(it.get("fragile")),
                "hazmat": bool(it.get("hazmat")),
                "on_hand_by_warehouse": on_hand,
                "available_after_reservations": available,
                "movement_last_n_days": {
                    "lookback_days": lookback_days,
                    **movement,
                },
                "learned_routing": routing,
                "external_signals": ext.get("signals", {}),
            })

    context_origin = [
        "ERP: WH_STOCK + BIN_DETAIL + Z_RESERVED + MAT_MASTER",
        f"Change boundary: MSEG, last {lookback_days}d",
        "Learner: enforced ROUTING patterns from LEARNED_PATTERNS",
    ]
    if sources_seen:
        context_origin.append(
            "External: " + ", ".join(sorted(sources_seen))
        )
    else:
        context_origin.append("External: (none — no live signals matched)")

    return {
        "as_of": _now_iso(),
        "window": f"{window_days}d",
        "items": items_out,
        "context_origin": context_origin,
    }
