"""Live workflow learner.

Mines WORKFLOW_OBS for actionable patterns and writes them to LEARNED_PATTERNS,
which the adapter consults at decision time. Three pattern families today:

  ROUTING       — preferred source warehouse for a class of item
                  (e.g., fragile items source from WH02)
  APPROVAL      — vendor + amount-bucket fast-tracks
                  (e.g., V1001 invoices <$5k auto-approved by u_mgr_c)
  PREFERENCE    — generic key/value preference learned from human FEEDBACK

Patterns require a minimum support and confidence to be codified, and are
re-derived from scratch each run so corrections via FEEDBACK actually take
effect.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "erp.db"

MIN_SUPPORT = 5
MIN_CONFIDENCE = 0.6


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# ROUTING: preferred source warehouse for fragile vs non-fragile transfers
# ---------------------------------------------------------------------------

def mine_routing(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Look at move_stock + suggest_source_warehouse history, partition by
    fragile flag, count source-warehouse choices."""
    fragile_set = {
        r[0] for r in conn.execute(
            "SELECT MATNR FROM MAT_MASTER WHERE Z_FRAGILE=1"
        ).fetchall()
    }

    obs = conn.execute(
        "SELECT TS, ACTOR, TOOL, ARGS_JSON, RESULT_JSON, OUTCOME "
        "FROM WORKFLOW_OBS WHERE TOOL IN ('move_stock','suggest_source_warehouse') "
        "AND OUTCOME='OK'"
    ).fetchall()

    fragile_choices: Counter[str] = Counter()
    nonfragile_choices: Counter[str] = Counter()

    for row in obs:
        args = json.loads(row["ARGS_JSON"]) if row["ARGS_JSON"] else {}
        result = json.loads(row["RESULT_JSON"]) if row["RESULT_JSON"] else {}
        matnr = args.get("matnr")
        if not matnr:
            continue
        # Source warehouse can be in args (move_stock: "from") or
        # in synthetic seed (result: "chosen_source")
        chosen = args.get("from") or args.get("from_warehouse") or result.get("chosen_source")
        if not chosen:
            continue
        if matnr in fragile_set:
            fragile_choices[chosen] += 1
        else:
            nonfragile_choices[chosen] += 1

    patterns: list[dict[str, Any]] = []
    for label, counts in (("fragile_source_warehouse", fragile_choices),
                          ("nonfragile_source_warehouse", nonfragile_choices)):
        total = sum(counts.values())
        if total < MIN_SUPPORT or not counts:
            continue
        wh, n = counts.most_common(1)[0]
        confidence = n / total
        if confidence < MIN_CONFIDENCE:
            continue
        patterns.append({
            "kind": "ROUTING",
            "key": label,
            "value": {"warehouse": wh, "alternatives": dict(counts.most_common())},
            "support": total,
            "confidence": round(confidence, 3),
        })
    return patterns


# ---------------------------------------------------------------------------
# APPROVAL: vendor + amount-bucket fast-tracks
# ---------------------------------------------------------------------------

