"""Structural confidence scoring for the discovered concept map.

The semantic mapper proposes (concept -> table -> columns) bindings.
This module verifies each binding against the actual data in the
dossier and emits a deterministic confidence score plus a list of
human-readable evidence strings.

Confidence is structural — it does not ask Claude. It asks the
data:

  - Does the column claimed as `id` actually look like an id (PK
    flag, high cardinality, near-1:1 with row count)?
  - Does the column claimed as `country` carry 2-letter codes?
  - Does the column claimed as `status` look like an enum?
  - Is this concept's table a foreign-key target from many others
    (master) or none (ledger)?

Each signal returns a per-column score in [0, 1]. The concept-level
confidence is the mean of contributing signals, weighted by how
opinionated each signal is. Evidence strings are the human-readable
explanation that accompanies every score.

This file has no LLM dependencies and is safe to call offline.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

# ---------------------------------------------------------------------------
# Per-role shape testers
#
# Each tester takes the column descriptor + stats from the dossier and the
# column's role assignment (e.g. "id", "name", "country") and returns a
# (score, evidence) pair. score ∈ [0,1]; evidence is one short string or None.
# ---------------------------------------------------------------------------

CountryRE = re.compile(r"^[A-Z]{2}$")
CurrencyRE = re.compile(r"^[A-Z]{3}$")
DateRE = re.compile(r"^\d{4}-\d{2}-\d{2}")  # ISO-shaped


def _examples(col: dict[str, Any]) -> list[Any]:
    return col.get("stats", {}).get("examples", []) or []


def _cardinality(col: dict[str, Any]) -> tuple[int, int]:
    s = col.get("stats", {}) or {}
    return s.get("rows", 0), s.get("distinct", 0)


def score_id(col: dict[str, Any]) -> tuple[float, str | None]:
    rows, distinct = _cardinality(col)
    if col.get("pk"):
        return 1.0, f"{col['name']} is PK (declared)"
    if rows > 0 and distinct == rows:
        return 0.95, f"{col['name']} is 1:1 unique across {rows} rows"
    if rows > 0 and distinct >= 0.95 * rows:
        return 0.8, f"{col['name']} is ≥95% unique"
    return 0.4, f"{col['name']} cardinality unclear"


def score_name(col: dict[str, Any]) -> tuple[float, str | None]:
    if "TEXT" not in (col.get("type") or "").upper():
        return 0.3, f"{col['name']} not text-typed"
    rows, distinct = _cardinality(col)
    if rows > 0 and distinct >= 0.8 * rows:
        return 0.85, f"{col['name']} text + high distinctness"
    if rows > 0 and distinct >= 0.5 * rows:
        return 0.7, f"{col['name']} text + medium distinctness"
    return 0.5, f"{col['name']} text but low distinctness"


def score_country(col: dict[str, Any]) -> tuple[float, str | None]:
    ex = _examples(col)
    if not ex:
        return 0.5, None
    matches = sum(1 for v in ex if isinstance(v, str) and CountryRE.match(v))
    score = matches / len(ex)
    if score >= 0.8:
        return min(1.0, score + 0.1), f"{col['name']} values look like ISO-2 country codes"
    return max(0.3, score), f"{col['name']} country shape weak"


def score_currency(col: dict[str, Any]) -> tuple[float, str | None]:
    ex = _examples(col)
    if not ex:
        return 0.5, None
    matches = sum(1 for v in ex if isinstance(v, str) and CurrencyRE.match(v))
    score = matches / len(ex)
    if score >= 0.8:
        return min(1.0, score + 0.1), f"{col['name']} values look like ISO-4217 currency codes"
    return max(0.3, score), f"{col['name']} currency shape weak"


def score_status(col: dict[str, Any]) -> tuple[float, str | None]:
    rows, distinct = _cardinality(col)
    if rows == 0:
        return 0.5, None
    if distinct <= 10 and "TEXT" in (col.get("type") or "").upper():
        return 0.9, f"{col['name']} low-cardinality text — enum-shaped ({distinct} distinct)"
    if distinct <= 10:
        return 0.75, f"{col['name']} low-cardinality ({distinct} distinct)"
    return 0.4, f"{col['name']} high-cardinality, not enum-shaped"


def score_flag(col: dict[str, Any]) -> tuple[float, str | None]:
    _, distinct = _cardinality(col)
    if distinct <= 2:
        return 0.95, f"{col['name']} boolean-shaped ({distinct} distinct)"
    if distinct <= 5:
        return 0.7, f"{col['name']} small enum, possibly flag"
    return 0.3, f"{col['name']} not flag-shaped"


def score_numeric(col: dict[str, Any]) -> tuple[float, str | None]:
    typ = (col.get("type") or "").upper()
    if "REAL" in typ or "INT" in typ or "NUM" in typ:
        return 0.95, f"{col['name']} is {typ}"
    return 0.3, f"{col['name']} not numeric-typed"


def score_date(col: dict[str, Any]) -> tuple[float, str | None]:
    ex = _examples(col)
    if not ex:
        return 0.5, None
    matches = sum(1 for v in ex if isinstance(v, str) and DateRE.match(v))
    if matches >= max(1, len(ex) // 2):
        return 0.9, f"{col['name']} values are ISO-8601 shaped"
    return 0.4, f"{col['name']} date shape weak"


def score_default(col: dict[str, Any]) -> tuple[float, str | None]:
    """Catch-all for roles we don't have a dedicated tester for. Confidence
    based purely on the column existing and being non-empty."""
    rows, _ = _cardinality(col)
    if rows > 0:
        return 0.7, None
    return 0.5, None


# Role -> tester registry. Add new roles here.
ROLE_TESTERS = {
    "id": score_id,
    "doc_id": score_id,
    "name": score_name,
    "country": score_country,
    "currency": score_currency,
    "status": score_status,
    "fragile_flag": score_flag,
    "hazmat_flag": score_flag,
    "blocked": score_flag,
    "active": score_flag,
    "qty": score_numeric,
    "amount": score_numeric,
    "price": score_numeric,
    "weight": score_numeric,
    "min_amount": score_numeric,
    "max_amount": score_numeric,
    "unrestricted": score_numeric,
    "in_inspection": score_numeric,
    "blocked_qty": score_numeric,
    "doc_date": score_date,
    "approved_at": score_date,
    "created_at": score_date,
    "posted_at": score_date,
    "expires": score_date,
}


# ---------------------------------------------------------------------------
# Concept-level scoring
# ---------------------------------------------------------------------------

def _column_by_name(table_meta: dict[str, Any], col_name: str) -> dict[str, Any] | None:
    for c in table_meta.get("columns", []):
        if c["name"] == col_name:
            return c
    return None


def _referrer_counts(dossier: dict[str, Any]) -> dict[str, int]:
    """How many tables FK *into* a given table — high = master concept."""
    by_target: dict[str, int] = defaultdict(int)
    for edge in dossier.get("fk_edges", []):
        by_target[edge.get("to_table", "")] += 1
    return by_target


def score_concept(
    concept_name: str,
    binding: dict[str, Any],
    dossier: dict[str, Any],
    referrer_counts: dict[str, int],
) -> tuple[float, list[str]]:
    """Score a single concept binding. Returns (confidence, evidence_list)."""
    table = binding.get("table")
    columns = binding.get("columns", {}) or {}
    table_meta = dossier.get("tables", {}).get(table)

    if not table_meta:
        return 0.0, [f"binding references unknown table {table}"]

    evidence: list[str] = []
    scores: list[float] = []

    # Per-column role checks
    for role, col_name in columns.items():
        col = _column_by_name(table_meta, col_name)
        if not col:
            scores.append(0.2)
            evidence.append(f"column {col_name} not found in {table}")
            continue
        tester = ROLE_TESTERS.get(role, score_default)
        score, ev = tester(col)
        scores.append(score)
        if ev:
            evidence.append(ev)

    # FK-target signal — boosts master concepts (item, vendor, warehouse)
    refs = referrer_counts.get(table, 0)
    if refs >= 3:
        scores.append(min(1.0, 0.7 + 0.1 * refs))
        evidence.append(f"FK target from {refs} other tables (master shape)")
    elif refs > 0:
        scores.append(0.65)
        evidence.append(f"FK target from {refs} other table(s)")

    # Row-count sanity — empty tables are suspicious
    if table_meta.get("row_count", 0) == 0:
        scores.append(0.3)
        evidence.append("table is empty — confidence reduced")
    else:
        evidence.append(f"{table_meta['row_count']} rows in {table}")

    if not scores:
        return 0.5, evidence

    confidence = round(sum(scores) / len(scores), 3)
    # Cap at 0.99 — Coat is never 100% certain about an inferred mapping
    confidence = min(0.99, confidence)
    return confidence, evidence


def score_all_concepts(
    concept_map: dict[str, Any],
    dossier: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Score every concept in the map. Returns a parallel dict keyed by
    concept name, each value is {confidence: float, evidence: [str]}."""
    referrers = _referrer_counts(dossier)
    out: dict[str, dict[str, Any]] = {}
    for name, binding in concept_map.items():
        conf, ev = score_concept(name, binding, dossier, referrers)
        out[name] = {"confidence": conf, "evidence": ev}
    return out
