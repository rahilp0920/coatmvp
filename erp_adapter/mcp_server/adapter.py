"""ERP adapter — translates business-concept calls into messy-schema SQL.

The MCP server below uses these functions, but they're separated so the agent
demo can drive them directly without spinning up a full MCP transport.

Every adapter call is logged to WORKFLOW_OBS for the live learner to mine.
Learned patterns from LEARNED_PATTERNS are consulted to bias decisions
(e.g. preferred source warehouse for fragile items).
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "erp.db"
CONTEXT_PATH = ROOT / "context" / "context.yaml"


# ---------------------------------------------------------------------------
# Connection + context loading
# ---------------------------------------------------------------------------

def load_context() -> dict[str, Any]:
    if not CONTEXT_PATH.exists():
        raise FileNotFoundError(
            f"Context file missing at {CONTEXT_PATH}. Run discovery first."
        )
    return yaml.safe_load(CONTEXT_PATH.read_text())


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Observation logging — every adapter call writes one row
# ---------------------------------------------------------------------------

_LEARN_EVERY = 10  # re-mine patterns every N observations


def _log_obs(conn: sqlite3.Connection, *, actor: str, tool: str,
             args: dict[str, Any], result: dict[str, Any], outcome: str,
             feedback: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO WORKFLOW_OBS (TS,ACTOR,TOOL,ARGS_JSON,RESULT_JSON,OUTCOME,FEEDBACK)"
        " VALUES (?,?,?,?,?,?,?)",
        (now_iso(), actor, tool, json.dumps(args, default=str),
         json.dumps(result, default=str), outcome, feedback),
    )
    obs_id = cur.lastrowid
    # Live learning trigger: every Nth observation OR every FEEDBACK row
    should_learn = (obs_id % _LEARN_EVERY == 0) or outcome == "FEEDBACK"
    if should_learn:
        # Defer import to avoid cycle on module load
        try:
            from learner.miner import run_once
            conn.commit()
            run_once(verbose=False)
        except Exception as e:  # noqa: BLE001 — learning failures must not break tool calls
            print(f"[adapter] learner failed: {e}")
    return obs_id


def _learned_patterns(conn: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT KEY, VALUE_JSON, SUPPORT, CONFIDENCE FROM LEARNED_PATTERNS WHERE KIND=?",
        (kind,),
    ).fetchall()
    return [
        {"key": r["KEY"], "value": json.loads(r["VALUE_JSON"]),
         "support": r["SUPPORT"], "confidence": r["CONFIDENCE"]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Concept resolution — business name -> physical (table, columns)
# ---------------------------------------------------------------------------

class ConceptResolver:
    def __init__(self, ctx: dict[str, Any]):
        self.ctx = ctx
        self.cmap = ctx["concept_map"]

    def table(self, concept: str) -> str:
        return self.cmap[concept]["table"]

    def col(self, concept: str, role: str) -> str:
        return self.cmap[concept]["columns"][role]


# ---------------------------------------------------------------------------
# Inventory operations
# ---------------------------------------------------------------------------

def find_item(query: str, *, actor: str = "agent") -> dict[str, Any]:
    """Find items by id or partial name match."""
    ctx = load_context()
    r = ConceptResolver(ctx)
    items_tbl = r.table("item")
    id_col, name_col = r.col("item", "id"), r.col("item", "name")
    fragile_col = r.col("item", "fragile_flag")
    hazmat_col = r.col("item", "hazmat_flag")

    with db() as conn:
        sql = (
            f"SELECT {id_col} AS id, {name_col} AS name, "
            f"{fragile_col} AS fragile, {hazmat_col} AS hazmat "
            f"FROM {items_tbl} "
            f"WHERE LOWER({id_col}) LIKE LOWER(?) OR LOWER({name_col}) LIKE LOWER(?)"
            f" LIMIT 25"
        )
        like = f"%{query}%"
        rows = [dict(r) for r in conn.execute(sql, (like, like)).fetchall()]
        result = {"matches": rows, "count": len(rows)}
        _log_obs(conn, actor=actor, tool="find_item", args={"query": query},
                 result=result, outcome="OK")
    return result


def get_stock(matnr: str, *, actor: str = "agent") -> dict[str, Any]:
    """Return per-warehouse stock with the *true* available quantity (LABST minus
    active reservations) and bin-level breakdown. This is the "where is it
    really" answer the agent gets to ask without knowing about Z_RESERVED."""
    ctx = load_context()
    r = ConceptResolver(ctx)

    sw = r.table("stock_by_warehouse")
    sb = r.table("stock_by_bin")
    rsv = r.table("reservation")
    item_col = r.col("stock_by_warehouse", "item")
    wh_col = r.col("stock_by_warehouse", "warehouse")
    unrest_col = r.col("stock_by_warehouse", "unrestricted")
    rsv_qty_col = r.col("reservation", "qty")
    rsv_item_col = r.col("reservation", "item")
    rsv_wh_col = r.col("reservation", "warehouse")
    rsv_exp_col = r.col("reservation", "expires")

    with db() as conn:
        wh_rows = conn.execute(
            f"SELECT {wh_col} AS wh, {unrest_col} AS unrestricted "
            f"FROM {sw} WHERE {item_col}=?",
            (matnr,),
        ).fetchall()

        out = []
        for wh in wh_rows:
            reserved = conn.execute(
                f"SELECT COALESCE(SUM({rsv_qty_col}),0) FROM {rsv} "
                f"WHERE {rsv_item_col}=? AND {rsv_wh_col}=? "
                f"AND ({rsv_exp_col} IS NULL OR {rsv_exp_col} > ?)",
                (matnr, wh["wh"], now_iso()),
            ).fetchone()[0]
            bins = conn.execute(
                f"SELECT BIN_CODE, LGORT, QTY, Z_STATUS FROM {sb} "
                f"WHERE MATNR=? AND WERKS=?",
                (matnr, wh["wh"]),
            ).fetchall()
            out.append({
                "warehouse": wh["wh"],
                "unrestricted": wh["unrestricted"],
                "reserved": reserved,
                "available": wh["unrestricted"] - reserved,
                "bins": [dict(b) for b in bins],
            })
        # Sort by available desc — most-stocked warehouse first
        out.sort(key=lambda x: -x["available"])
        result = {"item": matnr, "by_warehouse": out,
                  "total_available": sum(x["available"] for x in out)}
        _log_obs(conn, actor=actor, tool="get_stock", args={"matnr": matnr},
                 result={"total_available": result["total_available"],
                         "wh_count": len(out)},
                 outcome="OK")
    return result