def mine_approvals(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """If a manager has approved many invoices for the same vendor under a
    consistent amount ceiling, codify a fast-track pattern. We pull from both
    AP_HEAD history and synthetic 'approve_invoice' observations."""
    by_vendor: dict[str, dict[str, list]] = defaultdict(lambda: {"amounts": [], "approvers": []})

    # AP_HEAD historical approvals
    rows = conn.execute(
        "SELECT LIFNR, WRBTR, APPROVER FROM AP_HEAD "
        "WHERE STATUS IN ('APPR','POST') AND APPROVER IS NOT NULL"
    ).fetchall()
    for r in rows:
        if not r["LIFNR"]:
            continue
        by_vendor[r["LIFNR"]]["amounts"].append(r["WRBTR"] or 0)
        by_vendor[r["LIFNR"]]["approvers"].append(r["APPROVER"])

    # Synthetic approve_invoice observations
    obs = conn.execute(
        "SELECT ARGS_JSON, ACTOR FROM WORKFLOW_OBS "
        "WHERE TOOL='approve_invoice' AND OUTCOME='OK'"
    ).fetchall()
    for row in obs:
        args = json.loads(row["ARGS_JSON"]) if row["ARGS_JSON"] else {}
        v = args.get("vendor")
        if not v:
            continue
        by_vendor[v]["amounts"].append(args.get("amount") or 0)
        by_vendor[v]["approvers"].append(row["ACTOR"])

    patterns: list[dict[str, Any]] = []
    for vendor, data in by_vendor.items():
        amounts = data["amounts"]
        approvers = data["approvers"]
        if len(amounts) < MIN_SUPPORT:
            continue
        approver_counts = Counter(approvers)
        top_approver, n_top = approver_counts.most_common(1)[0]
        if top_approver in ("system", None) or top_approver == "":
            continue
        confidence = n_top / len(approvers)
        if confidence < MIN_CONFIDENCE:
            continue
        ceiling = max(amounts) * 1.1  # 10% headroom over observed max
        patterns.append({
            "kind": "APPROVAL",
            "key": "vendor_fast_track",
            "value": {
                "vendor": vendor,
                "approver_user": top_approver,
                "amount_ceiling": round(ceiling, 2),
                "observed_max": max(amounts),
            },
            "support": len(amounts),
            "confidence": round(confidence, 3),
        })
    return patterns


# ---------------------------------------------------------------------------
# PREFERENCE: free-text feedback distilled into rules
# ---------------------------------------------------------------------------

def mine_feedback_preferences(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Group recent FEEDBACK observations and emit one PREFERENCE pattern per
    distinct recurring theme. We don't NLP it deeply — we just store the most
    recent unique feedback strings as soft rules the agent should consult."""
    rows = conn.execute(
        "SELECT TS, TOOL, ARGS_JSON, FEEDBACK FROM WORKFLOW_OBS "
        "WHERE OUTCOME='FEEDBACK' AND FEEDBACK IS NOT NULL AND FEEDBACK != '' "
        "ORDER BY TS DESC LIMIT 50"
    ).fetchall()
    if not rows:
        return []

    # Bucket by tool
    by_tool: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_tool[r["TOOL"]].append({
            "ts": r["TS"],
            "args": json.loads(r["ARGS_JSON"]) if r["ARGS_JSON"] else {},
            "feedback": r["FEEDBACK"],
        })

    patterns: list[dict[str, Any]] = []
    for tool, items in by_tool.items():
        # Deduplicate by feedback text, keep most recent
        seen: dict[str, dict] = {}
        for it in items:
            seen.setdefault(it["feedback"], it)
        patterns.append({
            "kind": "PREFERENCE",
            "key": f"feedback_{tool}",
            "value": {"corrections": list(seen.values())[:5]},
            "support": len(items),
            "confidence": 1.0,
        })
    return patterns


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run_once(verbose: bool = True) -> dict[str, Any]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        patterns: list[dict[str, Any]] = []
        patterns.extend(mine_routing(conn))
        patterns.extend(mine_approvals(conn))
        patterns.extend(mine_feedback_preferences(conn))

        # Replace prior patterns wholesale — feedback corrections must take effect
        conn.execute("DELETE FROM LEARNED_PATTERNS")
        for p in patterns:
            conn.execute(
                "INSERT INTO LEARNED_PATTERNS (KIND,KEY,VALUE_JSON,SUPPORT,CONFIDENCE,LEARNED_AT) "
                "VALUES (?,?,?,?,?,?)",
                (p["kind"], p["key"], json.dumps(p["value"], default=str),
                 p["support"], p["confidence"], now_iso()),
            )
        conn.commit()
        if verbose:
            print(f"[learner] codified {len(patterns)} patterns:")
            for p in patterns:
                print(f"  [{p['kind']}] {p['key']} support={p['support']} "
                      f"confidence={p['confidence']:.2f} value={p['value']}")
        return {"patterns": patterns}
    finally:
        conn.close()


def main() -> None:
    run_once()


if __name__ == "__main__":
    main()
