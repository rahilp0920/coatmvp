# Coat — observability and learning

Three questions this doc answers:

1. How does Coat sit inside customer workflows without becoming a 24/7
   tail-and-poll expense?
2. How do we extract enough context to learn the company's true rules —
   including who is *allowed* to do what — without ingesting everything?
3. How do we capture not just *what* changed in the ERP, but *why* — the
   motivation chain that led up to the change?

The short version: we observe at the change boundary, not at the data
boundary. We filter cheaply before we think expensively. We treat
permissions as learnable, ratified rules — not hand-coded role tables.
And we treat *motivation* as a first-class signal, gathered from the
context window around each event, not from the event row itself.

This document explains how each of those works, where it sits in the
repo, and what is still open research.

---

## What Coat is not

- **Not a SIEM.** We are not building a security event lake.
- **Not a polling agent.** We do not periodically scan the customer's
  database for changes. That is expensive, invasive, and gets
  refused by every IT department worth its salt.
- **Not a webhook ingestor.** We do not ask the customer to forward
  every row-change event to our cloud. The volume is unworkable and the
  trust posture is hostile.
- **Not retraining a model per customer.** We do not fine-tune an LLM on
  customer data. We turn customer workflows into a queryable, ratifiable
  rulebook that the model consults at decision time.

What we *do*: subscribe to the change boundary, summarize cheaply,
escalate to the LLM only when novelty crosses a threshold, and surface
proposed rules for human ratification before any of them gates an
action.

---

## The four observation surfaces

Coat draws context from four distinct surfaces, in order of how cheaply
and reliably they're available:

### 1. The adapter log (today)

Every tool call through `mcp_server/adapter.py` writes a row to
`WORKFLOW_OBS` with actor, tool, args, result summary, outcome, and
timestamp. This is the cheapest surface — no extra infrastructure, no
external dependencies, deterministic. It captures every action the
agent or a plugin took *through Coat*.

The limitation is also obvious: **it does not capture what humans do in
the ERP directly.** A clerk who posts an invoice through SAP GUI never
shows up here. Surfaces 2 and 3 close that gap.

### 2. The ERP change boundary

Modern ERPs emit row-level change events without anyone polling. The
exact mechanism differs by backend. Coat reads from whichever one the
customer has, ignores the rest, and presents a uniform `ChangeEvent`
shape to the learner.

| ERP / DB | Mechanism | Notes |
|----------|-----------|-------|
| **SAP HANA / S/4 on HANA** | Smart Data Integration (SDI), CDS view delta tokens, log replay | Native. No row-by-row polling. Delta token gives "rows changed since cursor X." |
| **SAP ECC (Oracle/IBM DB2 backed)** | BAdI hooks (Business Add-Ins), BTE events (Business Transaction Events), CDHDR/CDPOS change documents | Standard SAP audit tables. `CDHDR` is the change-document header; `CDPOS` is the per-field diff. Most ECC customers already populate these. |
| **S/4HANA Cloud** | Business event handler (`/sap/opu/odata/sap/API_…/$batch`), SAP Event Mesh | Cloud-native, push-based. We register, we get notified. |
| **Oracle Fusion** | REST resources with `?since=<token>` pagination, plus Atom feeds for finance events | First-class delta support. |
| **NetSuite** | SuiteAnalytics Connect with audit-log filtering, RecordRef saved searches | Vendor's CDC story is weaker; we sample-and-diff for changed entities. |
| **Workday** | Workday Web Services with effective-dated retrieval, Reports-as-a-Service | Reports we own do the filtering. |
| **Generic Postgres / MySQL** | Logical replication slot, binlog tail | The "we already have a HANA-shaped story for everyone else" path. |

The unified shape we materialize on top of these:

```yaml
change_event:
  ts: 2026-05-03T14:11:08Z
  source: sap_s4_cloud
  entity: ap_invoice_header     # business concept, not table name
  entity_id: "INV-90237"
  op: update                    # create | update | delete
  before: { status: PARK }
  after:  { status: APPR, approver: u_mgr_c, approved_at: ... }
  actor: u_mgr_c
  session_id: GUI-58291
  upstream_refs: [PO-44102, GR-19938]
```