def _is_fragile(conn: sqlite3.Connection, matnr: str) -> bool:
    row = conn.execute("SELECT Z_FRAGILE FROM MAT_MASTER WHERE MATNR=?", (matnr,)).fetchone()
    return bool(row and row[0])


def suggest_source_warehouse(matnr: str, qty: float, *, actor: str = "agent") -> dict[str, Any]:
    """Pick a source warehouse for a transfer. Bias by learned routing patterns
    and surface any per-item feedback the agent should consider."""
    stock = get_stock(matnr, actor=actor)
    candidates = [w for w in stock["by_warehouse"] if w["available"] >= qty]
    if not candidates:
        return {"chosen": None, "reason": "No warehouse has enough available stock.",
                "candidates": stock["by_warehouse"]}

    explanation = "Highest available stock."
    chosen = candidates[0]["warehouse"]
    related_feedback: list[str] = []

    with db() as conn:
        if _is_fragile(conn, matnr):
            patterns = _learned_patterns(conn, "ROUTING")
            for p in patterns:
                if p["key"] == "fragile_source_warehouse" and p["confidence"] >= 0.6:
                    preferred = p["value"].get("warehouse")
                    if preferred and any(c["warehouse"] == preferred for c in candidates):
                        chosen = preferred
                        explanation = (f"Learned routing: fragile items source from "
                                       f"{preferred} (support={p['support']}, "
                                       f"confidence={p['confidence']:.2f}).")
                        break

        # Per-item feedback override hint — text-match on matnr in feedback args
        for p in _learned_patterns(conn, "PREFERENCE"):
            if not p["key"].startswith("feedback_"):
                continue
            for corr in p["value"].get("corrections", []):
                if corr.get("args", {}).get("matnr") == matnr:
                    related_feedback.append(corr.get("feedback", ""))

        result = {"chosen": chosen, "reason": explanation,
                  "candidates": [c["warehouse"] for c in candidates],
                  "related_feedback": related_feedback}
        _log_obs(conn, actor=actor, tool="suggest_source_warehouse",
                 args={"matnr": matnr, "qty": qty}, result=result, outcome="OK")
    return result


