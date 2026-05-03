"""Supply-chain / shipping news signal source.

In production this module subscribes to RSS / news feeds (Port advisories,
trade news, supplier alerts), parses them, and emits per-item or
per-vendor-or-per-region risk records. For the MVP demo it emits a
small set of synthetic advisories tied to specific SKUs and warehouses
so the inventory context bundle has visible non-ERP context to surface.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone


SYNTHETIC_ADVISORIES = [
    {
        "entity_kind": "item",
        "entity_key": "SKU-200",
        "summary": "Port of Long Beach 5/3 advisory — 3-5 day delay on display-panel imports",
        "risk_score": 0.62,
        "risk_band": "medium",
        "horizon_days": 7,
    },
    {
        "entity_kind": "item",
        "entity_key": "SKU-441",
        "summary": "Suzhou PCB cluster rolling brown-outs — capacity down 15% through 5/15",
        "risk_score": 0.51,
        "risk_band": "medium",
        "horizon_days": 14,
    },
    {
        "entity_kind": "warehouse",
        "entity_key": "WH03",
        "summary": "Rotterdam customs system maintenance 5/8 — same-day clearances paused",
        "risk_score": 0.40,
        "risk_band": "low",
        "horizon_days": 3,
    },
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _expires(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")


def seed(conn: sqlite3.Connection) -> int:
    inserted = 0
    for row in SYNTHETIC_ADVISORIES:
        payload = {
            "summary": row["summary"],
            "risk_score": row["risk_score"],
            "risk_band": row["risk_band"],
            "horizon_days": row["horizon_days"],
        }
        conn.execute(
            "INSERT INTO EXTERNAL_SIGNALS "
            "(SOURCE, ENTITY_KIND, ENTITY_KEY, AS_OF, EXPIRES_AT, PAYLOAD_JSON, PROVENANCE) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "shipping_news",
                row["entity_kind"],
                row["entity_key"],
                _now(),
                _expires(row["horizon_days"]),
                json.dumps(payload),
                "rss:synthetic-v1",
            ),
        )
        inserted += 1
    return inserted
