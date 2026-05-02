"""LLM-powered semantic mapper.

Takes the introspection dossier and asks Claude to label each table/column
with its business meaning, then collapses the result into a `context.yaml`
keyed by business concepts (item, stock, available_stock, vendor, invoice, ...).

This is the layer that solves "where is information stored" — given a concept
the agent cares about, it tells the MCP server which tables/columns/joins to use.

Falls back to a hand-curated mapping when ANTHROPIC_API_KEY is not set, so the
MVP stays demoable offline.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DOSSIER_PATH = ROOT / "context" / "dossier.json"
CONTEXT_PATH = ROOT / "context" / "context.yaml"

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-7")

PROMPT = """You are a semantic mapper for an unknown ERP database. You are
given a dossier of every table: column names, types, sample rows, FK edges,
and per-column stats. Your job is to identify the *business concept* each
table and column represents.

Return a single JSON object with this shape:

{
  "concept_map": {
    "<concept>": {
      "table": "<TABLE_NAME>",
      "columns": {"<role>": "<COLUMN_NAME>", ...},
      "notes": "<one short sentence on quirks>"
    },
    ...
  },
  "join_paths": [
    {"from": "<concept>", "to": "<concept>", "via": "<TABLE.col = TABLE.col>"}
  ],
  "derived_views": [
    {"name": "<concept>", "definition": "<plain-English derivation>",
     "sql_hint": "<SQL fragment that computes it>"}
  ]
}

Concepts you MUST identify if present: item, vendor, warehouse, stock_by_warehouse,
stock_by_bin, reservation, available_stock (derived), ap_invoice_header,
ap_invoice_line, gl_entry, stock_movement, approval_rule, user.