The adapter's existing `concept_map` is what makes this uniform — the
same concept name on the change boundary as on the tool surface.

### 3. The ERP transport boundary

For customers who can't enable CDC (older ECC, locked-down on-prem),
Coat sits as an OData / RFC proxy and observes the request stream.
This catches Fiori, GUI, and machine-to-machine calls. It is more
invasive than CDC and only used when CDC is unavailable.

The proxy emits the same `ChangeEvent` shape so downstream code is
identical. The cost: the proxy has to be in the network path, which is
a real deployment ask.

### 4. The human boundary

This is where the *motivation* lives. A vendor master got changed —
why? The reason is almost never on the row. It's in:

- The Slack thread that preceded the change ("hey can someone
  re-onboard Acme, they sent us new banking details?").
- The email from the vendor that triggered it.
- The ticket that asked for it.
- The approval comment the manager left.

Coat's role here is to *correlate*, not to ingest. We index the human
surface lightly (Slack, email, ticket system via existing connectors),
keep only metadata + short summaries, and join to ERP events on
`{actor, time-window, entity}`. The full body of the human-side
artifact stays in its own system; we keep a pointer.

This is the highest-value surface and the deepest research area. See
*Open research* below.

---

## Filtering: cheap before expensive

Calling Claude on every change event is the wrong default. The
observation pipeline is tiered so the LLM only sees novelty.

```
┌─────────────────────────────┐
│   Change events arriving    │
└──────────────┬──────────────┘
               │
        ┌──────▼──────┐
        │ Tier 1      │  Statistical: Counter, n-gram, sequence mining
        │ (cheap)     │  Updates pattern tables. No LLM.
        └──────┬──────┘
               │  emits "candidate pattern" or "novelty score"
               │
        ┌──────▼──────┐
        │ Tier 2      │  Rules: declared invariants, schema constraints
        │ (cheap)     │  e.g. "manager approves invoices ≤ $10k"
        └──────┬──────┘
               │  emits "rule mismatch" if event violates
               │
        ┌──────▼──────┐
        │ Tier 3      │  LLM: only when novelty > threshold
        │ (expensive) │  asks Claude to label intent, propose pattern
        └──────┬──────┘
               │  emits "proposed pattern (needs ratification)"
               │
        ┌──────▼──────┐
        │ Tier 4      │  Human ratification: weekly review or inline prompt
        │             │  promotes "candidate" → "trial" → "enforced"
        └─────────────┘
```

**Tier 1** is what `learner/miner.py` does today. It's deterministic
and runs on every Nth observation or every feedback row. Cheap.

**Tier 2** is `Z_APPR_RULES`-style declared rules plus schema-derived
invariants (FKs, NOT-NULL, type constraints). A rule mismatch is a
strong novelty signal even before the LLM looks.

**Tier 3** is where Claude enters. The LLM is asked to (a) label the
intent of a sequence, (b) propose a pattern that would explain it, and
(c) score whether existing patterns already cover it. We rate-limit
this tier — a sane default is "≤ 12 LLM calls per hour per customer
unless rate-limit override is explicit." Cost is bounded and
predictable.

**Tier 4** is the trust mechanism. Patterns proposed by Tier 3 do not
gate any action until a human ratifies. See *Confidence-gated learned
permissions* below.

This cascade is the answer to "doing this 24/7 is expensive." We do
the cheap thing 24/7. We do the expensive thing only when the cheap
thing produces a question the cheap thing can't answer.

---

## Intent windows: capturing motivation

A change in isolation is data. A change in context is information.

When Tier 1 sees an event, it materializes an *intent window* — the
trail of activity leading up to it. The window is cheap to construct:

- **Actor window**: the last K actions by the same actor across the
  last T minutes.
- **Session window**: every action with the same `session_id` (Fiori
  session, GUI mode, OData batch) until idle for >2 minutes.
- **Entity window**: every event touching the same business entity
  (`ap_invoice_header:INV-90237`) and its upstream refs (PO, GR).
- **Cross-system window**: matched human-surface artifacts (Slack,
  email, ticket) within ± T minutes of the event, by the same actor.

The window is what the learner reasons over. A `move_stock` decision
becomes interpretable when you see the four `get_stock` calls and the
two `find_item` calls that preceded it. A vendor master change becomes
interpretable when you see the email thread it followed.

The first three windows are constructible from the adapter log and
change boundary alone. The fourth requires the human-boundary
correlator.

---

## Confidence-gated learned permissions

Hand-coded permissions are brittle. Z_APPR_RULES says "manager approves
$1k–$10k", but the actual policy in this company is "manager Carol
approves vendor V1001 invoices up to $4,260, but only on cost center
4400, and only if the PO had a goods receipt." That's not a row in a
table. That's an empirical pattern.

Coat learns these by mining the change boundary, not by asking. The
lifecycle of a learned permission:

| Stage | What it means | What it can do |
|-------|---------------|----------------|
| **candidate** | Pattern emerged from Tier 1. Support and confidence below the trial threshold. | Logged. Surfaced in admin review. **Does not gate any action.** |
| **trial** | Crossed the trial threshold (e.g. support ≥ 5, confidence ≥ 0.6). | Annotates relevant tool results (`learned_note`). **Does not gate.** Plugins can read it. The agent surfaces it as a recommendation. |
| **enforced** | Human ratified the pattern. | Now influences decision logic — an `approval` rule fast-tracks an invoice; a `routing` rule overrides a default; a `read_access` rule blocks a query. |
| **demoted** | Pattern oscillated, contradicted by feedback, or aged out without re-confirmation. | Returns to *candidate* or is removed entirely. |

The promotion gate is the contract with the customer. **No pattern
mined from observations becomes an enforced rule until an authorized
human says "yes, this is how we do it."** That's how we earn the right
to learn aggressively.

The current MVP implements *candidate* and *trial* (any pattern that
crosses `MIN_SUPPORT=5` and `MIN_CONFIDENCE=0.6` shows up in
`LEARNED_PATTERNS` and influences `suggest_source_warehouse` and
`post_invoice`). The *enforced* and *demoted* states need a
ratification UI plus an audit-trail table — tracked as a near-term
extension.

---

## Configuration shape

Customers and plugins need a single place to declare *what gets
observed, how aggressively, and how patterns are promoted*. Proposed
shape, written to `config/learning.yaml`:

```yaml
observation:
  sources:
    - id: erp_adapter_log
      kind: workflow_obs
      retention: forever
    - id: sap_s4_cdc
      kind: cdc
      backend: sap_s4_cloud
      entities: [ap_invoice_header, ap_invoice_line, stock_movement, vendor]
      delta: hourly                # or "stream" for push-based
      retention: 90d
    - id: slack_pivots
      kind: human_correlator
      backend: slack
      channels: ["#ap-ops", "#warehouse"]
      retain: metadata_only        # never the message body
      window_minutes: 30

filtering:
  tier1:
    enabled: true
    miners: [routing, approval, sequence, anomaly]
    run_every_n_obs: 10
  tier2:
    enabled: true
    rules_path: config/declared_rules.yaml
  tier3:
    enabled: true
    model: claude-opus-4-6
    rate_limit:
      per_hour: 12
      per_day: 200
    novelty_threshold: 0.4
  tier4:
    ratification:
      surface: weekly_review        # or "inline_prompt"
      reviewers: [u_mgr_c, u_cfo]

learning:
  patterns:
    - kind: ROUTING
      promotion:
        candidate: { min_support: 3 }
        trial:     { min_support: 5,  min_confidence: 0.60 }
        enforced:  { requires_ratification: true }
    - kind: APPROVAL
      promotion:
        candidate: { min_support: 5 }
        trial:     { min_support: 8,  min_confidence: 0.75 }
        enforced:  { requires_ratification: true }
    - kind: ACCESS
      promotion:
        candidate: { min_support: 10 }
        trial:     { min_support: 25, min_confidence: 0.85 }
        enforced:  { requires_ratification: true,
                     reviewers: [u_cfo] }   # access patterns need higher sign-off

  decay:
    no_reconfirmation_after: 60d
    contradiction_demotes: true
```

This config is the contract with plugins. A plugin that wants to ask
"is there a learned access rule for vendor reads on cost center 4400?"
calls `list_patterns(kind=ACCESS, scope={vendor: ..., cost_center: ...})`
and gets either an enforced rule, a trial pattern (advisory), or
nothing. Same shape for every backend Coat is pointed at.

---

## How this maps to the current repo

The pieces above are not all built yet. Here is the extension map for
where each one slots in.

| Concern | Where it goes |
|---------|---------------|
| Cheap Tier 1 mining | `learner/miner.py` — extend with sequence-pattern + anomaly miners |
| Tier 2 declared rules | `config/declared_rules.yaml` (new) + `learner/rules.py` (new) |
| Tier 3 LLM escalation | `learner/llm_escalator.py` (new) — bounded, rate-limited |
| Tier 4 ratification | `mcp_server/adapter.py` — new tools `list_patterns`, `ratify_pattern`, `demote_pattern` |
| ERP change-boundary observers | `observers/` (new directory, sibling to `learner/`) — one module per backend (`sap_s4.py`, `ecc_baadi.py`, `postgres_cdc.py`) |
| Human-boundary correlators | `observers/human/` — one module per system (`slack.py`, `email.py`, `tickets.py`) |
| Intent window builder | `learner/windows.py` (new) |
| Configuration loader | `config/loader.py` (new) reading `config/learning.yaml` |

The architectural invariant: **all of this is downstream of
`WORKFLOW_OBS` plus the unified `ChangeEvent` shape**. New observers
emit `ChangeEvent`s. New miners read them. New tools surface results.
Nothing in the agent's tool surface changes; nothing in the discovery
or context.yaml format changes.

---

## Open research

Things that need real thinking before they ship to production
customers.

**Multi-system intent stitching.** Joining ERP events to Slack/email
threads by `{actor, time-window, entity}` is heuristic. False joins
are common. The right answer probably involves embedding both event
streams into a shared semantic space and ranking matches by similarity
under a temporal prior. We have not built this yet. It is the highest
single-feature step-change in observability quality.

**Sequence-pattern learning vs. Markov vs. DAG.** ROUTING and
APPROVAL today are flat statistical patterns. Real workflows are
sequences with branches: PO → GR → invoice → match → post.
Representing those as Markov chains or DAGs and mining them changes
what we can detect (out-of-order steps, missing-receipt invoices,
unusual approval paths). The data is in `WORKFLOW_OBS` already; the
miners aren't written.

**Adversarial drift.** A bad-actor operator who knows Coat is learning
can train it. They post 30 small invoices to a fake vendor, the
vendor_fast_track learns, then they post a big one. Defenses:
diversity-weighted confidence (one operator alone cannot promote a
pattern), demotion on contradiction with declared rules, time-decay,
and required ratification at the *enforced* gate. We have intuitions
but not a formal threat model.

**Permission lineage / explainability.** Every enforced permission
should answer "where did this rule come from? which observations
backed it? who ratified it? when?" That's a join of `LEARNED_PATTERNS`,
`WORKFLOW_OBS`, and a future `RATIFICATIONS` table. The schema is
small but the UX of presenting it cleanly to a finance auditor is the
research piece.

**The "ask the LLM why" trap.** It is tempting to escalate every
unexplained event to Claude with the question *"why did this user do
this?"*. Over-using this turns the LLM into the rule book and removes
the grounding. The discipline is: **the LLM proposes structure, the
data confirms it, the human ratifies it.** Anywhere we drift from that
order, we are renting the rules instead of owning them.

---

## Summary

We sit inside customer workflows by reading from the ERP's own change
boundary, not by polling. We extract context cheaply by tiered
filtering — counters and rules first, LLM only on novelty, with a
small per-hour budget. We capture motivation by windowing each event
against the actor's recent activity, the session, the entity's
upstream history, and lightly-correlated human-surface artifacts. We
turn observations into permissions through a four-stage lifecycle
(candidate → trial → enforced → demoted) where promotion to enforcing
status requires explicit human ratification.

Everything above is configurable per-customer through a single
`learning.yaml`, and every backend Coat speaks emits the same
`ChangeEvent` shape so the upstream code is uniform.

The MVP today implements the adapter log, statistical Tier 1 mining,
and the trial stage of the lifecycle. The path from here to the full
system is incremental and the extension points are explicit.
