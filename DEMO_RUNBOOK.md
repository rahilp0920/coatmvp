# Coat — demo runbook

The 4-to-5-minute spine for the recorded demo, scene by scene. Each
scene names the infrastructure it depends on so the build plan is
traceable to what lands on screen.

The recording is async, so retakes are free. The point is not heroic
liveness — it is a tight, opinionated story that lands in under five
minutes.

The whole thing runs in VS Code with Claude Code open and the project
loaded. Split-pane: editor left, terminal right.

## Philosophy on screen

**As easy as wearing a coat.** Two ideas the recording has to land:

*Permissions are derived, not declared.* The admin describes the
agent's job in plain English. Coat infers what the agent needs to
read, write, and act on — and surfaces a proposal for human
ratification. Agents don't ask for capabilities; Coat figures out
what they need and the human signs off.

*Context is delivered, not assembled.* The agent does not fetch
from Coat, then fetch from a weather API, then join them in code.
**Coat is the context layer.** External signals — weather, supply
chain news, sanctions lists, market data — are registered with
Coat. The agent calls one tool, gets a fully-assembled,
business-shaped context bundle, and spends its tokens on
*reasoning*, not on plumbing. That's what makes a third-party
agent feel like it has a senior analyst handing it briefings —
because it does, and the analyst is Coat.

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

## Scene 4 — Onboard a specialized external agent in plain English — ~75s

**On screen:** terminal. The admin runs:

```
$ coat agent onboard
```

Coat asks one question: *"What does this agent do? Describe it like
you'd describe it to a new hire."*

The admin types:

> *"Atlas — an inventory planning specialist. It needs to understand
> our current stock, movement velocity, learned routing patterns,
> and outside demand signals — weather, supply-chain news, anything
> that shapes near-term demand. It produces stockout risk and
> reorder recommendations. It doesn't post anything to the ERP."*

Coat thinks for a beat, then surfaces the proposal:

```
PROPOSED AGENT — atlas@coat.io/v1                            (provider: o3 / Gemini 2.5)

Description
  Specialized inventory-planning agent. Reasons over an inventory
  context bundle Coat assembles. Outputs reorder recommendations.
  Does not write to the ERP.

Inferred scopes (least privilege)
  ✓ coat:concepts:read           — knows the shape of this ERP
  ✓ coat:context:inventory:read  — receives Coat-assembled inventory bundle
  ✓ coat:patterns:read           — sees learned routing patterns explicitly
  ✗ coat:inventory:write         — NOT granted (description says "doesn't post anything")
  ✗ coat:invoice:*               — NOT granted (out of role)

Inventory context bundle this agent will receive on each call
  • current stock by warehouse + bin (from ERP)
  • last 60d of movements (from change boundary)
  • learned routing + reservation patterns (from learner)
  • external demand signals — weather, supply-chain news
        these external sources are registered with Coat at the tenant level;
        Atlas does NOT fetch them. Coat does. Atlas reasons.

Mode
  trial (advisory only) for first 50 calls or 7 days, whichever comes first
  promotion to enforced requires re-ratification

  [r]atify   [e]dit scopes   [c]ancel
```

The admin presses `r`. Coat writes the agent manifest, registers the
key, opens a trial session, and prints:

```
✓ atlas@coat.io/v1 onboarded.  audit_id=aud_01HV3R…
  trial budget:  50 calls / 7d
  next ratification due:  after 50th call OR Friday May 10
```

**Voice:** *"This is the onboarding. The admin describes the agent
like they'd brief a new hire. Coat infers the smallest scope set
that covers the job and tells the admin which scopes were
**not** granted, with reasons. The human signs off. The agent is
live in trial mode. Notice what's not happening: nobody told Coat
'go fetch weather, here's an API key.' The external sources are
already registered with Coat at the tenant level. Atlas doesn't
fetch. Atlas reasons over what Coat hands it. As easy as
wearing a coat."*

**Infrastructure required:**
- `coat agent onboard` CLI: prompts for description, asks Claude to
  parse it into a manifest (description → tasks → required scopes
  → required context bundles), renders the proposal with explicit
  "granted" and "denied" lines, persists on ratify. *To build —
  ~140 lines.*
- Manifest derivation logic: takes a description + Coat's tool /
  bundle catalog and returns the minimal scope and bundle set.
  Claude-powered with a curated keyword fallback for offline. *To
  build — ~80 lines.*