Be terse. Output ONLY the JSON, no prose."""


# Hand-curated fallback so the MVP works without an API key.
FALLBACK_CONTEXT: dict[str, Any] = {
    "concept_map": {
        "item": {
            "table": "MAT_MASTER",
            "columns": {"id": "MATNR", "name": "MAKTX", "type": "MTART",
                        "uom": "MEINS", "weight": "NTGEW",
                        "fragile_flag": "Z_FRAGILE", "hazmat_flag": "Z_HAZMAT"},
            "notes": "Custom Z_FRAGILE / Z_HAZMAT flags drive routing rules."
        },
        "vendor": {
            "table": "LFA1",
            "columns": {"id": "LIFNR", "name": "NAME1", "country": "LAND1",
                        "rating": "Z_RATING", "blocked": "SPERR"},
            "notes": "SPERR=1 means vendor is blocked — refuse new invoices."
        },
        "warehouse": {
            "table": "T001W",
            "columns": {"id": "WERKS", "name": "NAME1", "country": "LAND1",
                        "region": "Z_REGION"},
            "notes": "WERKS is the plant/warehouse code."
        },
        "stock_by_warehouse": {
            "table": "WH_STOCK",
            "columns": {"item": "MATNR", "warehouse": "WERKS",
                        "unrestricted": "LABST", "in_inspection": "INSME",
                        "blocked": "RETME"},
            "notes": "Rollup of BIN_DETAIL.QTY where Z_STATUS='OK'. Stale relative to bins."
        },
        "stock_by_bin": {
            "table": "BIN_DETAIL",
            "columns": {"item": "MATNR", "warehouse": "WERKS",
                        "storage_location": "LGORT", "bin": "BIN_CODE",
                        "qty": "QTY", "status": "Z_STATUS"},
            "notes": "Source of truth. Status OK/QI/BLK; only OK is sellable."
        },
        "reservation": {
            "table": "Z_RESERVED",
            "columns": {"id": "RES_ID", "item": "MATNR", "warehouse": "WERKS",
                        "qty": "QTY", "doc": "REF_DOC", "expires": "EXPIRES_AT"},
            "notes": "Custom Z-table. Subtract active rows from LABST for true availability."
        },
        "ap_invoice_header": {
            "table": "AP_HEAD",
            "columns": {"id": "BELNR", "company_code": "BUKRS", "vendor": "LIFNR",
                        "doc_date": "BLDAT", "currency": "WAERS",
                        "amount": "WRBTR", "status": "STATUS",
                        "approver": "APPROVER", "approved_at": "APPROVED_AT",
                        "created_by": "CREATED_BY", "created_at": "CREATED_AT"},
            "notes": "STATUS lifecycle: PARK -> APPR -> POST. REJ for rejected."
        },
        "ap_invoice_line": {
            "table": "AP_LINES",
            "columns": {"doc_id": "BELNR", "line": "POSNR", "item": "MATNR",
                        "warehouse": "WERKS", "qty": "MENGE", "price": "NETPR",
                        "amount": "NETWR"},
            "notes": "Line items for AP_HEAD."
        },
        "gl_entry": {
            "table": "GL_ENTRIES",
            "columns": {"id": "GL_ID", "doc_id": "BELNR", "account": "HKONT",
                        "company_code": "BUKRS", "amount": "DMBTR",
                        "dc": "SHKZG", "doc_date": "BLDAT",
                        "posted_at": "POSTED_AT"},
            "notes": "SHKZG: S=debit, H=credit. Every posted invoice has 2+ rows."
        },
        "stock_movement": {
            "table": "MSEG",
            "columns": {"doc": "MBLNR", "line": "ZEILE", "type": "BWART",
                        "item": "MATNR", "from_warehouse": "WERKS_FROM",
                        "to_warehouse": "WERKS_TO", "qty": "MENGE",
                        "posted_by": "POSTED_BY", "posted_at": "POSTED_AT"},
            "notes": "BWART codes: 101=goods receipt, 311=transfer, 261=consumption."
        },
        "approval_rule": {
            "table": "Z_APPR_RULES",
            "columns": {"id": "RULE_ID", "doc_type": "DOC_TYPE",
                        "min_amount": "MIN_AMT", "max_amount": "MAX_AMT",
                        "role": "APPROVER_ROLE", "active": "ACTIVE"},
            "notes": "Custom Z-table. AUTO/MANAGER/CFO bracket by amount."
        },
        "user": {
            "table": "USERS",
            "columns": {"id": "USER_ID", "name": "USER_NAME", "role": "ROLE"},
            "notes": "Roles: CLERK, MANAGER, CFO."
        },
    },
    "join_paths": [
        {"from": "stock_by_warehouse", "to": "item",      "via": "WH_STOCK.MATNR = MAT_MASTER.MATNR"},
        {"from": "stock_by_warehouse", "to": "warehouse", "via": "WH_STOCK.WERKS = T001W.WERKS"},
        {"from": "stock_by_bin",       "to": "stock_by_warehouse",
                                           "via": "BIN_DETAIL.MATNR = WH_STOCK.MATNR AND BIN_DETAIL.WERKS = WH_STOCK.WERKS"},
        {"from": "reservation",        "to": "stock_by_warehouse",
                                           "via": "Z_RESERVED.MATNR = WH_STOCK.MATNR AND Z_RESERVED.WERKS = WH_STOCK.WERKS"},
        {"from": "ap_invoice_line",    "to": "ap_invoice_header", "via": "AP_LINES.BELNR = AP_HEAD.BELNR"},
        {"from": "ap_invoice_header",  "to": "vendor",   "via": "AP_HEAD.LIFNR = LFA1.LIFNR"},
        {"from": "gl_entry",           "to": "ap_invoice_header", "via": "GL_ENTRIES.BELNR = AP_HEAD.BELNR"},
    ],
    "derived_views": [
        {
            "name": "available_stock",
            "definition": "Unrestricted stock minus active reservations.",
            "sql_hint": (
                "SELECT s.MATNR, s.WERKS, "
                "s.LABST - COALESCE((SELECT SUM(QTY) FROM Z_RESERVED r "
                "WHERE r.MATNR=s.MATNR AND r.WERKS=s.WERKS "
                "AND (r.EXPIRES_AT IS NULL OR r.EXPIRES_AT > CURRENT_TIMESTAMP)), 0) AS available "
                "FROM WH_STOCK s"
            ),
        }
    ],
}


def call_claude(dossier: dict[str, Any]) -> dict[str, Any]:
    """Hit Claude with the dossier. Returns parsed JSON, or raises on failure."""
    import anthropic  # imported lazily so offline mode doesn't require it

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=PROMPT,
        messages=[{
            "role": "user",
            "content": "Dossier:\n```json\n" + json.dumps(dossier, default=str)[:120_000] + "\n```",
        }],
    )
    text = "".join(block.text for block in msg.content if hasattr(block, "text"))
    # tolerate ```json fences
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


def map_semantically(use_llm: bool | None = None) -> dict[str, Any]:
    if use_llm is None:
        use_llm = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if not DOSSIER_PATH.exists():
        from .introspect import main as run_introspect
        run_introspect()
    dossier = json.loads(DOSSIER_PATH.read_text())
    if use_llm:
        try:
            return call_claude(dossier)
        except Exception as e:  # noqa: BLE001
            print(f"[semantic_map] LLM call failed ({e}); falling back to curated mapping.")
    return FALLBACK_CONTEXT


def main() -> None:
    ctx = map_semantically()
    CONTEXT_PATH.write_text(yaml.safe_dump(ctx, sort_keys=False, default_flow_style=False))
    n = len(ctx.get("concept_map", {}))
    print(f"Wrote {n} concepts -> {CONTEXT_PATH}")


if __name__ == "__main__":
    main()
