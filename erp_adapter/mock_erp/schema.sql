-- Deliberately messy ERP schema modeled on SAP-style real-world ugliness.
-- Cryptic field names, denormalized stock, custom Z-tables, split header/line txns.
-- This is what the discovery layer has to make sense of.

-- =========================================================================
-- MASTER DATA
-- =========================================================================

-- Material master (items). MATNR=material number, MAKTX=description, MTART=type.
CREATE TABLE MAT_MASTER (
    MATNR       TEXT PRIMARY KEY,
    MAKTX       TEXT NOT NULL,
    MTART       TEXT,                -- ROH=raw, HALB=semi, FERT=finished, HAWA=trade
    MEINS       TEXT DEFAULT 'EA',   -- base unit of measure
    NTGEW       REAL,                -- net weight kg
    Z_FRAGILE   INTEGER DEFAULT 0,   -- custom field added by customer
    Z_HAZMAT    INTEGER DEFAULT 0,   -- custom field
    ERSDA       TEXT,                -- created on
    ERNAM       TEXT                 -- created by
);

-- Vendors. LIFNR=vendor number, NAME1=name.
CREATE TABLE LFA1 (
    LIFNR       TEXT PRIMARY KEY,
    NAME1       TEXT NOT NULL,
    LAND1       TEXT,                -- country
    Z_RATING    TEXT,                -- A/B/C custom rating
    SPERR       INTEGER DEFAULT 0    -- 1=blocked
);

-- Plants/warehouses. WERKS=plant code.
CREATE TABLE T001W (
    WERKS       TEXT PRIMARY KEY,
    NAME1       TEXT NOT NULL,
    LAND1       TEXT,
    Z_REGION    TEXT
);

-- =========================================================================
-- STOCK (split across THREE tables — this is the "where is it stored" demo)
-- =========================================================================

-- Plant-level rollup. Updated by triggers from BIN_DETAIL.
-- LABST=unrestricted stock, INSME=quality inspection, RETME=blocked/returns.
CREATE TABLE WH_STOCK (
    MATNR       TEXT,
    WERKS       TEXT,
    LABST       REAL DEFAULT 0,
    INSME       REAL DEFAULT 0,
    RETME       REAL DEFAULT 0,
    PRIMARY KEY (MATNR, WERKS),
    FOREIGN KEY (MATNR) REFERENCES MAT_MASTER(MATNR),
    FOREIGN KEY (WERKS) REFERENCES T001W(WERKS)
);

-- Bin-level detail. The "true" stock — WH_STOCK is just a sum.
CREATE TABLE BIN_DETAIL (
    MATNR       TEXT,
    WERKS       TEXT,
    LGORT       TEXT,                -- storage location
    BIN_CODE    TEXT,                -- aisle-rack-shelf
    QTY         REAL DEFAULT 0,
    Z_STATUS    TEXT DEFAULT 'OK',   -- OK/QI/BLK
    PRIMARY KEY (MATNR, WERKS, LGORT, BIN_CODE)
);

-- Reservations / soft-allocated stock. Custom Z-table.
-- This is the gotcha: "available" stock = LABST - sum(Z_RESERVED.QTY).
CREATE TABLE Z_RESERVED (
    RES_ID      INTEGER PRIMARY KEY AUTOINCREMENT,
    MATNR       TEXT,
    WERKS       TEXT,
    QTY         REAL,
    REF_DOC     TEXT,                -- references SO/PO/transfer doc
    CREATED_AT  TEXT,
    EXPIRES_AT  TEXT
);

-- =========================================================================
-- TRANSACTIONS (header/line split, GL postings separate)
-- =========================================================================

-- AP invoice header. BELNR=document number, BUKRS=company code.
CREATE TABLE AP_HEAD (
    BELNR       TEXT PRIMARY KEY,
    BUKRS       TEXT,
    LIFNR       TEXT,                -- vendor
    BLDAT       TEXT,                -- document date
    WAERS       TEXT DEFAULT 'USD',  -- currency
    WRBTR       REAL,                -- gross amount
    STATUS      TEXT DEFAULT 'PARK', -- PARK=parked, POST=posted, APPR=approved, REJ
    APPROVER    TEXT,
    APPROVED_AT TEXT,
    CREATED_BY  TEXT,
    CREATED_AT  TEXT,
    FOREIGN KEY (LIFNR) REFERENCES LFA1(LIFNR)
);

