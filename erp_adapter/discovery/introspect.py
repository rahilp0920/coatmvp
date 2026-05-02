"""Schema introspection — dumps tables, columns, samples, FK edges, and
synthetic statistics that the LLM-powered semantic mapper consumes.

This is the "where is information stored" core. The output is a structured
dossier of the unknown ERP that gets handed to Claude for semantic labeling.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "erp.db"

# Tables that belong to the adapter, not the ERP.
ADAPTER_TABLES = {"WORKFLOW_OBS", "LEARNED_PATTERNS"}


def list_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return [r[0] for r in rows if r[0] not in ADAPTER_TABLES]


def describe_columns(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    cols = []
    for row in conn.execute(f"PRAGMA table_info({table})").fetchall():
        cols.append({
            "name": row[1],
            "type": row[2],
            "notnull": bool(row[3]),
            "default": row[4],
            "pk": bool(row[5]),
        })
    return cols


def foreign_keys(conn: sqlite3.Connection, table: str) -> list[dict[str, str]]:
    fks = []
    for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
        fks.append({"from": row[3], "to_table": row[2], "to_col": row[4]})
    return fks


def sample_rows(conn: sqlite3.Connection, table: str, n: int = 5) -> list[dict[str, Any]]:
    cur = conn.execute(f"SELECT * FROM {table} ORDER BY RANDOM() LIMIT {n}")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def column_stats(conn: sqlite3.Connection, table: str, col: str) -> dict[str, Any]:
    """Cardinality and a few example values — clues for what the column means."""
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        distinct = conn.execute(f"SELECT COUNT(DISTINCT {col}) FROM {table}").fetchone()[0]
        examples = [
            r[0] for r in conn.execute(
                f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL LIMIT 5"
            ).fetchall()
        ]
        return {"rows": total, "distinct": distinct, "examples": examples}
    except sqlite3.Error:
        return {"rows": 0, "distinct": 0, "examples": []}


def build_dossier(db_path: Path | None = None) -> dict[str, Any]:
    db_path = db_path or DB_PATH
    conn = sqlite3.connect(db_path)
    try:
        dossier = {"database": str(db_path), "tables": {}, "fk_edges": []}
        for tbl in list_tables(conn):
            cols = describe_columns(conn, tbl)
            for col in cols:
                col["stats"] = column_stats(conn, tbl, col["name"])
            dossier["tables"][tbl] = {
                "columns": cols,
                "samples": sample_rows(conn, tbl, n=5),
                "row_count": conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0],
            }
            for fk in foreign_keys(conn, tbl):
                dossier["fk_edges"].append({"from_table": tbl, **fk})
        return dossier
    finally:
        conn.close()


def main() -> None:
    dossier = build_dossier()
    out_path = ROOT / "context" / "dossier.json"
    out_path.write_text(json.dumps(dossier, indent=2, default=str))
    print(f"Wrote dossier with {len(dossier['tables'])} tables and {len(dossier['fk_edges'])} FK edges -> {out_path}")


if __name__ == "__main__":
    main()
