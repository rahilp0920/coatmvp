"""Seed the mock ERP with realistic messy data + a backlog of historical
workflow observations so the live learner has something to mine on day one."""
from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "erp.db"
SCHEMA_PATH = ROOT / "mock_erp" / "schema.sql"

random.seed(42)


def now_iso(offset_days: int = 0) -> str:
    return (datetime(2026, 5, 1) + timedelta(days=offset_days)).isoformat(timespec="seconds")


def reset_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_PATH.read_text())
    return conn


def seed_masters(c: sqlite3.Cursor) -> None:
    # Plants
    plants = [
        ("WH01", "Newark Distribution Center", "US", "EAST"),
        ("WH02", "Reno Fulfillment", "US", "WEST"),
        ("WH03", "Rotterdam Hub", "NL", "EU"),
    ]
    c.executemany("INSERT INTO T001W VALUES (?,?,?,?)", plants)

    # Vendors
    vendors = [
        ("V1001", "Acme Components Inc.", "US", "A", 0),
        ("V1002", "Globex Industrial", "US", "B", 0),
        ("V1003", "Initech Supply", "US", "A", 0),
        ("V1004", "Hooli Materials Ltd", "GB", "C", 0),
        ("V1005", "Soylent Logistics", "US", "B", 1),  # blocked
    ]
    c.executemany("INSERT INTO LFA1 VALUES (?,?,?,?,?)", vendors)

    # Materials — mix of fragile, hazmat, normal
    materials = [
        # MATNR, MAKTX, MTART, MEINS, NTGEW, Z_FRAGILE, Z_HAZMAT
        ("SKU-100", "Industrial Bearing 6204-2RS",   "HAWA", "EA",  0.10, 0, 0),
        ("SKU-101", "Steel Shaft 25mm x 500mm",      "HALB", "EA",  2.50, 0, 0),
        ("SKU-200", "Glass Display Panel 24in",      "HAWA", "EA",  4.20, 1, 0),
        ("SKU-201", "Ceramic Capacitor Reel 1uF",    "ROH",  "EA",  0.05, 1, 0),
        ("SKU-300", "Lithium Cell 18650 3.7V",       "HAWA", "EA",  0.04, 0, 1),
        ("SKU-301", "Solvent Drum 200L",             "ROH",  "DR", 220.0, 0, 1),
        ("SKU-400", "Packaging Box L",               "HAWA", "EA",  0.30, 0, 0),
        ("SKU-441", "Printed Circuit Board Rev-C",   "HALB", "EA",  0.08, 1, 0),
        ("SKU-500", "Finished Assembly Model X",     "FERT", "EA",  3.10, 1, 0),
    ]
    for m in materials:
        c.execute(
            "INSERT INTO MAT_MASTER (MATNR,MAKTX,MTART,MEINS,NTGEW,Z_FRAGILE,Z_HAZMAT,ERSDA,ERNAM)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (*m, now_iso(-365), "system"),
        )

    # Users
    users = [
        ("u_clerk_a", "Alice Chen",   "CLERK"),
        ("u_clerk_b", "Bob Diaz",     "CLERK"),
        ("u_mgr_c",   "Carol Singh",  "MANAGER"),
        ("u_cfo",     "Dana Park",    "CFO"),
    ]
    c.executemany("INSERT INTO USERS VALUES (?,?,?)", users)

    # Approval rules (intentionally INCOMPLETE — agent will surface gaps)
    rules = [
        ("AP_INVOICE", 0,     1000.0,   "AUTO",    1),
        ("AP_INVOICE", 1000,  10000.0,  "MANAGER", 1),
        ("AP_INVOICE", 10000, 1e9,      "CFO",     1),
        ("TRANSFER",   0,     5000.0,   "AUTO",    1),
        ("TRANSFER",   5000,  1e9,      "MANAGER", 1),
    ]
    c.executemany("INSERT INTO Z_APPR_RULES (DOC_TYPE,MIN_AMT,MAX_AMT,APPROVER_ROLE,ACTIVE) VALUES (?,?,?,?,?)", rules)