- "Trial mode" budget + expiry plumbing. *To build — ~40 lines.*
- `AGENTS` table — id, manifest, trial_budget, next_review.
  *To build — schema + 30 lines.*
- `config/connections/external/*.yaml` — per-tenant external source
  registry (weather provider, news feed, sanctions list, etc.).
  *To build — schema + ~60 lines.*

---

## Scene 5 — Ask Atlas a real question; mid-flight permission expansion — ~75s

**On screen:** Claude Code in the editor pane (or a terminal chat —
either works). The admin types:

> *"Atlas, what's our stockout risk for next week, and what should
> we reorder?"*

Atlas makes **one** tool call: `get_inventory_context(window=7d)`.
Coat receives it, assembles the bundle from every relevant source
it owns, and returns it in business shape:

```json
{
  "as_of": "2026-05-03T14:14:32Z",
  "window": "7d",
  "items": [
    {
      "sku": "SKU-441",
      "name": "Printed Circuit Board Rev-C",
      "on_hand_by_warehouse": { "WH01": 180, "WH02": 320, "WH03": 0 },
      "available_after_reservations": { "WH01": 130, "WH02": 220, "WH03": 0 },
      "movement_last_60d": { "out_avg_per_day": 28.4, "trend": "rising" },
      "learned_routing": "fragile_source=WH02 (conf 0.84)",
      "external_signals": {
        "weather_demand_modifier": 1.18,
        "weather_summary": "heat dome forecast 5/9-5/12, HVAC demand up",
        "supply_chain_risk": "low",
        "news_summary": null
      }
    },
    { "sku": "SKU-200", "...": "..." },
    { "sku": "SKU-300", "...": "..." }
  ],
  "context_origin": [
    "ERP: WH_STOCK + BIN_DETAIL + Z_RESERVED",
    "Change boundary: MSEG, last 60d",
    "Learner: ROUTING patterns (3 enforced)",
    "External: Open-Meteo (registered tenant-level), RSS shipping (registered)"
  ]
}
```

That single response is everything Atlas needs. No second call. No
weather API. No RSS parser. **The agent reasons over context Coat
delivered.** Atlas produces:

```
INVENTORY OUTLOOK — week of 2026-05-10  (Atlas, trial 7/50)

SKU       wh    avail   proj_demand    stockout_risk    reorder_qty   confidence
SKU-441   WH01   130     ~210±18        HIGH             ≥30 by Fri    0.84
SKU-200   WH02   ...     ~110±14        MEDIUM           ≥20 by Mon    0.71
SKU-300   WH02   ...     ~180±22        LOW              none          0.79

Reasoning:
  • SKU-441 demand uplift from heat-dome forecast (weather_demand_modifier 1.18)
  • Routing pattern says fragile sources from WH02 — keep ≥220 there
  • SKU-300 stable; consistent with movement trend
```

The admin replies:

> *"Stage the reorder PO for SKU-441."*

Atlas tries to call `create_purchase_order`. Coat returns:

```
cap.denied  create_purchase_order  agent=atlas@coat.io/v1
            reason: scope coat:procurement:write not granted
            this scope was NOT in the original manifest

Coat surfaces the request to admin:
  Atlas is asking for a new capability:
    coat:procurement:write@vendor=*,max_amount=$5,000
  Atlas's current task argues for granting this — review?  [y/n]
```

The admin presses `y`. Coat updates the manifest, the trial budget
expands by one scope, the call retries, the PO is staged. Audit
trail captures the whole arc.

**Voice:** *"Atlas runs under the scopes Coat inferred. Atlas is
**reasoning**, not plumbing. The agent made one tool call and got
back current stock, recent movements, learned patterns, AND
external demand signals — all assembled by Coat into a single
business-shaped bundle. The incumbents' AI can't see the external
signals because their AI is filtered by their access controls.
External agents in the wild can't see the ERP context because
they don't have the rails. Coat is the layer where both
sides meet. Then — when Atlas tries to write something it wasn't
authorized for, Coat surfaces the gap inline. Mid-flight
ratification. One human keystroke. Audited end to end."*

**Infrastructure required:**
- Atlas as a configured agent backed by a non-Claude reasoning model
  (e.g., `openai/o3` or `google/gemini-2.5-pro`). *To build — ~180
  lines: client wrapper + system prompt + tool-call loop.*