def move_stock(matnr: str, qty: float, from_warehouse: str, to_warehouse: str,
               *, actor: str = "agent", reason: str | None = None) -> dict[str, Any]:
    """Post a stock transfer (movement type 311). Decrements bins on source,
    creates them on destination, updates WH_STOCK rollups."""
    if from_warehouse == to_warehouse:
        return {"ok": False, "error": "Source and destination warehouses are the same."}

    with db() as conn:
        # Pull bins on source by FIFO of largest qty first
        bins = conn.execute(
            "SELECT BIN_CODE, LGORT, QTY FROM BIN_DETAIL "
            "WHERE MATNR=? AND WERKS=? AND Z_STATUS='OK' ORDER BY QTY DESC",
            (matnr, from_warehouse),
        ).fetchall()
        total = sum(b["QTY"] for b in bins)
        if total < qty:
            result = {"ok": False, "error": f"Insufficient stock at {from_warehouse}: "
                                            f"have {total}, need {qty}."}
            _log_obs(conn, actor=actor, tool="move_stock",
                     args={"matnr": matnr, "qty": qty,
                           "from": from_warehouse, "to": to_warehouse},
                     result=result, outcome="DENIED")
            return result

        remaining = qty
        for b in bins:
            if remaining <= 0:
                break
            take = min(remaining, b["QTY"])
            conn.execute(
                "UPDATE BIN_DETAIL SET QTY=QTY-? "
                "WHERE MATNR=? AND WERKS=? AND LGORT=? AND BIN_CODE=?",
                (take, matnr, from_warehouse, b["LGORT"], b["BIN_CODE"]),
            )
            remaining -= take

        # Land on destination MAIN/RECV bin
        dest_bin = "M01-1-1"
        conn.execute(
            "INSERT INTO BIN_DETAIL (MATNR,WERKS,LGORT,BIN_CODE,QTY,Z_STATUS) "
            "VALUES (?,?,?,?,?, 'OK') "
            "ON CONFLICT(MATNR,WERKS,LGORT,BIN_CODE) DO UPDATE SET QTY=QTY+excluded.QTY",
            (matnr, to_warehouse, "MAIN", dest_bin, qty),
        )

        # Update rollups
        conn.execute(
            "UPDATE WH_STOCK SET LABST=LABST-? WHERE MATNR=? AND WERKS=?",
            (qty, matnr, from_warehouse),
        )
        conn.execute(
            "INSERT INTO WH_STOCK (MATNR,WERKS,LABST,INSME,RETME) VALUES (?,?,?,0,0) "
            "ON CONFLICT(MATNR,WERKS) DO UPDATE SET LABST=LABST+excluded.LABST",
            (matnr, to_warehouse, qty),
        )

        # Material doc (MSEG)
        mblnr = "DOC" + str(int(time.time()))
        conn.execute(
            "INSERT INTO MSEG (MBLNR,ZEILE,BWART,MATNR,WERKS_FROM,WERKS_TO,"
            "LGORT_FROM,LGORT_TO,MENGE,POSTED_BY,POSTED_AT) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (mblnr, 1, "311", matnr, from_warehouse, to_warehouse,
             "MAIN", "MAIN", qty, actor, now_iso()),
        )

        result = {"ok": True, "doc": mblnr, "moved": qty,
                  "from": from_warehouse, "to": to_warehouse}
        _log_obs(conn, actor=actor, tool="move_stock",
                 args={"matnr": matnr, "qty": qty,
                       "from": from_warehouse, "to": to_warehouse, "reason": reason},
                 result=result, outcome="OK")
    return result


# ---------------------------------------------------------------------------
# Transaction operations
# ---------------------------------------------------------------------------

def _resolve_approval_role(conn: sqlite3.Connection, doc_type: str,
                           amount: float) -> str:
    """Walk Z_APPR_RULES, then check learned overrides."""
    row = conn.execute(
        "SELECT APPROVER_ROLE FROM Z_APPR_RULES "
        "WHERE DOC_TYPE=? AND ACTIVE=1 AND ? >= MIN_AMT AND ? < MAX_AMT "
        "ORDER BY MIN_AMT DESC LIMIT 1",
        (doc_type, amount, amount),
    ).fetchone()
    return row["APPROVER_ROLE"] if row else "MANAGER"


