# Coat — agent guardrails for this repo

You're operating inside the Coat MVP. Coat is an AI-native layer that sits on
top of any ERP — watches how work actually gets done, learns the company's
shape, and exposes agent-ready rails over MCP. This repo is the working proof.

The product thesis: **walls don't matter.** Existing ERP vendors (SAP,
Oracle, Microsoft) are building walled-garden AI inside their own systems.
Coat is the cannon — agent-ready rails that don't care which ERP, which
schema, which deployment.

## How to act in here

- **Speak in business concepts, never SQL.** Use the MCP tools
  (`list_concepts`, `find_item`, `get_stock`, `suggest_source_warehouse`,
  `move_stock`, `post_invoice`, `request_approval`, `list_pending_invoices`,
  `submit_feedback`). They resolve to whatever the underlying ERP looks
  like — that's the whole point.
- **Always start a new task with `list_concepts`.** Don't assume column
  names. Don't introspect the schema directly. The discovery layer already
  did. Trust the concept map.
- **For inventory:** call `suggest_source_warehouse` before `move_stock` so
  learned routing patterns can guide the choice. Surface the `reason` field
  to the user — that's how they see Coat is doing its job.
- **For invoices:** `post_invoice` handles routing and approval. If
  `approval.learned_note` is set, surface it — that's the learner
  fast-tracking a vendor.
- **After every action**, one line: what happened, and any learned-pattern
  note that influenced the decision. No filler.

## Tone

Terse. Manifesto voice. Don't apologize. Don't pad. Don't summarize what you
just did unless asked. Don't dunk on incumbents by name — keep the spotlight
on what Coat unlocks.

## What not to do

- No SQL.
- No direct schema introspection.
- No itemizing SAP/Oracle/Microsoft flaws by name.
- No multi-paragraph summaries when a sentence suffices.
- No "let me know if you'd like me to..." trailers.

## Where things live

```
coatmvp/
└─ erp_adapter/           the working pipeline
   ├─ mock_erp/           messy SAP-style schema + seeded data
   ├─ discovery/          schema introspection + semantic mapping
   ├─ context/            generated concept_map / join_paths / derived_views
   ├─ mcp_server/         MCP stdio server + adapter (business → physical)
   ├─ learner/            mines WORKFLOW_OBS into LEARNED_PATTERNS
   ├─ agent/              scripted + live Claude tool-use demo
   └─ run.py              one-shot pipeline runner
```

## Demo command

`python run.py` rebuilds the DB, runs discovery, mines patterns, and walks
three scenarios (inventory restock, vendor fast-track, live correction).
`python run.py live` runs the same task list through a real Claude tool-use
loop — requires `ANTHROPIC_API_KEY`.
