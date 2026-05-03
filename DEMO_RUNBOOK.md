# Coat — demo runbook

The 4-to-5-minute spine for the recorded demo, scene by scene. Each
scene names the infrastructure it depends on so the build plan is
traceable to what lands on screen.

The recording is async, so retakes are free. The point is not heroic
liveness — it is a tight, opinionated story that lands in under five
minutes.

The whole thing runs in VS Code with Claude Code open and the project
loaded. Split-pane: editor left, terminal right.

---

## Scene 1 — Configure Coat into the workflow (employee perspective) — ~45s

**On screen:** terminal in `~/coatmvp`. Run `coat init`. The CLI
walks through: discover SAP-shaped schema in `mock_erp/erp.db`,
introspect, ask Claude to label semantically, write `context.yaml`.
Output is a clean banner with three lines per concept: name, table
backing it, structural confidence.

**Voice:** *"This is what a new customer sees on day one. We point Coat
at their ERP. We don't ask for a schema map. We don't ask for an
ontology. The discovery layer reads the database directly and figures
out which physical tables back which business concepts. Ten minutes
of compute, no consultant."*

**Infrastructure required:**
- `coat init` CLI wrapping the existing `run.py` pipeline with cleaner
  output. *To build — ~50 lines.*
- Existing `discovery/introspect.py` + `semantic_map.py`. *Works today.*
- Confidence score per concept added to `context.yaml`. *To build — see scene 2.*

---

## Scene 2 — First-pass confidence map (why we beat Glean) — ~60s

**On screen:** the rendered concept catalog. Each concept shows its
confidence as a structural score, *not* a relevance score:

```
DISCOVERED CONCEPTS  (acme demo • 14:11:08 UTC)

CONCEPT                     TABLE       CONFIDENCE  EVIDENCE
item                        MAT_MASTER  ████░ 0.94  PK pattern, FK target from 3 tables, name shape, custom Z-flags
vendor                      LFA1        ████░ 0.91  PK pattern, FK target from AP_HEAD, country distribution
warehouse                   T001W       ███░░ 0.78  3 distinct rows, FK target from stock tables
stock_by_warehouse          WH_STOCK    █████ 0.99  composite PK, joins to item+warehouse, numeric LABST/INSME/RETME
stock_by_bin                BIN_DETAIL  █████ 0.97  finer-grained than WH_STOCK, status enum OK/QI/BLK
reservation                 Z_RESERVED  ███░░ 0.71  custom Z-table, lacks FK; inferred from column overlap with stock_by_warehouse
ap_invoice_header           AP_HEAD     █████ 0.96  status enum PARK/APPR/POST/REJ, vendor FK, paired with AP_LINES
ap_invoice_line             AP_LINES    █████ 0.98  composite PK with AP_HEAD, line-item shape
gl_entry                    GL_ENTRIES  ████░ 0.89  S/H debit-credit pattern, BELNR FK
stock_movement              MSEG        ████░ 0.86  BWART movement-type codes, item+warehouse refs
approval_rule               Z_APPR_RULES  ███░ 0.74  custom Z-table, bracketed amount ranges
user                        USERS       █████ 0.99  role enum CLERK/MANAGER/CFO

Derived view: available_stock = stock_by_warehouse.unrestricted - sum(active reservations)
```

**Voice:** *"This is not a list of guesses. The confidence is
structural — we tested each hypothesis against the data. Foreign-key
overlap, distribution shape, custom-flag patterns, FK targeting from
related tables. Glean is enterprise search. It indexes documents and
ranks them by relevance. That's not what an ERP needs. Coat doesn't
search the schema, it verifies the schema."*

**Why this beats Glean specifically:**
| | Glean | Coat |
|---|---|---|
| Surface | Read-only search over indexed text | Action-capable on structured ERP data |
| Confidence | Semantic-similarity relevance | Structural verification — testable hypotheses against the data |
| Permissions | Inherits source-system ACLs | Learns who can do what; ratified before enforcing |
| Output | Documents, snippets | Tool calls, posted transactions, audit chain |
| Where it sits | Indexes ERP exports, generic | At the change boundary, ERP-native |

**Infrastructure required:**
- Confidence-scoring function added to `discovery/semantic_map.py`.
  Tests run against the dossier: column-name shape, sample-value
  distribution, FK consistency, cardinality. *To build — ~80 lines.*
- Output rendering with confidence bars. *To build — small extension to `agent/demo.py` or new `cli/render_concepts.py`. ~30 lines.*

---

## Scene 3 — Workflow query → pattern emerges in real time — ~60s

