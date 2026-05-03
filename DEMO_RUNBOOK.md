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

**As easy as wearing a coat.** The admin never types a scope string.
They describe the agent's job in plain English. Coat infers what the
agent needs to read, write, and act on — and surfaces a proposal for
human ratification. Permissions are *derived*, not declared. Agents
don't ask for capabilities — Coat figures out what they need and the
human signs off.

This is what the recording has to feel like, scene by scene.

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

> *"Atlas — an inventory planning specialist. It looks at our stock,
> recent movements, and outside data like weather and shipping
> disruptions to predict next week's stockout risk per SKU and
> recommend reorder quantities. It needs to read inventory and
> movements; it doesn't post anything to the ERP itself."*

Coat thinks for a beat, then surfaces the proposal:

```
PROPOSED AGENT — atlas@coat.io/v1                            (provider: o3 / Gemini 2.5)

Description
  Specialized inventory-planning agent. Reads stock + movement history;
  joins external context; outputs reorder recommendations. Does not
  write to the ERP.

Inferred scopes (least privilege)
  ✓ coat:concepts:read         — needs to know the shape of this ERP
  ✓ coat:inventory:read        — get_stock, suggest_source_warehouse (read-only)
  ✓ coat:patterns:read         — surface routing patterns to inform recommendations
  ✗ coat:inventory:write       — NOT granted (description says "doesn't post anything")
  ✗ coat:invoice:*             — NOT granted (out of role)

Mode
  trial (advisory only) for first 50 calls or 7 days, whichever comes first
  promotion to enforced requires re-ratification

External data sources the agent declared it will use
  • weather/precipitation data (Open-Meteo)
  • shipping-disruption news (RSS, hashed for audit)

  [r]atify   [e]dit scopes   [c]ancel
```

The admin presses `r`. Coat writes the agent manifest, registers the
key, opens a trial session, and prints:

```
✓ atlas@coat.io/v1 onboarded.  audit_id=aud_01HV3R…
  trial budget:  50 calls / 7d
  next ratification due:  after 50th call OR Friday May 10
```

**Voice:** *"This is the onboarding. The admin doesn't think about
permissions. They describe the agent like they'd describe a new
hire. Coat reads the description, infers what the agent needs to
read, write, and act on — and proposes the smallest scope set that
covers it. The human signs off. The agent is live in trial mode.
That's the whole onboarding. As easy as wearing a coat."*

**Infrastructure required:**
- `coat agent onboard` CLI: prompts for description, asks Claude to
  parse it into a manifest (description → tasks → required tools →
  scope set), renders the proposal with explicit "granted" and
  "denied" lines, persists on ratify. *To build — ~140 lines.*
- Manifest derivation logic on the adapter side: a small function
  that takes a description + Coat's tool catalog and returns the
  minimal scope set. Uses Claude with a tight system prompt; falls
  back to a curated keyword-to-scope mapping if offline. *To build —
  ~80 lines.*
- "Trial mode" in the existing capability lifecycle from
  `OBSERVABILITY.md` — already specified, just needs the budget +
  expiry plumbing. *To build — ~40 lines.*
- Agent registry table — single `AGENTS` table with id, manifest,
  pubkey_path (placeholder ok for the demo), trial_budget, next_review.
  *To build — schema + 30 lines.*

---

## Scene 5 — Ask Atlas a real question; mid-flight permission expansion — ~75s

**On screen:** Claude Code in the editor pane (or a terminal chat —
either works). The admin types:

> *"Atlas, what's our stockout risk for next week, and what should
> we reorder?"*

Atlas thinks. Tool calls stream in real time — `list_concepts`,
`get_stock` for the top SKUs, `suggest_source_warehouse` for routing
context, `list_patterns(kind=ROUTING)` for learned constraints. Each
call shows the granted scope it ran under. Then the answer:

```
INVENTORY OUTLOOK — week of 2026-05-10  (Atlas, trial mode, 7/50 calls)

SKU       wh    on_hand  proj_demand    stockout_risk    reorder_qty   confidence
SKU-441   WH01   180     ~210±18        HIGH             ≥30 by Fri    0.84
SKU-200   WH02    95     ~110±14        MEDIUM           ≥20 by Mon    0.71
SKU-300   WH02   240     ~180±22        LOW              none          0.79

Reasoning notes:
  • SKU-441 trending up due to seasonal HVAC demand (Open-Meteo: heat dome 5/9–5/12)
  • SKU-200 affected by upstream shipping delay (RSS: Port of Long Beach 5/3 advisory)
  • SKU-300 stable; consistent with last 3 quarters

Recommended action: place reorder for SKU-441 (30 units) and SKU-200 (20 units).
```

The admin replies:

> *"Go ahead and stage the reorder PO for SKU-441."*

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

**Voice:** *"Atlas runs under the scopes Coat inferred. Read-only,
trial mode. Real reasoning, real external data, real ERP context —
things the incumbents' AI can't see because it's filtered by their
access controls. When Atlas hits the edge of its granted scope, it
doesn't fail silently. Coat surfaces the gap to the admin in
plain English: 'Atlas is asking for this capability. Want to grant
it?' Mid-flight ratification. Audit chain captures the entire arc —
who asked for what, when, why, and what they got back. SOC 2,
GDPR, SOX-aware from day one. Full compliance roadmap in
COMPLIANCE.md."*

**Infrastructure required:**
- Atlas as a configured agent backed by a non-Claude reasoning model
  (e.g., `openai/o3` or `google/gemini-2.5-pro` via their respective
  SDKs) — demonstrates Coat is provider-agnostic. *To build — ~180
  lines: client wrapper + system prompt + tool-call loop.*
- Synthetic external data: `data/external/weather.csv` and
  `data/external/shipping_disruptions.csv`. *To build — trivial.*
- `cap.denied` path on tool dispatch + "scope expansion request"
  surface that posts to the admin and waits for ratify. *To build —
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
| 8 | Manifest derivation logic — Claude-powered scope inference + curated fallback | 4 | ~80 lines |
| 9 | `AGENTS` table + trial-mode budget plumbing | 4, 5 | ~70 lines |
| 10 | Atlas — non-Claude reasoning model wrapper (o3 / Gemini) + system prompt + tool loop | 5 | ~180 lines |
| 11 | Synthetic external data CSVs (weather + shipping disruptions) | 5 | trivial |
| 12 | `cap.denied` path + "scope expansion request" inline-ratification surface | 5 | ~120 lines |
| 13 | `coat audit --entity` CLI | 6 | ~80 lines |

Total: roughly 1,100 lines of new code, plus two CSV fixtures. All
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
