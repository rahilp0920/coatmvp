# Coat

**An AI-native layer that sits on top of any ERP, learns how work gets done, and exposes agent-ready rails over MCP.**

Existing ERP vendors are racing to build walled-garden AI inside their own
systems. Their AI can't see external data, is filtered by their access
controls, doesn't reach the on-prem deployments where roughly half the
world's businesses still live. Coat is the cannon. Walls don't matter when
the rails are agent-native and the runtime is everywhere.

This repository is the working proof. It runs end-to-end on a deliberately
messy SAP-style ERP, discovers its shape, learns from the workflow log, and
lets a Claude agent execute real ERP tasks through a small set of
business-concept tools — no SQL, no schema knowledge, no consultants.

---

## Quick start

```bash
cd erp_adapter
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python run.py             # rebuild DB, discover schema, mine patterns, run scripted demo
python run.py live        # same pipeline, real Claude tool-use loop (needs ANTHROPIC_API_KEY)
python run.py inspect     # dump current LEARNED_PATTERNS + recent observations
```

> Python ≥ 3.10 is required (the MCP SDK does not support 3.9).

To plug Coat into Claude Code or Claude Desktop as an MCP server, see
[`erp_adapter/README.md`](erp_adapter/README.md).

---

## What's in this repo

```
coatmvp/
├─ CLAUDE.md              agent guardrails — read before any tool use
├─ README.md              this file
├─ ARCHITECTURE.md        deep-dive on discovery, adapter, learner, plugin surface
├─ SECURITY.md            permissions model, data isolation, audit
├─ OBSERVABILITY.md       how Coat sees / what it remembers / what it learns
├─ AGENT_PROTOCOL.md      how agents identify, scope capabilities, and audit through Coat's MCP surface
├─ DEPLOYMENT.md          deployment shapes, configuration layering, visibility surfaces (admin / auditor)
├─ COMPLIANCE.md          posture today, frameworks that matter, roadmap, architecture-level enablers
├─ DEMO_RUNBOOK.md        the 6-scene demo storyline + per-scene infrastructure build map
└─ erp_adapter/           the working pipeline
   ├─ mock_erp/           messy SAP-style schema + seeded data
   ├─ discovery/          schema introspection + semantic mapping
   ├─ context/            generated concept_map / join_paths / derived_views
   ├─ mcp_server/         MCP stdio server + adapter (business → physical)
   ├─ learner/            mines WORKFLOW_OBS into LEARNED_PATTERNS
   ├─ agent/              scripted + live Claude tool-use demo
   └─ run.py              one-shot pipeline runner
```

---

## How the pieces fit

```
 Claude / any MCP client
        │  high-level business tools
        ▼
   MCP server  ─── tool catalog generated from context.yaml
        │
        ▼
   Adapter  ── translates "get_stock(SKU-441)" into the right joins,
                logs every call to WORKFLOW_OBS,
                consults LEARNED_PATTERNS at decision time
        │
        ├──▶ ERP (SQLite mock today, OData/RFC tomorrow)
        ▲
   Discovery (one-shot per ERP)
        │  introspects schema, samples rows, asks Claude to label semantics
        ▼
   context.yaml ── concept_map • join_paths • derived_views

   Learner (continuous)
        WORKFLOW_OBS  ─▶  ROUTING / APPROVAL / PREFERENCE patterns
                          (re-mined on every Nth call and on every feedback row)
```

The boundary between *what Coat knows* and *what the agent sees* is
deliberate: the agent only ever sees business concepts and the tools that
operate on them. Everything cryptic — `MATNR`, `BUKRS`, `Z_RESERVED`,
custom Z-tables, header/line splits, `SHKZG` debit/credit — stays inside
the adapter. Point Coat at a different ERP and the agent code, the
prompts, and the tool surface do not change.

---

## What you can demo today

Run `python run.py` for the scripted three-scenario walk-through:

1. **Inventory restock** — agent calls `list_concepts`, `get_stock`, `suggest_source_warehouse`, `move_stock`. The suggestion's `reason` field cites a learned routing pattern (fragile items source from WH02 with 0.9 confidence over 37 prior moves).
2. **Vendor fast-track** — same `post_invoice` call, two vendors, opposite outcomes. V1001 auto-posts because the learner saw 5+ manager-approved invoices under a consistent ceiling. V1002 parks for review.
3. **Live correction** — a user submits feedback on a routing decision. The learner re-mines on the spot. The next routing call honors the override.

Run `python run.py live` to put a real Claude agent (Opus or Sonnet) through
the same scenarios via tool use.

---

## Plugin and agent interface

Coat is consumed through MCP. Any plugin — Claude Code's GSD, a custom
plugin, a scheduled task, another agent — that speaks MCP can call the same
tool surface:

| Tool | Purpose |
|------|---------|
| `list_concepts` | Enumerate this company's business objects and the tables behind them |
| `find_item` | Search materials by id or name |
| `get_stock` | Per-warehouse stock with true available quantity (post-reservations) |
| `suggest_source_warehouse` | Pick a source warehouse using learned routing |
| `move_stock` | Post a stock transfer (writes BIN_DETAIL + WH_STOCK + MSEG) |
| `post_invoice` | Park / auto-approve / post an AP invoice with GL entries |
| `request_approval` | Approve or reject a parked invoice on behalf of a named user |
| `list_pending_invoices` | List invoices currently awaiting approval |
| `submit_feedback` | Attach human correction to a prior observation; triggers a re-mine |

Tools are described to the model with full input schemas — see
`mcp_server/server.py`. Any plugin that wants to add a new concept (e.g. a
purchase-order matcher, a vendor-onboarding skill) does it by extending the
adapter and registering one new tool, not by touching the agent or the
schema.

For the architecture of dynamic context, dispatch, and the learning
substrate, read [`ARCHITECTURE.md`](ARCHITECTURE.md). For permissions,
audit, and the safety model, read [`SECURITY.md`](SECURITY.md). For how
Coat sits inside customer workflows without 24/7 polling and how
permissions are learned (not hand-coded), read
[`OBSERVABILITY.md`](OBSERVABILITY.md). For how any agent (Claude Code,
GSD, custom plugins, partner systems) identifies itself, requests
capabilities, and is enforced/audited at every call, read
[`AGENT_PROTOCOL.md`](AGENT_PROTOCOL.md). For how Coat actually lands
inside a customer (sidecar / cloud SaaS / hybrid bridge), the
configuration tree, the first-week onboarding flow, and the visibility
surfaces an admin or auditor uses to see learned patterns with their
confidence, read [`DEPLOYMENT.md`](DEPLOYMENT.md).

---

## Status

Private beta. Not for external distribution.
