# Coat — demo runbook

The 4-to-5-minute spine for the recorded demo, scene by scene. Each
scene names the infrastructure it depends on so the build plan is
traceable to what lands on screen.

The recording is async, so retakes are free. The point is not heroic
liveness — it is a tight, opinionated story that lands in under five
minutes.

The whole thing runs in VS Code. Three-pane layout:

- **Editor pane (left):** the project files. Open `CLAUDE.md` and the
  agent description file you'll paste in scene 4. Optionally Claude
  Code in the side panel.
- **Watch pane (top-right):** `coat watch` running. Live tail of
  `WORKFLOW_OBS` + capability grants. This pane *is* Coat being on,
  ambient through the whole recording.
- **Work pane (bottom-right):** where you type. All commands here.

One-time setup before recording:

```bash
cd ~/coatmvp/erp_adapter
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e .              # registers `coat` and `atlas` as binaries
```

After this, every command in the runbook is `coat ...` or `atlas`.

## Live-mode validation (do this once before recording)

Atlas runs in two modes — *scripted* (deterministic reasoning over the
bundle, no LLM call, runs offline) and *live* (real tool-use loop with
whichever provider has an API key in the env). Recording-grade
demos use live mode because the agent's voice — the reasoning between
the bundle and the recommendation — is the most distinctive moment.

`agents/provider.py` auto-picks the provider when `ATLAS_PROVIDER` is
unset, preferring **non-Claude** providers when keys are present. That
is deliberate: the most powerful talking point in scene 5 is
*"different brain, same MCP surface, same answer."* Validate at least
one non-Claude provider before recording.

```bash
# 1. Reset the world
coat init
coat agent onboard --from-file <(cat <<'EOF'
Atlas — an inventory planning specialist. It needs to understand stock,
movement velocity, learned patterns, and outside demand signals — weather,
supply-chain news. It produces stockout risk and reorder recommendations.
It doesn't post anything to the ERP.
EOF
) --provider openai --model o3 --auto-yes

# 2. Anthropic — the safety net (always works if you have a key)
ANTHROPIC_API_KEY=sk-ant-... \
  ATLAS_PROVIDER=anthropic ATLAS_MODEL=claude-opus-4-6 \
  atlas "what's our stockout risk for next week?"

# 3. OpenAI — the recommended demo provider for the model-agnostic story
pip install openai>=1.30
OPENAI_API_KEY=sk-... \
  ATLAS_PROVIDER=openai ATLAS_MODEL=o3 \
  atlas "what's our stockout risk for next week?"

# 4. Google Gemini — alternate
pip install google-genai>=0.3
GOOGLE_API_KEY=... \
  ATLAS_PROVIDER=google ATLAS_MODEL=gemini-2.5-pro \
  atlas "what's our stockout risk for next week?"
```

**Pass criteria for each provider:** Atlas calls
`get_inventory_context` exactly once, the bundle returns, the model
produces a recommendation table containing SKU-441 with HIGH risk and
a non-zero reorder qty, and the reasoning notes cite the Suzhou
brown-out (or the heat dome, or both). If a provider returns *more
than one tool call* or hallucinates a tool name, that's a system-prompt
issue — `agents/atlas.py:SYSTEM_PROMPT` is where to tighten.

If a provider chokes (rate limit, model-name mismatch, timeout), fall
back to scripted mode for the recording — `atlas --scripted` produces
a deterministic forecast rendered identically. The voice-over still
lands.

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

**Watch pane (already running):** `coat watch` showing the empty timeline.

**Work pane:**

```bash
coat init
```

**On screen:** five-step pipeline runs in the work pane — build mock
ERP, introspect, semantically map, render the concept catalog, mine
historical patterns. The watch pane shows learner ticks landing in
real time. The work pane ends with the green "Coat is installed" panel
and the next-step menu.

**Voice:** *"This is what a new customer sees on day one. We point Coat
at their ERP. We don't ask for a schema map. We don't ask for an
ontology. Discovery reads the database directly and figures out which
physical tables back which business concepts. Ten minutes of compute,
no consultant."*

**Infrastructure status:** ✓ shipped — `cli/coat_init.py` wraps the
pipeline with the next-step menu. Confidence-scored context renders
in step 3 (scene 2 below).

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