**On screen:** split terminal. Left pane simulates "the SAP side" — an
employee posts an invoice through what looks like a SAP transaction
(no Coat involved):

```
$ sap-rails post-invoice --vendor V1001 --amount 2500 --as u_clerk_b
SAP DOC 4900000178 created.  vendor=V1001  amount=$2,500  status=PARK
$ sap-rails approve --doc 4900000178 --as u_mgr_c
SAP DOC 4900000178 approved by u_mgr_c.  status=POST
```

Right pane is `coat watch`, streaming the change boundary. As each
SAP transaction lands, Coat sees the row change, normalizes it,
writes a `WORKFLOW_OBS` row, and the learner re-evaluates patterns:

```
[14:12:01] cdc.event       ap_invoice_header  INV-… PARK  actor=u_clerk_b
[14:12:01] obs.captured    workflow_obs#52    invoice.create
[14:12:03] cdc.event       ap_invoice_header  INV-… POST  actor=u_mgr_c
[14:12:03] obs.captured    workflow_obs#53    invoice.approve
[14:12:03] learner.tick
[14:12:03] pattern         APPROVAL  v=V1001 ≤ $4,260 → u_mgr_c
                              status: trial → enforced (ratification queued)
                              support: 11→12   confidence: 1.00
```

**Voice:** *"The employee is using SAP. They're not using Coat.
They're not even thinking about Coat. Coat is sitting at the change
boundary, not in the middle of their workflow. Every row that
changes flows into the observation log, the learner picks it up, and
patterns emerge from real activity. No polling. No 'please send us
your data.' We see what the ERP already emits."*

**Infrastructure required:**
- `sap_rails/` module: a small Python CLI that bypasses Coat's
  adapter and writes directly to AP_HEAD / MSEG, mimicking a SAP
  transaction. *To build — ~120 lines.*
- A SQLite trigger or a small `bridge/` watcher that detects writes
  outside Coat's adapter and emits a normalized `ChangeEvent` into
  `WORKFLOW_OBS`. *To build — ~60 lines (triggers route).*
- `coat watch` CLI that tails `WORKFLOW_OBS` and the learner tick,
  rendered with `rich`. *To build — ~80 lines.*
- Existing `learner/miner.py`. *Works today.*

---

## Scene 4 — External agent predicts inventory under explicit permissions — ~75s

**On screen:** Claude Code in the editor pane. A new file opens —
`agents/forecast_inventory.py`. The script declares its identity and
requested scopes via Coat's MCP, gets a session, and runs.

```python
# Forecasting agent — predicts next-week stockouts
# Uses external weather + sales-trend data + Coat's stock + movement history
# Requested scopes: coat:concepts:read, coat:inventory:read, coat:patterns:read
```

Run it. Coat's handshake response prints (granted scope set, restricted
tool catalog, audit_id), then the agent fetches `get_stock` for the
top-10 SKUs, fetches `MSEG` movement history through Coat's adapter,
joins synthetic external data (a CSV of weather + sales-trend), and
predicts:

```
INVENTORY FORECAST — week of 2026-05-10
SKU       wh    on_hand  predicted_demand  stockout_risk
SKU-441   WH01   180     ~210              HIGH      ← reorder ≥30 by Friday
SKU-200   WH02    95     ~110              MEDIUM
SKU-300   WH02   240     ~180              LOW
…

Note: forecasting agent has NO access to invoice posting (coat:invoice:* not granted).
      Tried to call post_invoice for replenishment PO — denied at protocol layer.
```

The "denied" line is the money shot. The same agent that just produced
a useful forecast cannot post a transaction it wasn't authorized for.
The protocol enforced that the moment the agent connected.

**Voice:** *"Now plug in an external agent. It's a forecaster. It
needs Coat's inventory data plus things Coat doesn't have — weather,
external sales trends. The incumbents won't let you bring in
external context like this. Their AI is filtered by their access
controls and locked to their data. Ours plugs into Coat over MCP.
And — watch — when this agent tries to post an invoice for the
reorder, it's denied at the protocol layer because its scopes don't
include `coat:invoice:post`. Permissions are enforced at handshake,
not vibes."*

**Infrastructure required:**
- `agents/forecast_inventory.py` — the forecasting agent script.
  *To build — ~150 lines (uses adapter directly + a synthetic external
  data CSV).*
- Stub of the agent protocol's handshake output: when an agent
  connects, the adapter emits a "session opened" line with granted
  scopes and the restricted tool catalog. Full crypto handshake not
  required for the demo — the *visibility* of granted scopes is what
  lands. *To build — ~50 lines extension to `mcp_server/server.py`.*