- `get_inventory_context(window, scope?)` MCP tool on the adapter:
  reads ERP stock + bins + reservations, joins recent MSEG
  movements, joins enforced ROUTING patterns, joins external
  signals from registered sources, returns a single bundle.
  *To build — ~150 lines (most of the joins exist; this is
  composition).*
- External-source registry + adapter: `external_sources/` directory
  with one module per source type (`weather.py`, `news_rss.py`).
  Each emits records into a `EXTERNAL_SIGNALS` table keyed by
  entity (SKU, vendor, region). The context-bundle assembler reads
  from this table by key, not by going to the source live.
  *To build — ~200 lines + 2 small CSV fixtures.*
- `cap.denied` path on tool dispatch + "scope expansion request"
  surface that posts to admin and waits for ratify. *To build —
  ~120 lines.*
- `coat audit --entity X --since T` CLI for the next scene. *To
  build — ~80 lines.*
- `COMPLIANCE.md` for the verbal claim. *Already in repo.*

---

## Scene 6 — Full circle, audit chain, the thesis — ~25s

**On screen:** `coat audit --entity SKU-441 --since 1h` — the entity
timeline that ties the whole story together:

```
ENTITY TIMELINE — item / SKU-441   (last hour)

14:12:01  cdc.event       move_stock 80 WH02→WH01
                          actor=u_clerk_b (SAP rails, human)
                          capability_origin=cdc.human_action
14:14:32  obs.read        get_stock
                          agent=atlas@coat.io/v1   scope=coat:inventory:read
                          capability_origin=manifest (inferred from description)
14:14:34  obs.read        suggest_source_warehouse
                          agent=atlas@coat.io/v1   scope=coat:inventory:read
14:15:01  cap.denied      create_purchase_order    agent=atlas@coat.io/v1
                          reason: coat:procurement:write not granted
14:15:18  cap.granted     coat:procurement:write@vendor=*,max=5000
                          ratified by u_mgr_c   reason: "approved for Atlas trial"
14:15:19  obs.write       create_purchase_order  PO-2026-0014
                          agent=atlas@coat.io/v1   scope=coat:procurement:write
                          capability_origin=ratification_aud_01HV3T…
```

**Voice:** *"Watch the chain. A human moved stock through SAP. Coat
saw it on the change boundary. An external agent — built on a
different model, configured by description, not by manifest — ran
under the scopes we inferred. Hit the edge of its permissions. The
admin granted one new capability in one click. Every step has a
reason, every reason has a human signature, every signature has a
timestamp. This is what 'AI-native ERP' actually means. Walls don't
matter. The agent doesn't matter. The model doesn't matter. The
rails do."*

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
| 7 | `coat agent onboard` CLI — natural-language → manifest derivation, ratification UX | 4 | ~140 lines |
| 8 | Manifest derivation logic — Claude-powered scope + bundle inference + curated fallback | 4 | ~80 lines |
| 9 | `AGENTS` table + trial-mode budget plumbing | 4, 5 | ~70 lines |
| 10 | `external_sources/` registry + adapters (weather, news), `EXTERNAL_SIGNALS` table | 4, 5 | ~200 lines + fixtures |
| 11 | `get_inventory_context(window, scope?)` MCP tool — assembles ERP + change boundary + patterns + external signals into one bundle | 5 | ~150 lines |
| 12 | Atlas — non-Claude reasoning model wrapper (o3 / Gemini) + system prompt + tool loop | 5 | ~180 lines |
| 13 | `cap.denied` path + "scope expansion request" inline-ratification surface | 5 | ~120 lines |
| 14 | `coat audit --entity` CLI | 6 | ~80 lines |

Total: roughly 1,300 lines of new code, plus a few CSV fixtures.
All additive, none of it requires changes to the core adapter or
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
- `coat agent onboard` CLI + manifest derivation
- Atlas (specialized non-Claude inventory agent)
- `cap.denied` + scope-expansion ratification surface
- `coat init`, `coat watch`, `coat audit` CLIs

Next move: greenlight the build, pick scene-1-and-2 as the first
slice (confidence-scored context map), and ship that today.
Scene 4 (the "as easy as wearing a coat" onboarding) is the
second slice — it's the one that lands the philosophy on screen.
Scene 5 (Atlas + mid-flight ratification) is the third. Recording
on day three or four.