def post_invoice(vendor: str, amount: float, currency: str = "USD",
                 lines: list[dict[str, Any]] | None = None,
                 *, actor: str = "agent",
                 company_code: str = "1000") -> dict[str, Any]:
    """Park (and where possible auto-approve) an AP invoice. Returns the doc
    number, the routing decision, and a list of GL entries created."""
    with db() as conn:
        v = conn.execute("SELECT NAME1, SPERR, Z_RATING FROM LFA1 WHERE LIFNR=?", (vendor,)).fetchone()
        if not v:
            return {"ok": False, "error": f"Unknown vendor {vendor}"}
        if v["SPERR"]:
            result = {"ok": False, "error": f"Vendor {vendor} ({v['NAME1']}) is blocked."}
            _log_obs(conn, actor=actor, tool="post_invoice",
                     args={"vendor": vendor, "amount": amount},
                     result=result, outcome="DENIED")
            return result

        belnr = "INV-" + datetime.now().strftime("%Y%m%d%H%M%S%f")
        role = _resolve_approval_role(conn, "AP_INVOICE", amount)

        # Learned approval shortcut?
        approver = None
        approved_at = None
        status = "PARK"
        learned_note = None
        for p in _learned_patterns(conn, "APPROVAL"):
            if p["key"] == "vendor_fast_track" \
                    and p["value"].get("vendor") == vendor \
                    and amount <= p["value"].get("amount_ceiling", 0) \
                    and p["confidence"] >= 0.6:
                approver = p["value"].get("approver_user")
                approved_at = now_iso()
                status = "APPR"
                learned_note = (f"Learned fast-track: vendor {vendor} amounts up to "
                                f"${p['value']['amount_ceiling']:.0f} approved by {approver}.")
                break

        if status == "PARK" and role == "AUTO":
            status = "APPR"
            approver = "system"
            approved_at = now_iso()

        conn.execute(
            "INSERT INTO AP_HEAD (BELNR,BUKRS,LIFNR,BLDAT,WAERS,WRBTR,STATUS,"
            "APPROVER,APPROVED_AT,CREATED_BY,CREATED_AT) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (belnr, company_code, vendor, now_iso(), currency, amount, status,
             approver, approved_at, actor, now_iso()),
        )

        for i, line in enumerate(lines or [], start=1):
            conn.execute(
                "INSERT INTO AP_LINES (BELNR,POSNR,MATNR,WERKS,MENGE,NETPR,NETWR) "
                "VALUES (?,?,?,?,?,?,?)",
                (belnr, i, line.get("matnr"), line.get("werks"),
                 line.get("qty"), line.get("price"),
                 (line.get("qty") or 0) * (line.get("price") or 0)),
            )

        gl_entries = []
        if status == "APPR":
            # Post to GL
            conn.execute(
                "INSERT INTO GL_ENTRIES (BELNR,HKONT,BUKRS,DMBTR,SHKZG,BLDAT,POSTED_AT) "
                "VALUES (?,?,?,?,?,?,?)",
                (belnr, "510000", company_code, amount, "S", now_iso(), now_iso()),
            )
            conn.execute(
                "INSERT INTO GL_ENTRIES (BELNR,HKONT,BUKRS,DMBTR,SHKZG,BLDAT,POSTED_AT) "
                "VALUES (?,?,?,?,?,?,?)",
                (belnr, "211000", company_code, amount, "H", now_iso(), now_iso()),
            )
            conn.execute("UPDATE AP_HEAD SET STATUS='POST' WHERE BELNR=?", (belnr,))
            gl_entries = [
                {"account": "510000", "dc": "S", "amount": amount},
                {"account": "211000", "dc": "H", "amount": amount},
            ]

        result = {
            "ok": True,
            "doc": belnr,
            "status": "POST" if status == "APPR" else status,
            "approval": {"required_role": role, "approver": approver,
                         "learned_note": learned_note},
            "gl_entries": gl_entries,
        }
        _log_obs(conn, actor=actor, tool="post_invoice",
                 args={"vendor": vendor, "amount": amount, "currency": currency},
                 result=result, outcome="OK")
    return result