-- AP invoice lines.
CREATE TABLE AP_LINES (
    BELNR       TEXT,
    POSNR       INTEGER,             -- line item number
    MATNR       TEXT,
    WERKS       TEXT,
    MENGE       REAL,                -- quantity
    NETPR       REAL,                -- net price per unit
    NETWR       REAL,                -- line net amount
    PRIMARY KEY (BELNR, POSNR),
    FOREIGN KEY (BELNR) REFERENCES AP_HEAD(BELNR)
);

-- GL journal entries — every posting hits here too. Yes, double-bookkeeping.
CREATE TABLE GL_ENTRIES (
    GL_ID       INTEGER PRIMARY KEY AUTOINCREMENT,
    BELNR       TEXT,                -- source document
    HKONT       TEXT,                -- GL account
    BUKRS       TEXT,
    DMBTR       REAL,                -- amount in local currency
    SHKZG       TEXT,                -- S=debit, H=credit
    BLDAT       TEXT,
    POSTED_AT   TEXT
);

-- Stock movement document (transfers, GR, GI). MBLNR=material doc number.
CREATE TABLE MSEG (
    MBLNR       TEXT,
    ZEILE       INTEGER,             -- line
    BWART       TEXT,                -- movement type (101=GR, 311=transfer, 261=consumption)
    MATNR       TEXT,
    WERKS_FROM  TEXT,
    WERKS_TO    TEXT,
    LGORT_FROM  TEXT,
    LGORT_TO    TEXT,
    MENGE       REAL,
    POSTED_BY   TEXT,
    POSTED_AT   TEXT,
    PRIMARY KEY (MBLNR, ZEILE)
);

-- =========================================================================
-- APPROVALS (custom Z-table, derived rules)
-- =========================================================================

-- Approval chain rules. The shape of these is exactly what the learner
-- should be able to reconstruct/refine from observed approvals.
CREATE TABLE Z_APPR_RULES (
    RULE_ID     INTEGER PRIMARY KEY AUTOINCREMENT,
    DOC_TYPE    TEXT,                -- AP_INVOICE, TRANSFER, etc.
    MIN_AMT     REAL,
    MAX_AMT     REAL,
    APPROVER_ROLE TEXT,              -- AUTO, MANAGER, CFO
    ACTIVE      INTEGER DEFAULT 1
);

-- Users with roles.
CREATE TABLE USERS (
    USER_ID     TEXT PRIMARY KEY,
    USER_NAME   TEXT,
    ROLE        TEXT                 -- CLERK, MANAGER, CFO
);

-- =========================================================================
-- LEARNING SUBSTRATE (used by our adapter, not by the ERP itself)
-- =========================================================================

-- Every MCP tool call gets logged here. The learner mines this.
CREATE TABLE WORKFLOW_OBS (
    OBS_ID      INTEGER PRIMARY KEY AUTOINCREMENT,
    TS          TEXT,
    ACTOR       TEXT,                -- user or 'agent'
    TOOL        TEXT,                -- MCP tool name called
    ARGS_JSON   TEXT,                -- arguments
    RESULT_JSON TEXT,                -- what came back
    OUTCOME     TEXT,                -- OK / DENIED / FEEDBACK
    FEEDBACK    TEXT                 -- free text correction from human
);

-- Patterns the learner has codified (after seeing N observations).
CREATE TABLE LEARNED_PATTERNS (
    PATTERN_ID  INTEGER PRIMARY KEY AUTOINCREMENT,
    KIND        TEXT,                -- ROUTING / APPROVAL / PREFERENCE / ALIAS
    KEY         TEXT,                -- e.g. "fragile_item_source_warehouse"
    VALUE_JSON  TEXT,
    SUPPORT     INTEGER,             -- how many observations back this
    CONFIDENCE  REAL,
    LEARNED_AT  TEXT
);
