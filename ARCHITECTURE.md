# Coat — architecture

This document describes how Coat dynamically structures **context**,
**tasks**, and **agent interfaces** so that an autonomous agent — and any
plugin sitting next to it — can drive an unfamiliar ERP without ever
seeing the underlying schema.

The four ideas that matter:

1. **Discovery** turns an unknown ERP into a typed, business-keyed map.
2. **Adapter** translates business-concept calls into whatever the
   underlying ERP needs, and is the sole write path.
3. **Tool surface** is the single MCP-shaped contract that every agent
   and every plugin consumes.
4. **Learner** turns the audit log of past tool calls back into rules
   that bias the next decision.

Each is independently extensible.

---

## 1. Dynamic context (`discovery/`, `context/`)

When Coat is first pointed at an ERP, two scripts run, in order:

`discovery/introspect.py` walks the database and produces
`context/dossier.json` — a structured dump of every table: column types,
nullability, primary and foreign keys, a small set of sampled rows, and
per-column cardinality and example values. Adapter-owned tables
(`WORKFLOW_OBS`, `LEARNED_PATTERNS`) are excluded — they are Coat's, not
the ERP's.

`discovery/semantic_map.py` hands the dossier to Claude with a prompt
that asks for a structured JSON labeling: which physical table is the
`item` master, which columns play which roles (`id`, `name`, `qty`,
`status`, etc.), what foreign-key edges exist between concepts, and
which views need to be derived (e.g. `available_stock = LABST −
active reservations`). When `ANTHROPIC_API_KEY` is unset, a curated
fallback mapping is used so the demo runs offline.

The output is `context/context.yaml`, the **single source of truth for
how this company's ERP is shaped**. It looks like:

```yaml
concept_map:
  item:
    table: MAT_MASTER
    columns: { id: MATNR, name: MAKTX, fragile_flag: Z_FRAGILE, … }
    notes: "Custom Z_FRAGILE / Z_HAZMAT flags drive routing rules."
  stock_by_warehouse:
    table: WH_STOCK
    columns: { item: MATNR, warehouse: WERKS, unrestricted: LABST, … }
    notes: "Rollup of BIN_DETAIL.QTY where Z_STATUS='OK'. Stale relative to bins."
  …
join_paths:
  - { from: stock_by_warehouse, to: item, via: "WH_STOCK.MATNR = MAT_MASTER.MATNR" }
  …
derived_views:
  - name: available_stock
    definition: "Unrestricted stock minus active reservations."
    sql_hint: "SELECT s.MATNR, s.WERKS, s.LABST - …"
```

Pointing Coat at a different company's ERP regenerates this file.
Everything downstream — the adapter, the tool catalog, the agent prompt
— reads it at startup. Nothing else has to change.

### Adding a concept

1. Run discovery against the ERP. Inspect the generated `context.yaml`.
2. If a concept the agent should know about is missing or
   mis-labeled, edit `context.yaml` directly. The format is the
   contract.
3. If the concept needs adapter logic (joins, derived views, filters
   beyond simple column lookups), add a small function to
   `mcp_server/adapter.py` that uses `ConceptResolver` to fetch
   physical names from the context.
4. If you want the concept exposed to the agent as a tool, add an entry
   to `TOOL_SCHEMAS` in `mcp_server/server.py` — see §3 below.

This is the entire extension model for "Coat learns a new business
object."

---

## 2. Adapter — the business-concept boundary

`mcp_server/adapter.py` is the layer that translates between business
concepts and physical SQL. It does three things, every call:

**Resolve the concept.** The `ConceptResolver` class in `adapter.py`
takes a concept name (e.g. `"stock_by_warehouse"`) and returns the
physical table and the column for a given role. The agent code is
written entirely in business roles — `r.col("stock_by_warehouse",
"unrestricted")` returns `"LABST"` here, but on a different ERP it
might return `"AvailableQty"` or `"ON_HAND"`. The function signature
doesn't change.

