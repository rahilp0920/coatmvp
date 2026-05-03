"""Weather + climate signal source.

In production this module would call Open-Meteo (or NOAA, or a vendor
API) on schedule and write rolling forecasts into EXTERNAL_SIGNALS. For
the MVP demo it emits synthetic-but-realistic forecasts keyed by
warehouse region — the bundle assembler joins these into the inventory
context bundle so Atlas never has to know what an API key is.

Each row carries a demand-modifier interpretation so a downstream agent
can use it as a single number instead of having to reason about
temperatures.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone


# Per-warehouse regional forecasts. Keyed by warehouse code (matches T001W.WERKS).
# Synthetic for the demo — the shape matches what a real Open-Meteo aggregation
# would emit after region-binding.
SYNTHETIC_FORECASTS = [
    {
        "werks": "WH01",
        "region": "US-East",
        "summary": "Heat dome forecast 5/9-5/12 — peaks 38C in Northeast metro",
        "anomaly": "high_temp",
        "demand_modifier": 1.18,                   # +18% demand vs. baseline
        "demand_modifier_reason": "HVAC + cooling components run hot during heat events",
        "confidence": 0.74,
    },
    {
        "werks": "WH02",
        "region": "US-West",
        "summary": "Mild week ahead, no anomalies; baseline conditions",
        "anomaly": None,
        "demand_modifier": 1.00,
        "demand_modifier_reason": "no weather-driven demand uplift",
        "confidence": 0.92,
    },
    {
        "werks": "WH03",
        "region": "EU-NL",
        "summary": "Storm Karin landfall 5/8 — port disruption likely 24-48h",
        "anomaly": "storm",
        "demand_modifier": 0.94,
        "demand_modifier_reason": "logistics-side disruption suppresses near-term ship-to demand",
        "confidence": 0.68,
    },
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _expires(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")


def seed(conn: sqlite3.Connection) -> int:
    """Insert one signal per warehouse region. Returns count inserted."""
    inserted = 0
    for row in SYNTHETIC_FORECASTS:
        payload = {
            "summary": row["summary"],
            "anomaly": row["anomaly"],
            "demand_modifier": row["demand_modifier"],
            "demand_modifier_reason": row["demand_modifier_reason"],
            "confidence": row["confidence"],
            "region": row["region"],
        }
        conn.execute(
            "INSERT INTO EXTERNAL_SIGNALS "
            "(SOURCE, ENTITY_KIND, ENTITY_KEY, AS_OF, EXPIRES_AT, PAYLOAD_JSON, PROVENANCE) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "weather",
                "warehouse",
                row["werks"],
                _now(),
                _expires(7),
                json.dumps(payload),
                "open-meteo:synthetic-v1",
            ),
        )
        inserted += 1
    return inserted