- An explicit `cap.denied` path in tool dispatch when an agent calls
  a tool outside its granted scope. *To build — ~30 lines.*
- A small synthetic external-data CSV — `data/external/weather_sales.csv`. *To build — trivial.*

---

## Scene 5 — Permissions, security, and the path to compliance — ~30s

**On screen:** `coat audit --entity SKU-441 --since 1h`. The terminal
prints a clean entity timeline showing every event: who did what,
under what capability, derived from which pattern, ratified by whom:

```
ENTITY TIMELINE — item / SKU-441
14:11:42 obs.read         get_stock           agent=forecast@coat.io/v1
                                              scope=coat:inventory:read
                                              audit_id=aud_01HV3M…
14:12:01 obs.write        move_stock 80 WH02→WH01
                                              actor=u_clerk_b (via SAP rails)
                                              capability_origin=cdc (human action)
14:12:03 cap.denied       post_invoice        agent=forecast@coat.io/v1
                                              reason="scope coat:invoice:post not granted"
                                              audit_id=aud_01HV3M…
```

**Voice:** *"Every action chains back through the capability that
authorized it, the pattern that derived the capability, the
observations that backed the pattern, the human who ratified it.
Provenance is everything. This is the audit chain a finance auditor
or a security reviewer needs. SOC 2 next quarter, GDPR DPA on day
one, SOX-aware controls in the architecture from the start. The
detailed roadmap is in COMPLIANCE.md."*

**Infrastructure required:**
- `coat audit --entity X --since T` CLI. *To build — ~80 lines, queries `WORKFLOW_OBS`.*
- `COMPLIANCE.md` for the verbal claim to point at. *Written in this push.*

---

## Scene 6 — Full circle — ~20s

**On screen:** the pattern catalog from scene 2 again. The new
`vendor_fast_track` pattern from scene 3 is now at status `trial` →
ready for ratification.

**Voice:** *"One employee posted one invoice. Coat saw it on the
change boundary. Mined a candidate pattern. Surfaced it for
ratification. An external agent ran an action on the same data,
under a different capability, denied where appropriate, audited end
to end. Same Coat layer. Different agents. Different ERPs tomorrow.
Walls don't matter."*

**Infrastructure required:**
- Pattern catalog renderer (already in scene 2). *Reuse.*

---

## Build map — what has to exist for the recording

Eight pieces of code, each small, each independently testable.
Sized rough.

| # | Piece | Scene(s) | Size |
|---|-------|----------|------|
| 1 | Confidence-scoring in `discovery/semantic_map.py` | 1, 2 | ~80 lines |
| 2 | `cli/render_concepts.py` (catalog with confidence bars) | 1, 2 | ~30 lines |
| 3 | `coat init` CLI wrapper around `run.py` | 1 | ~50 lines |
| 4 | `sap_rails/` direct-write SAP simulator | 3 | ~120 lines |
| 5 | SQLite triggers / bridge watcher emitting CDC into `WORKFLOW_OBS` | 3 | ~60 lines |
| 6 | `coat watch` live-stream CLI | 3, 6 | ~80 lines |
| 7 | `agents/forecast_inventory.py` + synthetic external CSV | 4 | ~150 lines + data |
| 8 | Visible-handshake + `cap.denied` in `mcp_server/server.py` | 4, 5 | ~80 lines |
| 9 | `coat audit --entity` CLI | 5 | ~80 lines |

Total: roughly 700 lines of new code, plus a small CSV fixture. All
additive, none of it requires changes to the core adapter or
discovery beyond the confidence-scoring extension.

---

## Recording order

Build everything, then record once with multiple takes. Recording
order is not necessarily build order — record scene 2 first (concept
map is the highest-stakes still frame), then scenes 1 → 3 → 4 → 5 →
6. Then cut.

---

## Status at the time of writing

Built today (works in your repo right now):
- Discovery + semantic map (without confidence scoring yet)
- Adapter, MCP tool surface, learner, scripted demo
- Pattern catalog visible via `python run.py inspect`

Not yet built (the list above):
- Confidence scoring on the concept map
- SAP-rails simulator + CDC bridge
- External forecasting agent
- Visible handshake + `cap.denied`
- `coat init`, `coat watch`, `coat audit` CLIs

Next move: greenlight the build, pick scene-1-and-2 as the first
slice (confidence-scored context map), and ship that today. Scene 3
and 4 come tomorrow. Recording on day three.