**Execute the operation.** The adapter is written so that every
non-trivial decision is captured in code, not delegated to the LLM —
e.g. `get_stock` joins `WH_STOCK` (rollup) with `BIN_DETAIL` (truth)
and subtracts active rows from `Z_RESERVED`, returning a single
`available` number. The agent does not know any of those tables exist.

**Log to `WORKFLOW_OBS`.** Every call writes a row containing the actor,
the tool name, the args, a result summary, an outcome
(`OK` / `DENIED` / `FEEDBACK`), and a timestamp. This is the substrate
the learner mines.

The mutation set is intentionally small: `move_stock`, `post_invoice`,
`request_approval`, `submit_feedback`. Read tools (`find_item`,
`get_stock`, `list_pending_invoices`, `suggest_source_warehouse`,
`list_concepts`) cover the rest of the surface. There is no general
SQL-execution tool. See `SECURITY.md` for why.

### Adding a tool

1. Write a function in `adapter.py` that uses `ConceptResolver` to look
   up physical names. Wrap the work in a `with db() as conn:` block so
   transactions commit cleanly. Call `_log_obs` at the end.
2. Register the tool in `mcp_server/server.py` by appending to
   `TOOL_SCHEMAS` (name, description, JSON Schema input) and adding a
   dispatch entry in `call_tool`.
3. If the tool is one the scripted demo or a plugin should call without
   going over MCP, also add it to the `TOOLS` dict in
   `agent/demo.py`.

Three files touched, no schema changes, no agent code changes.

---

## 3. Agent and plugin interface (MCP)

The MCP server in `mcp_server/server.py` is the contract. Every agent
and every plugin sees the same nine tools, with the same JSON-Schema
inputs, regardless of which ERP is behind them.

| Tool | Inputs | Returns |
|------|--------|---------|
| `list_concepts` | — | `{concepts, derived}` |
| `find_item` | `query: string` | `{matches, count}` |
| `get_stock` | `matnr: string` | `{item, by_warehouse[], total_available}` |
| `suggest_source_warehouse` | `matnr, qty` | `{chosen, reason, candidates, related_feedback}` |
| `move_stock` | `matnr, qty, from_warehouse, to_warehouse, reason?` | `{ok, doc, moved, from, to}` |
| `post_invoice` | `vendor, amount, currency?, lines?` | `{ok, doc, status, approval, gl_entries}` |
| `request_approval` | `belnr, decided_by, decision` | `{ok, doc, status, decided_by, gl_entries}` |
| `list_pending_invoices` | — | `{pending[], count}` |
| `submit_feedback` | `obs_id: int, feedback: string` | `{ok, obs_id}` |

Two paths to consume them:

**Live, over MCP.** Run `python -m mcp_server.server` and add an entry
to `~/.config/claude/claude_desktop_config.json` or your Claude Code
MCP config:

```json
{
  "mcpServers": {
    "erp-adapter": {
      "command": "python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/Users/YOU/coatmvp/erp_adapter"
    }
  }
}
```

Any Claude Code session inside the project then has the full tool
catalog available. Slash commands, agents, GSD workflows, custom
plugins — they all see the same nine tools.

**In-process, for orchestrating workflows.** A plugin or scripted
runner that wants to drive multi-step tasks without a transport hop
can import the adapter directly: `from mcp_server import adapter` and
call `adapter.get_stock(...)`, `adapter.post_invoice(...)`. The audit
log and the learner work identically through this path.

### How any plugin composes a workflow

A plugin (Get Shit Done, a custom Claude Code plugin, a scheduled task)
expresses a workflow as a sequence of tool calls. The plugin doesn't
need to know what ERP it's running against; the adapter answers in
business terms, and the plugin can compose:

```
list_concepts → find_item → get_stock → suggest_source_warehouse → move_stock
```

For an invoice-processing plugin:

```
list_concepts → list_pending_invoices → request_approval (per row, with
human-confirmation prompt for amounts above the auto bracket)
```

The plugin contract is *the tool surface*. Adding a new plugin does not
require schema knowledge, does not require new tables, and does not
require touching the adapter unless the plugin needs a brand-new
business operation that no existing tool covers — in which case §2.3
(`Adding a tool`) applies.

### Plugin-visible configuration

The two pieces of configuration that any plugin can introspect at
runtime:

- **`list_concepts`** — returns the live concept map plus derived views.
  A plugin that wants to know whether *this* ERP exposes `vendor` or
  `purchase_order` calls this once on startup and adapts.
- **`LEARNED_PATTERNS`** — a plugin that wants to surface "what has the
  system learned about this vendor?" can read this table directly (or
  through a future `list_patterns` tool) and cite support and
  confidence to the human in the loop.

---

## 4. Learning substrate (`learner/`)

`learner/miner.py` reads `WORKFLOW_OBS` and produces three families of
patterns:

- **ROUTING** — preferred source warehouse for a class of item (today
  fragile vs. non-fragile; trivially extensible to country, weight,
  hazmat, vendor, etc.).
- **APPROVAL** — vendor + amount-bucket fast-tracks. Codified only when
  ≥5 historical approvals exist for the vendor, ≥60% by the same
  manager, and a clear amount ceiling can be inferred.
- **PREFERENCE** — free-text human feedback on a prior observation,
  bucketed by tool, deduplicated by exact text, surfaced back to the
  agent on the next call to that tool.

Patterns are re-derived from scratch every learning cycle — there is no
incremental update — so a corrective feedback row instantly
invalidates a stale rule. The miner runs:

- Every Nth observation (default 10) — keeps overhead bounded during
  normal use.
- Immediately on any `FEEDBACK` row — corrections take effect on the
  next call.
- On demand via `python -m learner.miner` — for a fresh slate after a
  bulk operation.

### Adding a pattern miner

1. Write a function in `miner.py` that takes a `sqlite3.Connection`,
   queries `WORKFLOW_OBS`, and returns a list of pattern dicts of the
   form `{"kind": …, "key": …, "value": …, "support": …, "confidence": …}`.
2. Append the function's call to `run_once`.

The adapter consults patterns by `KIND` — for the new family to
influence decisions, add a corresponding `_learned_patterns(conn,
"YOUR_KIND")` lookup in the relevant adapter function.

---

## End-to-end: what one tool call actually does

A single `suggest_source_warehouse("SKU-441", qty=80)` call flows like:

```
agent → MCP server → adapter.suggest_source_warehouse
                      │
                      ├─ get_stock("SKU-441")
                      │    ├─ ConceptResolver → WH_STOCK, BIN_DETAIL, Z_RESERVED
                      │    ├─ JOIN + reservation subtraction → by_warehouse[]
                      │    └─ _log_obs("get_stock")
                      │
                      ├─ filter candidates where available >= 80
                      │
                      ├─ if item.fragile:
                      │     SELECT FROM LEARNED_PATTERNS WHERE KIND='ROUTING'
                      │     if pattern.confidence >= 0.6 and preferred ∈ candidates:
                      │         chosen = pattern.value.warehouse
                      │         reason = "Learned routing: …"
                      │
                      ├─ collect related PREFERENCE feedback for this matnr
                      │
                      └─ _log_obs("suggest_source_warehouse")
                           if obs_id % 10 == 0 or outcome == FEEDBACK:
                               relearn()  ← may emit new patterns
```

The agent receives `{chosen, reason, candidates, related_feedback}` and
moves on. Nothing in the call required it to know that `WH_STOCK.LABST`
exists, that `Z_RESERVED` is custom, or that fragility is a `Z_FRAGILE`
flag on `MAT_MASTER`.

That decoupling is the entire point. It is what lets the same agent and
the same plugins work against any ERP that Coat has run discovery
against.