def seed_stock(c: sqlite3.Cursor) -> None:
    """Stock distributed across bins, with WH_STOCK as a rollup. Some Z_RESERVED."""
    materials = [r[0] for r in c.execute("SELECT MATNR FROM MAT_MASTER").fetchall()]
    fragile = {r[0] for r in c.execute("SELECT MATNR FROM MAT_MASTER WHERE Z_FRAGILE=1").fetchall()}
    plants = ["WH01", "WH02", "WH03"]
    storage_locs = ["MAIN", "AUX"]

    rollups: dict[tuple[str, str], float] = {}
    for matnr in materials:
        for werks in plants:
            # Fragile items always stocked at WH02 (the learned-routing destination).
            # Non-fragile: 25% chance of being absent at a given plant.
            if matnr not in fragile and random.random() < 0.25:
                continue
            if matnr in fragile and werks != "WH02" and random.random() < 0.20:
                continue
            total = 0.0
            for lgort in storage_locs:
                for bin_idx in range(random.randint(1, 3)):
                    qty = round(random.uniform(20, 400), 0)
                    bin_code = f"{lgort[0]}{random.randint(1,5):02d}-{random.randint(1,9)}-{random.randint(1,4)}"
                    status = random.choices(["OK", "QI", "BLK"], weights=[85, 10, 5])[0]
                    c.execute(
                        "INSERT OR IGNORE INTO BIN_DETAIL VALUES (?,?,?,?,?,?)",
                        (matnr, werks, lgort, bin_code, qty, status),
                    )
                    if status == "OK":
                        total += qty
            rollups[(matnr, werks)] = total

    for (matnr, werks), labst in rollups.items():
        # Random splits between LABST/INSME/RETME
        ins = round(labst * random.uniform(0, 0.05), 0)
        ret = round(labst * random.uniform(0, 0.02), 0)
        c.execute(
            "INSERT INTO WH_STOCK (MATNR,WERKS,LABST,INSME,RETME) VALUES (?,?,?,?,?)",
            (matnr, werks, labst - ins - ret, ins, ret),
        )

    # Some active reservations
    reserved = [
        ("SKU-441", "WH01", 50,  "SO-2025-1188", now_iso(-3), now_iso(7)),
        ("SKU-441", "WH02", 100, "SO-2025-1190", now_iso(-1), now_iso(14)),
        ("SKU-500", "WH02", 30,  "SO-2025-1195", now_iso(0),  now_iso(5)),
        ("SKU-200", "WH01", 20,  "SO-2025-1170", now_iso(-7), now_iso(0)),  # expired-ish
    ]
    c.executemany(
        "INSERT INTO Z_RESERVED (MATNR,WERKS,QTY,REF_DOC,CREATED_AT,EXPIRES_AT) VALUES (?,?,?,?,?,?)",
        reserved,
    )