**Infrastructure status:** ✓ shipped.
- `discovery/confidence.py` runs structural verification per concept
  (PK shape, FK referrer count, sample distribution match for
  country/currency/status/flag/numeric/date roles).
- `cli/render_concepts.py` renders the rich catalog with confidence
  bars colored green / yellow / red.
- Confidence + evidence is baked into `context.yaml` — auditable on disk.

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

**Infrastructure status:** partial — `coat watch` ✓ shipped. The
SAP-rails simulator + change-boundary trigger were *cut* for the
recording: the existing scripted demo at `python run.py` already shows
pattern emergence (V1001 fast-track + fragile_source_warehouse); the
voice-over covers the "Coat sits at the change boundary" story
without needing a separate simulator. The watch pane in the corner
makes the architecture visible regardless.

---

## Scene 4 — Onboard a specialized external agent in plain English — ~75s

**Work pane:**

```bash
coat agent onboard
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

**Infrastructure status:** ✓ shipped.
- `coat agent onboard` CLI with three-stage manifest derivation
  (LLM-powered task extraction with deterministic keyword fallback,
  scope projection, proposal assembly).
- `AGENTS` + `CAPABILITY_GRANTS` tables with full audit chain.
- Trial-mode budget + expiry plumbing.
- External-source registry and `EXTERNAL_SIGNALS` table seeded with
  weather + shipping_news synthetic providers (Open-Meteo + RSS shape).

---

## Scene 5 — Ask Atlas a real question; mid-flight permission expansion — ~75s

**Work pane:**

```bash
atlas "what's our stockout risk for next week, and what should we reorder?"
```

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

**Work pane (admin types this):**

```bash
atlas --demo-denial
```

Atlas tries `move_stock` to rebalance SKU-441 from WH02 to WH01.
The dispatcher checks Atlas's manifest — `coat:inventory:write` is
not granted. Instead of failing silently, Coat surfaces the request
**inline in the same terminal**:

```
╭──────────────────────────────── Coat ────────────────────────────────╮
│  scope-expansion request                                             │
│    agent  : atlas@coat.io/v1                                         │
│    asks   : coat:inventory:write                                     │
│    to do  : move_stock(matnr=SKU-441)                                │
│    reason : agent does not hold coat:inventory:write                 │
│    audit  : aud_e3e58f58af6d4447a7faac94dbea17e6                     │
╰──────────────────────────────────────────────────────────────────────╯
Approve this scope for this agent? [y/n]
> y
```

Admin types `y`. Coat grants the capability (full audit row, origin
`admin`, note `inline ratification (mid-flight)`), updates the manifest,
retries the call automatically. Result panel: *"atlas resolved its own
permission ask — DOC1777828213 posted, qty=30, WH02→WH01, scope used:
coat:inventory:write."*

The admin never typed a scope string. The agent surfaced what it
needed. The human said yes. The capability lifecycle ratchet moved
exactly one click.

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

**Infrastructure status:** ✓ shipped.
- Atlas — `agents/atlas.py` + `agents/provider.py`. Provider-agnostic
  factory (Anthropic / OpenAI / Google) with one tool-use loop
  interface; auto-prefers non-Claude when keys are present; scripted
  fallback for offline.
- `mcp_server/bundles/inventory.py:get_inventory_context` — the bundle
  assembler. Composes ERP + change-boundary + learner + external
  signals + `context_origin` provenance.
- `external_sources/` registry — `weather.py` (synthetic Open-Meteo
  shape) and `shipping_news.py` (synthetic RSS advisories tied to
  SKU-441 / SKU-200 / WH03). `EXTERNAL_SIGNALS` table keyed by
  `(source, entity_kind, entity_key)`.
- `mcp_server/dispatch.py` — scope-aware dispatch with **inline
  mid-flight ratification**. TTY-detected by default;
  `COAT_FORCE_INLINE_RATIFY=1` forces on, `COAT_NO_INLINE_RATIFY=1`
  disables. On `y` the capability is granted (full audit), the call
  retries, the result lands.
- `coat audit --entity X --since T` ✓ shipped.
- `COMPLIANCE.md` ✓ in repo.

**Demo seed overrides:** `seed.py:apply_demo_overrides()` drains
SKU-441 (HIGH risk, reorder 133) and SKU-200 (HIGH risk, reorder 40)
so the forecast lands a real procurement decision instead of a calm
"everything LOW" table. SKU-300 / SKU-500 / SKU-100 etc. stay LOW —
the calm baseline that makes the HIGHs visible.

---

## Scene 5b — Real-time context update (the "living layer" beat) — ~30s

This is the beat that proves Coat is ambient and reactive, not a
snapshot. **Insert it between Atlas's first run and the inline-ratify
denial moment.**

The story: an employee is working in the ERP — fulfilling orders,
draining stock — and Coat observes every event on the change
boundary. The next agent run reflects the shifted state. No agent
restart, no re-deploy, no agent code touched.

**Work pane:**

```bash
coat sim activity --sku SKU-300 --qty 25 --warehouse WH02 --repeat 30 --over-hours 8
```

This simulates `u_clerk_a` posting 30 consumption events (BWART=261)
on SKU-300 at WH02 over the last 8 hours. Each event writes an MSEG
row, decrements `BIN_DETAIL.QTY` and `WH_STOCK.LABST`, and writes to
`WORKFLOW_OBS`.

**Watch pane (already running) lights up** with 30 `consume_stock`
events landing in rapid succession. That's Coat seeing the work
happen at the change boundary — exactly the architecture from
`OBSERVABILITY.md`: subscribe at the change boundary, never poll.

**Then refine the catalog from those observations:**

```bash
coat refine --window 2h
```

**On screen:** a row-per-concept table showing confidence bumps.
Six concepts get visibly upgraded — `item`, `warehouse`,
`stock_by_warehouse`, `stock_by_bin`, `reservation`, and
`stock_movement` each rise by +0.06 because Coat watched the
operator's consumption workflow touch each of those tables in a
consistent pattern. The custom Z-table (`Z_RESERVED` →
`reservation`) is the most striking lift: it had a low structural
score because no FK points at it, but operator behavior just
confirmed the inferred semantics. Discovery was the prior;
observations are the posterior.

**Voice:** *"Day-one discovery is a hypothesis — we ask the database
what we think each table is, with confidence scores. But that's a
prior, not the truth. Watch what happens when the system gets used.
Coat saw an operator fulfill thirty orders in the last eight hours.
Each order touched the item table, the warehouse table, the bin
table, the movement log, and a reservation row. Those co-occurrences
ARE the evidence. Confidence on six concepts just went up. The
custom Z-table with no foreign keys — that one we used to be unsure
about — went from 0.82 to 0.89. Coat got sharper because someone
worked."*

**Then re-run Atlas to see the inventory side:**

```bash
atlas --scripted
```

(or `atlas` with your provider key — the live-mode path is identical.)

SKU-300's row also shifts on three columns at once:

| | before activity | after activity |
|---|---|---|
| primary warehouse | WH02 | **WH01** (Atlas pivoted) |
| available | 844 | 707 |
| projected demand | ~108 ± 11 | **~199 ± 20** |

Three visible changes from one operator action:
- **Atlas pivoted warehouses.** WH02 was drained — Atlas now recommends sourcing from WH01.
- **Available shifted.** 844 → 707, because the WH02 hero is now at 94.
- **Projected demand nearly doubled.** Coat saw 30 fresh MSEG events through the bundle assembly. Velocity rose. Atlas's projection reflects it.

**Voice:** *"An employee just spent the last eight hours fulfilling
orders in our ERP. Coat saw every event on the change boundary —
that's the watch pane lighting up. Atlas didn't restart. Atlas
wasn't re-deployed. The next call to the bundle reflects the new
state — drained stock, rising velocity, a pivoted recommendation.
The agent reasoned over a different reality. Same contract, same
scopes, fresh context. That's the difference between an LLM bolted
on the side and a layer that lives in your workflow."*

**Infrastructure status:** ✓ shipped — `cli/coat_sim.py:activity()`
posts MSEG/BIN_DETAIL/WH_STOCK writes through the change boundary;
the inventory bundle assembler picks up both the drained available
and the bumped velocity on its next read.

**Three sim primitives are available — pick the right one for your beat:**

- `coat sim activity` — employee works in the ERP. **The primary
  demo beat.** Shows Coat's change-boundary observability.
- `coat sim feedback` — manager corrects a prior agent decision.
  Triggers the learner to re-mine and surfaces a new PREFERENCE
  pattern. Use as a B-roll if you have time.
- `coat sim news` — represents what *another agent* (a news
  monitor, a sanctions checker, a weather feed) would write to
  Coat over MCP. Useful when telling the third-party agent
  ecosystem story; less central to the change-boundary beat.

---

## Scene 6 — Full circle, audit chain, the thesis — ~25s

**Work pane:**

```bash
coat audit --entity SKU-441 --since 1h
```

The entity timeline that ties the whole story together:

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

## Build map — actual code shipped

| # | Piece | Scene(s) | Status | File(s) |
|---|-------|----------|--------|---------|
| 1 | Confidence scoring per concept | 1, 2 | ✓ shipped | `discovery/confidence.py` |
| 2 | Concept catalog renderer (rich table, confidence bars, evidence summary) | 1, 2 | ✓ shipped | `cli/render_concepts.py` |
| 3 | `coat init` bootstrap | 1 | ✓ shipped | `cli/coat_init.py` |
| 4 | `coat watch` live tail of `WORKFLOW_OBS` + capability grants | all (ambient) | ✓ shipped | `cli/coat_watch.py` |
| 5 | `coat agent onboard` CLI + manifest derivation (LLM + curated fallback) | 4 | ✓ shipped | `cli/agent_onboard.py`, `agents/manifest_derivation.py` |
| 6 | `AGENTS` + `CAPABILITY_GRANTS` tables, trial-mode budget plumbing | 4, 5 | ✓ shipped | `mock_erp/schema.sql` |
| 7 | `external_sources/` registry + weather/shipping_news synthetic providers, `EXTERNAL_SIGNALS` | 4, 5 | ✓ shipped | `external_sources/*.py` |
| 8 | `get_inventory_context` bundle assembler | 5 | ✓ shipped | `mcp_server/bundles/inventory.py` |
| 9 | Atlas — provider-agnostic agent (Anthropic / OpenAI / Google) | 5 | ✓ shipped | `agents/atlas.py`, `agents/provider.py` |
| 10 | Scope-aware dispatch + cap.denied + inline mid-flight ratification | 5 | ✓ shipped | `mcp_server/dispatch.py` |
| 11 | `coat agent grant` / `revoke-scope` CLI | 5 | ✓ shipped | `cli/agent_grant.py` |
| 12 | `coat audit --entity` entity timeline + capability provenance | 6 | ✓ shipped | `cli/coat_audit.py` |
| 13 | `coat` console-script entry point (after `pip install -e .`) | all | ✓ shipped | `pyproject.toml` |
| 14 | Demo seed overrides for visible risk-band spread (SKU-441 / SKU-200 → HIGH) | 5 | ✓ shipped | `mock_erp/seed.py` |
| 15 | `coat sim activity` + `feedback` + `news` — change-boundary observation, the "living layer" beat | 5b | ✓ shipped | `cli/coat_sim.py`, bundle MAX-risk aggregation, Atlas risk-band amplification |
| 16 | `coat refine` — raise concept-catalog confidence from observed operator workflows | 5b | ✓ shipped | `cli/coat_refine.py` |

Six things deliberately *cut* from the build:

- SAP-rails simulator that bypasses the adapter (scene 3 was reframed —
  the existing `python run.py` step "5/5 Run agent demo" already shows
  pattern emergence; the watch pane covers the change-boundary story).
- SQLite trigger CDC watcher (replaced by the simpler — and equally
  legible — story of `coat watch` tailing `WORKFLOW_OBS`).
- A full crypto handshake for the agent protocol (the inline-ratify UX
  + the audit chain land the protocol's *meaning* without yet shipping
  Ed25519 + signed envelopes).
- Multi-tenant config tree (`coat.yaml` + `config/connections/*.yaml`)
  — specced in `DEPLOYMENT.md`, not built; not required for a single-
  tenant demo.
- Web admin UI / ratification queue dashboard — terminal output is
  cleaner for a recording.
- 3-way invoice match — the original day-zero pick. Atlas's inventory
  story is more product-distinctive and lands the same architectural
  beats.

Total shipped: ~3,960 lines of production Python across 18 new files,
plus the architecture docs. All additive on top of Prince's original
pipeline.

---

## Recording order

Build everything, then record once with multiple takes. Recording
order is not necessarily build order — record scene 2 first (concept
map is the highest-stakes still frame), then scenes 1 → 3 → 4 → 5 →
6. Then cut.

---

## Recording sequence — exact commands, in order

After the one-time setup at the top of this doc, the recording runs
through these commands. Every box is one command. The watch pane is
already running in the corner.

```bash
# Scene 1 — Configure (45s)
coat init

# Scene 2 — Concept catalog with confidence (60s)
# Already rendered as step 3 of `coat init`. Pause on the table.
# (Optional drill-in:)
python -m cli.render_concepts --concept item

# Scene 3 — Voice-over the change-boundary architecture (60s)
# The watch pane shows pattern emergence as `coat init` ran.
# (Optional explicit:)
python -m learner.miner            # show pattern table refresh
coat audit --entity V1001 --since 1h    # see vendor_fast_track origin

# Scene 4 — Onboard Atlas in plain English (75s)
coat agent onboard
# → describe the agent (paste from README or type)
# → ratify with `r`

# Scene 5a — Atlas reasons (35s)
atlas "what's our stockout risk for next week, and what should we reorder?"
# → forecast table with SKU-441 HIGH and SKU-200 HIGH

# Scene 5b — Real-time context update (45s, the "living layer" beat)
coat sim activity --sku SKU-300 --qty 25 --warehouse WH02 --repeat 30 --over-hours 8
coat refine --window 2h            # confidence rises on six concepts (incl. reservation)
atlas                              # forecast shifts: WH02 drained, Atlas pivots to WH01,
                                   # projected demand nearly doubles

# Scene 5c — Mid-flight ratification (40s)
atlas --demo-denial
# → scope-expansion request panel renders
# → type `y` to ratify
# → call retries, DOC posted, "atlas resolved its own permission ask"

# Scene 6 — Full circle audit (25s)
coat audit --entity SKU-441 --since 1h
```

That is the complete recording, command by command. Total: ~5 minutes.

---

## Status — what's built and what's not

**Built and verified end-to-end** (everything in the build map above):
the entire demo runs against the mock SAP-shaped ERP that ships with
the repo. `coat init && coat agent onboard && atlas && atlas --demo-
denial && coat audit` is the full path, no manual setup beyond the
one-time `pip install -e .`.

**Deliberately cut** (see "Six things deliberately cut" above): the
SAP-rails simulator, SQLite trigger CDC watcher, full crypto handshake,
multi-tenant config tree, web admin UI, and 3-way invoice match — each
specced in the architecture docs but not required for the recording.

**On the path to production** (in `DEPLOYMENT.md`, `OBSERVABILITY.md`,
`AGENT_PROTOCOL.md`, `COMPLIANCE.md`): real CDC observers per ERP
backend, the Coat Bridge agent for hybrid deployments, multi-tenant
config + ratification queue UI, SOC 2 / ISO 27001 / GDPR / SAP
Endorsed Apps roadmap.

**Next moves before recording:**
1. Pull on your Mac, `pip install -e .`, run the full path once.
2. Validate Atlas live mode against your preferred provider — see
   "Live-mode validation" section near the top of this doc.
3. Practice the recording sequence twice, time it (target 4:30).
4. Record. Cut. Ship.

---

## Practice script — what to do, what to say, what to pause on

This is the second-pass version that builds on "Recording sequence."
Use it for dry runs.

### Terminal setup (do once)

- One VS Code window. Three terminals:
  - **Top-right (watch):** `coat watch` — leave running for the whole
    take. Fontsize one notch larger than the others; the eye returns
    to it during voice-over beats.
  - **Bottom-right (work):** the typing pane. Wide enough for a
    five-column rich table to fit on one line (~120 chars).
  - **Optional left (editor):** open `CLAUDE.md` so when the camera
    pans there it's the manifesto, not random Python.
- Theme: dark. Dot-matrix-y if you can. The cyan / yellow / red bars
  in Coat's output read best on dark.
- Hide the AI assistant pane during the recording — Coat is the agent
  here, not the IDE's copilot.
- Set `PS1='$ '` so the prompts are minimal. No git branch noise.

### Beat-by-beat (target 4:30)

| t  | scene | beat | say |
|----|-------|------|-----|
| 0:00 | open | wide shot of the layout, both panes visible, watch pane idle | *"This is Coat. We're an AI-native layer that sits on any ERP."* |
| 0:08 | 1 | run `coat init` in work pane; watch pane lights up with seed/discovery/learner ticks | *"Day-one onboarding. We point Coat at the customer's ERP. No schema map. No ontology."* |
| 0:30 | 2 | pause on the rendered concept catalog | *"Twelve concepts discovered. Each confidence is structural — we tested it against the data. Glean indexes documents and ranks by relevance. Coat doesn't search the schema, it verifies the schema."* |
| 1:15 | 3 | watch pane briefly on screen, point at the learner tick rows | *"Coat sees what humans do directly in SAP. No polling. We subscribe at the change boundary."* |
| 1:45 | 4 | run `coat agent onboard`, paste Atlas description, ratify with `r` | *"Onboarding an external agent. The admin describes the agent like a new hire. Coat infers the smallest scope set. Notice the scopes that are NOT granted — those are the safety lines."* |
| 2:30 | 5a | run `atlas` with question, pause on forecast | *"Atlas is built on a different model — OpenAI o3, in this take. Coat's MCP surface is provider-agnostic. Atlas made one tool call. Got a fully-assembled bundle. Spent its tokens on reasoning."* |
| 3:15 | 5b | run `atlas --demo-denial`, scope-expansion panel renders, type `y` | *"Atlas hits a permission it doesn't have. Watch — Coat asks the admin in plain English. One keystroke. The capability is granted with a full audit row, the call retries, the doc posts."* |
| 4:00 | 6 | run `coat audit --entity SKU-441 --since 1h` | *"The whole arc, end to end. Every action chains back to the capability that authorized it. The capability chains back to the manifest the human ratified. Provenance is everything."* |
| 4:25 | close | wide shot, watch pane still going | *"Coat. AI-native ERP. Walls don't matter."* |

### Pause-on moments (the still frames)

These are the screenshots that go into the deck and the email follow-up.
Make sure each one is on screen for at least 3 full seconds.

1. The concept catalog after `coat init` — confidence bars green / yellow.
2. The "PROPOSED AGENT" panel during `coat agent onboard` — green
   granted lines + red denied lines.
3. Atlas's forecast table — SKU-441 HIGH next to SKU-300 LOW; reorder
   column non-empty.
4. The yellow "scope-expansion request" panel during `--demo-denial`.
5. The audit timeline at the end — DENIED row in red, OK row in green,
   capability provenance at the bottom showing manifest + admin grants.

### Common pitfalls

- **Watch pane scrolls off the most recent event.** Resize the work
  pane down to keep watch in view, or scroll the watch pane down
  before starting the take.
- **Provider rate-limits mid-take.** The `--scripted` flag is the
  fallback. Run `atlas --scripted "stockout risk?"` — same table,
  no LLM. Voice-over the model-agnostic point with conviction.
- **`coat init` re-runs reset the database** including any agents
  you onboarded. If you re-init mid-recording, re-onboard Atlas
  before scene 5. (Or do all takes from a single `coat init`.)
- **Inline ratify doesn't fire because stdin isn't a TTY.** Make
  sure you're typing into the terminal directly, not via a piped
  script. If running headless, `COAT_FORCE_INLINE_RATIFY=1` forces
  the prompt.

### Recording tools

- **macOS:** `Cmd-Shift-5` → "Record selected portion." Pick the
  whole VS Code window, frame to hide the menu bar if it's
  distracting. AAC 256kbps audio.
- **Voice-over track:** if you want to redo the audio without redoing
  the screen, record screen silent then dub voice in iMovie / DaVinci.
  The latter handles voiceover better.
- **Cuts:** keep them at the natural pause between scenes (after
  `coat init` finishes, before `coat agent onboard`, etc.). Don't cut
  inside a panel.