def request_approval(belnr: str, decided_by: str, decision: str,
                     *, actor: str = "agent") -> dict[str, Any]:
    """Mark an invoice approved or rejected by a named user. The learner
    watches this and may codify a pattern (e.g., manager X always approves
    vendor Y up to $Z)."""
    if decision not in ("APPROVE", "REJECT"):
        return {"ok": False, "error": "decision must be APPROVE or REJECT"}
    with db() as conn:
        head = conn.execute("SELECT * FROM AP_HEAD WHERE BELNR=?", (belnr,)).fetchone()
        if not head:
            return {"ok": False, "error": f"No invoice {belnr}"}

        new_status = "APPR" if decision == "APPROVE" else "REJ"
        conn.execute(
            "UPDATE AP_HEAD SET STATUS=?, APPROVER=?, APPROVED_AT=? WHERE BELNR=?",
            (new_status, decided_by, now_iso(), belnr),
        )
        gl_entries: list[dict[str, Any]] = []
        if decision == "APPROVE":
            conn.execute(
                "INSERT INTO GL_ENTRIES (BELNR,HKONT,BUKRS,DMBTR,SHKZG,BLDAT,POSTED_AT) "
                "VALUES (?,?,?,?,?,?,?)",
                (belnr, "510000", head["BUKRS"], head["WRBTR"], "S", now_iso(), now_iso()),
            )
            conn.execute(
                "INSERT INTO GL_ENTRIES (BELNR,HKONT,BUKRS,DMBTR,SHKZG,BLDAT,POSTED_AT) "
                "VALUES (?,?,?,?,?,?,?)",
                (belnr, "211000", head["BUKRS"], head["WRBTR"], "H", now_iso(), now_iso()),
            )
            conn.execute("UPDATE AP_HEAD SET STATUS='POST' WHERE BELNR=?", (belnr,))
            gl_entries = [
                {"account": "510000", "dc": "S", "amount": head["WRBTR"]},
                {"account": "211000", "dc": "H", "amount": head["WRBTR"]},
            ]
        result = {"ok": True, "doc": belnr,
                  "status": "POST" if decision == "APPROVE" else "REJ",
                  "decided_by": decided_by, "gl_entries": gl_entries}
        _log_obs(conn, actor=actor, tool="request_approval",
                 args={"belnr": belnr, "decided_by": decided_by, "decision": decision},
                 result=result, outcome="OK")
    return result


def list_pending_invoices(*, actor: str = "agent") -> dict[str, Any]:
    with db() as conn:
        rows = conn.execute(
            "SELECT h.BELNR, h.LIFNR, v.NAME1 AS vendor_name, h.WRBTR, h.WAERS, "
            "h.STATUS, h.CREATED_BY, h.CREATED_AT "
            "FROM AP_HEAD h LEFT JOIN LFA1 v ON h.LIFNR=v.LIFNR "
            "WHERE h.STATUS IN ('PARK','APPR') ORDER BY h.CREATED_AT DESC LIMIT 25"
        ).fetchall()
        result = {"pending": [dict(r) for r in rows], "count": len(rows)}
        _log_obs(conn, actor=actor, tool="list_pending_invoices",
                 args={}, result={"count": len(rows)}, outcome="OK")
    return result


def submit_feedback(obs_id: int, feedback: str, *, actor: str = "user") -> dict[str, Any]:
    """Attach human feedback to a prior observation. The learner picks this
    up and may produce a pattern."""
    with db() as conn:
        conn.execute("UPDATE WORKFLOW_OBS SET FEEDBACK=?, OUTCOME='FEEDBACK' WHERE OBS_ID=?",
                     (feedback, obs_id))
        result = {"ok": True, "obs_id": obs_id}
        _log_obs(conn, actor=actor, tool="submit_feedback",
                 args={"obs_id": obs_id, "feedback": feedback},
                 result=result, outcome="OK")
    return result


def list_concepts(*, actor: str = "agent") -> dict[str, Any]:
    """Tell the agent what business concepts are available — derived live from
    the discovered context, so a different ERP would yield a different list."""
    ctx = load_context()
    cm = ctx["concept_map"]
    concepts = {
        name: {"physical_table": meta["table"], "notes": meta.get("notes", "")}
        for name, meta in cm.items()
    }
    derived = {d["name"]: d.get("definition", "") for d in ctx.get("derived_views", [])}
    return {"concepts": concepts, "derived": derived}