def seed_history(c: sqlite3.Cursor) -> None:
    """A backlog of past invoices, stock movements, and workflow observations.
    The learner mines WORKFLOW_OBS to derive routing preferences and approval
    patterns, so we plant evidence for a few learnable signals:
      - Fragile items (SKU-200, SKU-441, SKU-500) ship from WH02 preferentially.
      - Vendor V1001 invoices always get auto-approved by mgr_c (skip rule).
    """
    # Past AP invoices
    invoices = [
        ("INV-90001", "1000", "V1001", -45,  "USD",   850.0,  "POST", "u_mgr_c", -44, "u_clerk_a"),
        ("INV-90002", "1000", "V1002", -40,  "USD",  4200.0,  "POST", "u_mgr_c", -39, "u_clerk_a"),
        ("INV-90003", "1000", "V1001", -30,  "USD",   620.0,  "POST", "u_mgr_c", -30, "u_clerk_b"),
        ("INV-90004", "1000", "V1003", -20,  "USD", 12500.0,  "POST", "u_cfo",   -18, "u_clerk_a"),
        ("INV-90005", "1000", "V1001", -15,  "USD",  1800.0,  "POST", "u_mgr_c", -14, "u_clerk_b"),
        ("INV-90006", "1000", "V1004", -10,  "GBP",  3300.0,  "POST", "u_mgr_c",  -9, "u_clerk_a"),
        ("INV-90007", "1000", "V1001",  -5,  "USD",  2100.0,  "POST", "u_mgr_c",  -5, "u_clerk_a"),
    ]
    for inv in invoices:
        c.execute(
            "INSERT INTO AP_HEAD (BELNR,BUKRS,LIFNR,BLDAT,WAERS,WRBTR,STATUS,APPROVER,APPROVED_AT,CREATED_BY,CREATED_AT)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (inv[0], inv[1], inv[2], now_iso(inv[3]), inv[4], inv[5], inv[6], inv[7], now_iso(inv[8]), inv[9], now_iso(inv[3])),
        )
        # GL postings — debit expense, credit AP
        c.execute(
            "INSERT INTO GL_ENTRIES (BELNR,HKONT,BUKRS,DMBTR,SHKZG,BLDAT,POSTED_AT) VALUES (?,?,?,?,?,?,?)",
            (inv[0], "510000", inv[1], inv[5], "S", now_iso(inv[3]), now_iso(inv[8])),
        )
        c.execute(
            "INSERT INTO GL_ENTRIES (BELNR,HKONT,BUKRS,DMBTR,SHKZG,BLDAT,POSTED_AT) VALUES (?,?,?,?,?,?,?)",
            (inv[0], "211000", inv[1], inv[5], "H", now_iso(inv[3]), now_iso(inv[8])),
        )

    # Past stock movements — fragile items preferentially sourced from WH02
    fragile = ["SKU-200", "SKU-441", "SKU-500"]
    nonfragile = ["SKU-100", "SKU-101", "SKU-300", "SKU-400"]
    mblnr_seq = 70000
    for day in range(-60, 0, 2):
        # Two transfers per cycle
        for _ in range(2):
            mblnr_seq += 1
            mat = random.choice(fragile + nonfragile)
            if mat in fragile:
                from_wh, to_wh = "WH02", random.choice(["WH01", "WH03"])
            else:
                from_wh, to_wh = random.choice([("WH01","WH02"),("WH02","WH01"),("WH01","WH03")])
            c.execute(
                "INSERT INTO MSEG VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"DOC{mblnr_seq}", 1, "311", mat, from_wh, to_wh, "MAIN", "MAIN",
                 round(random.uniform(20, 100), 0), random.choice(["u_clerk_a","u_clerk_b"]),
                 now_iso(day)),
            )

    # Synthetic workflow observations — historical agent/user actions.
    # The learner will mine these to seed LEARNED_PATTERNS.
    obs = []
    for day in range(-60, 0, 1):
        # Source-warehouse choices for fragile items: 90% WH02
        if random.random() < 0.6:
            mat = random.choice(fragile)
            chose_wh = "WH02" if random.random() < 0.9 else "WH01"
            obs.append((
                now_iso(day), random.choice(["u_clerk_a","u_clerk_b"]),
                "move_stock",
                json.dumps({"matnr": mat, "qty": 30, "to_warehouse": "WH01"}),
                json.dumps({"chosen_source": chose_wh, "ok": True}),
                "OK", None,
            ))
        # Approvals for V1001 — manager fast-tracks
        if random.random() < 0.3:
            obs.append((
                now_iso(day), "u_mgr_c",
                "approve_invoice",
                json.dumps({"vendor": "V1001", "amount": round(random.uniform(200, 4000),2)}),
                json.dumps({"approved": True, "latency_sec": random.randint(30, 240)}),
                "OK", None,
            ))
    c.executemany(
        "INSERT INTO WORKFLOW_OBS (TS,ACTOR,TOOL,ARGS_JSON,RESULT_JSON,OUTCOME,FEEDBACK) VALUES (?,?,?,?,?,?,?)",
        obs,
    )


def main() -> None:
    conn = reset_db()
    c = conn.cursor()
    seed_masters(c)
    seed_stock(c)
    seed_history(c)
    conn.commit()

    # Sanity counts
    counts = {}
    for tbl in ("MAT_MASTER","LFA1","T001W","WH_STOCK","BIN_DETAIL","Z_RESERVED",
                "AP_HEAD","AP_LINES","GL_ENTRIES","MSEG","Z_APPR_RULES",
                "USERS","WORKFLOW_OBS","LEARNED_PATTERNS"):
        counts[tbl] = c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    conn.close()
    print(f"Seeded {DB_PATH}")
    for tbl, n in counts.items():
        print(f"  {tbl:20s} {n}")


if __name__ == "__main__":
    main()
