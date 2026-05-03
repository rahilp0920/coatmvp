# Coat — deployment, configuration, visibility

This doc answers three concrete questions a buyer will ask in their
first meeting:

1. *"Where does Coat actually run, and how does it touch our ERP?"*
2. *"What does the first week look like? What are we configuring?"*
3. *"How do we see what Coat has learned, what it's about to enforce,
   and what it has already done?"*

The architecture exists in `OBSERVABILITY.md` and `AGENT_PROTOCOL.md`.
This doc is about how it lands inside a real customer.

---

## Three deployment shapes

Coat fits into customer environments three ways. Each has a different
trust posture, ops footprint, and what's deployed where. Pick by the
ERP, not by preference.

| Shape | Where Coat runs | Where the bridge runs | Best fit |
|-------|-----------------|------------------------|----------|
| **Sidecar** | Inside the customer's network (their VPC, their datacenter, behind their firewall) | n/a — Coat *is* the in-network process | Locked-down ECC. Banks, defense, healthcare. Customers who refuse outbound traffic. |
| **Cloud SaaS** | In Coat's cloud | n/a — Coat reaches in over public APIs (S/4 Cloud, NetSuite, Workday) | Greenfield mid-market and any customer already operating their ERP as SaaS. |
| **Hybrid bridge** | In Coat's cloud | Small "Coat Bridge" agent inside the customer's network | Enterprise SAP, especially S/4 on-prem and ECC. The default for serious accounts. |

### Sidecar

The simplest trust story and the heaviest ops footprint. The customer
deploys Coat as a containerized service (Helm chart, Docker Compose,
or a single binary) onto a VM or pod they own. Coat's MCP server,
adapter, learner, and observers all run there. Coat reaches the ERP
on whatever internal network the ERP is on (RFC, JCo, OData over
private network).

The customer's ops team is on the hook for upgrades, patching, and
log retention. Coat ships an admin CLI and a per-tenant config bundle,
nothing else.

### Cloud SaaS

The lowest-touch deployment. Customer hands Coat-cloud a
service-account token for their ERP (S/4 Cloud communication arrangement,
NetSuite TBA token, Workday integration system user). Coat reaches in
through allowlisted outbound endpoints from their fixed egress IPs.

Works well for customers whose ERP is already SaaS, less well for
on-prem ECC with no externalized API. mTLS at the egress + signed
audit logs at rest.

### Hybrid bridge — the default for enterprise SAP

Most serious customers will not let Coat-cloud connect directly to
their HANA. They will deploy a small **Coat Bridge** agent inside
their network. The bridge:

- Subscribes to the ERP's change boundary (CDHDR/CDPOS, BAdI, SDI,
  business event handlers — whichever the customer's ERP exposes).
- Normalizes events into the unified `ChangeEvent` shape (see
  `OBSERVABILITY.md`).
- Forwards events to Coat-cloud over an outbound mTLS tunnel,
  initiated by the bridge (no inbound holes punched in their
  firewall).
- Receives back tool-call requests from Coat-cloud (when a plugin
  asks Coat to act) and proxies them to the ERP using the
  least-privileged communication user inside the customer's network.

This is the shape Snowflake, Datadog, and every modern enterprise SaaS
uses to live inside a Fortune-500 SAP shop. It's the right default.

```
┌──────────────────────────────────────────────────┐
│  Coat Cloud (managed by Coat)                    │
│  ┌────────────────────────────────────────────┐  │
│  │  MCP server   adapter   learner   admin UI │  │
│  └────┬─────────────────────────────────┬─────┘  │
│       │                                 │        │
└───────┼─────────────────────────────────┼────────┘
        │ outbound mTLS, agent-initiated  │
┌───────▼─────────────────────────────────▼────────┐
│  Customer Network                                │
│  ┌────────────────────────────────────────────┐  │
│  │  Coat Bridge  (single Go binary or pod)    │  │
│  │   • subscribes to ERP change boundary      │  │
│  │   • normalizes events, ships to cloud      │  │
│  │   • proxies tool calls back to ERP         │  │
│  └─────────────────┬──────────────────────────┘  │
│                    │                              │
│  ┌─────────────────▼──────────────────────────┐  │
│  │  Customer's ERP (S/4, ECC, Oracle, …)      │  │
│  └────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────┘
```

The bridge is the only piece that ever has direct ERP access. Coat-cloud
only ever sees normalized events and answers normalized tool calls.

---

## First-day onboarding

The first week with a new customer is one command and three review
sessions.

### `coat init`

A single command does all of:

1. Validates connections declared in `config/connections/*.yaml` (can
   we authenticate to the ERP? to Slack? to email?).
2. Runs schema introspection against the ERP, produces
   `context/dossier.json`.
3. Asks Claude to label the dossier semantically, writes
   `context/context.yaml`. Falls back to a curated mapping if the
   LLM is offline.
4. Subscribes to the ERP's change boundary (CDHDR-tail for ECC,
   delta-token for S/4, logical replication for Postgres).
5. Walks back over a configurable history window (default: 90 days)
   and replays change events into `WORKFLOW_OBS`. This is what
   gives the learner something to mine on day one.
6. Runs a Tier-1 mining pass against the replayed history. All
   patterns produced are at status `candidate` — none enforce yet.
7. Writes a per-customer summary report: how many concepts were
   discovered, how many patterns were proposed, what the
   confidence distribution looks like.

That's the entire bootstrap. It runs in ~10 minutes for a customer
with one ERP connection and a 90-day history window.

### The three review sessions

After `coat init` runs, the customer reviews:

- **Session 1: the concept map.** A domain expert (controller, IT
  business analyst) walks through `context/context.yaml` with Coat's
  CSM. Anything mislabeled gets fixed by editing the YAML directly —
  that file is the contract, and it's editable. Most customers find
  one or two custom Z-tables that need the right concept binding.
- **Session 2: declared rules.** The customer fills in
  `config/declared_rules.yaml` with the policies they *know* exist —
  approval brackets, blocked vendors, segregation-of-duties
  invariants. Tier 2 of the filtering cascade reads from here.
- **Session 3: pattern ratification.** Coat presents the candidate
  pattern catalog. The reviewer goes through each, sees the
  supporting observations, and either ratifies (promotes to enforced),
  rejects (deletes), or marks as trial-only (visible to agents as
  advisory, doesn't enforce).

After session 3, Coat is live. New events flow through the cascade.
Patterns mature. Agents act, scoped by capability.

---

## Configuration layering

A customer's Coat configuration lives in one tree:

```
coat/
├─ coat.yaml                        top-level: tenant_id, deployment_shape, version
├─ config/
│  ├─ connections/
│  │  ├─ erp_sap_s4.yaml            host, auth, OData entities, throttle
│  │  ├─ erp_ecc_jco.yaml           SAP JCo + RFC modules + BAdI subscription
│  │  ├─ slack.yaml                 workspace, channels to correlate, retention
│  │  └─ email.yaml                 IMAP, sender allow-list
│  ├─ learning.yaml                 tier thresholds, pattern promotion gates
│  ├─ declared_rules.yaml           Tier-2 invariants
│  └─ agents/
│     ├─ vendor_onboard.yaml        agent manifest (id, pubkey, scope manifest)
│     ├─ monthly_close.yaml
│     └─ stock_rebalance.yaml
└─ context/
   ├─ dossier.json                  produced by discovery, regenerable
   └─ context.yaml                  the editable concept map (the contract)
```

### `coat.yaml` — the top-level

```yaml
tenant_id: acme-corp
deployment_shape: hybrid_bridge       # sidecar | cloud_saas | hybrid_bridge
version: 0.4
environments:
  prod:
    bridge_endpoint: bridge.acme.internal:7443
    cloud_region: us-east-1
  staging:
    bridge_endpoint: bridge.acme-staging.internal:7443
    cloud_region: us-east-1

admin_reviewers:
  - name: Carol Singh
    user_id: u_mgr_c
    role: MANAGER
    can_ratify: [ROUTING, APPROVAL]
  - name: Dana Park
    user_id: u_cfo
    role: CFO
    can_ratify: [ROUTING, APPROVAL, ACCESS]      # ACCESS needs CFO

notifications:
  proactive_surfacing: slack
  channel: "#coat-review"
  weekly_digest: true
```

### `config/connections/erp_sap_s4.yaml`

```yaml
id: erp_sap_s4
backend: sap_s4_cloud
base_url: https://my-tenant-api.s4hana.cloud.sap
auth:
  kind: communication_user
  user_secret_ref: vault://kv/coat/acme/s4_user
  comm_arrangement: SAP_COM_0027

entities:
  - api: API_PURCHASEORDER_PROCESS_SRV
    concept: purchase_order
    delta: hourly
  - api: API_SUPPLIERINVOICE_PROCESS_SRV
    concept: ap_invoice_header
    delta: stream
  - api: API_MATERIAL_STOCK_SRV
    concept: stock_by_warehouse
    delta: hourly

throttle:
  requests_per_minute: 240
  burst: 60

retention:
  raw_events: 90d
  normalized_events: 365d
```

### `config/agents/vendor_onboard.yaml`

This is the agent manifest specified in `AGENT_PROTOCOL.md`:

```yaml
agent:
  id: vendor-onboard@coat.io/v3
  pubkey_path: keys/vendor-onboard-v3.pub
  description: "Onboards new vendors from W-9 PDFs and bank-detail emails."

requested_scopes:
  - coat:concepts:read
  - coat:vendor:read
  - coat:vendor:write
  - coat:patterns:read

resource_constraints:
  - vendor.country: ["US", "CA"]
  - invoice.max_amount: 0

duration:
  max_session_seconds: 3600
  rotate_keys_after_days: 30
```

The point of layering: each file is the right size for its audience.
The controller edits `context.yaml`. The IT admin edits
`connections/`. The compliance officer edits `declared_rules.yaml`
and `coat.yaml`'s `admin_reviewers`. The plugin author writes their
own `agents/<plugin>.yaml`. No one edits ten files at once.

---

## Visibility — what an admin sees

This is the answer to "how do we see learned patterns with their
confidence." Two surfaces, both backed by the same underlying tables.

### Programmatic surface (the truth)

New MCP tools on the adapter, exposed under `coat:patterns:*` and
`coat:audit:*` scopes:

| Tool | Returns |
|------|---------|
| `list_patterns(kind?, scope?, status?)` | Pattern catalog rows: id, kind, key, scope, status, support, confidence, ratified_by, last_used, last_observation_id |
| `pattern_detail(pattern_id)` | Full provenance: contributing observations, confidence trend over the last 30/60/90d, current capability grants derived from this pattern, which agents currently hold those grants |
| `pattern_history(pattern_id)` | Lifecycle log: candidate → trial → enforced → demoted, each transition with timestamp, actor, reason |
| `ratify_pattern(pattern_id, decision, note)` | Promotion to enforced (decision=APPROVE) or hard delete (decision=REJECT) or trial-pinned (decision=TRIAL_ONLY). Writes to RATIFICATIONS audit table. |
| `demote_pattern(pattern_id, reason)` | Force-demote a currently enforced pattern. Revokes derived capabilities on next handshake. |
| `list_grants(agent_id?)` | Currently active capability grants, by agent. Each row carries the originating pattern_id (or "manual" if directly granted by admin). |
| `audit_query(filters)` | Free-form WORKFLOW_OBS search by actor, tool, entity, time range, outcome. |

A plugin, dashboard, or another agent that wants to render the pattern
catalog calls `list_patterns` and gets the live state.

### Human surface (the catalog view)

The CLI / web view renders the same data with one additional cut:
**confidence as a visual bar** so a finance lead can scan a hundred
patterns in two minutes.

```
COAT — PATTERN CATALOG  (tenant: acme-corp, refresh: 2026-05-03 14:11:08 UTC)

KIND       KEY                                   STATUS    SUP  CONF       RATIFIED  LAST USED
─────────────────────────────────────────────────────────────────────────────────────────────
ROUTING    fragile_source=WH02                   enforced   38  ████░ 0.84  u_mgr_c   2 min ago
APPROVAL   v=V1001 ≤ $4,260 → u_mgr_c            enforced   11  █████ 1.00  u_cfo     14 min ago
ROUTING    hazmat_source=WH03                    candidate   2  ██░░░ 0.50    —         —
APPROVAL   v=V1003 ≤ $13,750 → u_cfo             trial       3  ███░░ 0.67    —         —
PREFERENCE feedback: SKU-200 prefer WH03         trial       1  █████ 1.00    —       45 min ago
ACCESS     u_clerk_a reads vendor.bank_*         candidate  21  ████░ 0.81    —         —

  [r]atify     [d]emote      [v]iew detail      [a]udit query      [/] filter
```

Drill-in (`pattern_detail`) shows:

```
PATTERN  pat_01HV3K… —  APPROVAL: v=V1001 ≤ $4,260 → u_mgr_c
status   enforced (since 2026-04-29 11:02:00 UTC, ratified by u_cfo)

PROVENANCE
  observation count       11
  date range              2026-04-12  →  2026-04-28
  contributing actors     u_mgr_c (10 of 11), u_clerk_b (1 of 11)
  amount distribution     $620 — $3,872 (mean $1,790, max $3,872, ceiling set 1.1× = $4,260)
  confidence (30d trend)  0.89 → 0.93 → 1.00

DERIVED CAPABILITIES (currently granted)
  • vendor-onboard@coat.io/v3        coat:invoice:post@vendor=V1001,max=4260
  • monthly-close@coat.io/v1         coat:invoice:approve@vendor=V1001,max=4260
  • gsd@coat.io/local-001            coat:invoice:post@vendor=V1001,max=4260

LIFECYCLE
  2026-04-15 09:14:21 UTC  candidate → trial      (support=8 reached threshold)
  2026-04-29 11:02:00 UTC  trial → enforced       (ratified by u_cfo,
                                                   note "consistent with our
                                                   long-standing AP practice")

  [d]emote     [r]otate cap     [v]iew observations     [b]ack
```

### The ratification queue

A specific view sorted by *impact*, not by chronology — what's worth a
domain expert's attention next.

```
COAT — RATIFICATION QUEUE  (acme-corp, 14:11:08 UTC)

PATTERN                                STATUS    PROJECTED MONTHLY CALLS  REVIEWER
──────────────────────────────────────────────────────────────────────────────────
APPROVAL  v=V1004 ≤ $5,200 → u_mgr_c   trial     ~22 invoices/mo          u_cfo
ROUTING   high-weight items → WH01     candidate ~310 moves/mo            u_mgr_c
ACCESS    u_clerk_b reads gl.*         candidate (security-sensitive)     u_cfo

  Selecting a row opens pattern_detail; ratification is one keystroke.
```

Impact is computed by replaying the last 30d of `WORKFLOW_OBS` against
the proposed rule and counting how many decisions would have been
shaped by it.

### Audit timeline by entity

For an auditor asking "what did Coat see and decide about INV-90237?"

```
ENTITY TIMELINE — ap_invoice_header / INV-90237  (acme-corp)

2026-04-26 09:11:42  CREATE   actor=u_clerk_a    tool=post_invoice
                              args={vendor:V1001, amount:$2,100}
                              outcome=OK status=POST
                              learned_note: "vendor V1001 ≤ $4,260 fast-track (pat_01HV3K…)"
2026-04-26 09:11:43  POST     two GL entries, balanced.
2026-04-26 09:11:43  AUDIT    audit_id=aud_01HV3M…   agent_id=vendor-onboard@coat.io/v3
                              granted_scope=coat:invoice:post@vendor=V1001,max=4260
                              capability_origin=pat_01HV3K…
```

Provenance is everything. Every event chains back through the
capability that authorized it, the pattern that derived the capability,
the observations that backed the pattern, and the human who ratified
it.

---

## Operations

`coat` is a single CLI that fronts the daemon. The verbs that matter
on day-to-day:

| Verb | What it does |
|------|--------------|
| `coat init` | First-day bootstrap (above) |
| `coat up` / `coat down` | Start / stop the daemon (bridge or sidecar) |
| `coat status` | Per-observer health: last successful CDC tick, last LLM call, queue depth, current rate-limit budget |
| `coat agent register / revoke` | Manage agent identities (per `AGENT_PROTOCOL.md`) |
| `coat pattern list / view / ratify / demote` | Pattern catalog operations from the terminal (mirror of the visibility tools above) |
| `coat rotate-keys` | Rotate per-agent and per-bridge keypairs on the configured cadence |
| `coat replay --since <date>` | Replay history from the change boundary into `WORKFLOW_OBS`. Used after a fresh deploy or to onboard a new ERP connection. |
| `coat upgrade` | Schema migrations on `LEARNED_PATTERNS`, `WORKFLOW_OBS`, `SESSIONS`, `CAPABILITY_GRANTS` |

Health endpoints (`/healthz`, `/readyz`) for K8s. Per-tenant Prometheus
metrics so customer ops teams can alert on stuck observers, LLM
budget exhaustion, or pattern-promotion velocity.

---

## Mapping to current repo

The MVP today doesn't have most of this — it has the adapter, the
discovery layer, the in-process learner, and the scripted demo. Here's
the additive build map.

| Concern | Where it goes (new) | Status |
|---------|---------------------|--------|
| `coat` CLI | `cli/coat/` (new) | not yet |
| Bridge agent | `bridge/` (new) — Go or Python single binary, mTLS outbound | not yet |
| Per-ERP observers | `observers/sap_s4.py`, `observers/ecc_baadi.py`, `observers/postgres_cdc.py` | not yet |
| Tenant config loader | `config/loader.py` reading `coat.yaml` + connections + agents | not yet |
| `RATIFICATIONS`, `SESSIONS`, `CAPABILITY_GRANTS` tables | `mock_erp/schema.sql` extension or new `meta_schema.sql` | not yet |
| Visibility MCP tools | `mcp_server/server.py` extension: `list_patterns`, `pattern_detail`, `pattern_history`, `ratify_pattern`, `demote_pattern`, `list_grants`, `audit_query` | next sprint |
| Pattern catalog renderer | `cli/coat/render/catalog.py` | next sprint |
| Web admin UI | `web/` (new) — same data, browser-rendered | post-MVP |

The next sprint slice is bounded: ~7 new tools on the existing MCP
server, plus the `RATIFICATIONS` / `SESSIONS` / `CAPABILITY_GRANTS`
tables. That alone gives any plugin or dashboard the full visibility
surface without touching the bridge or the observer layer. We can land
those before the bridge is real and demo against the sandbox.

---

## Open questions

- **Proactive surfacing vs. weekly digest.** Is the right default
  Slack-pinging the reviewer the moment a high-confidence candidate
  appears, or a weekly batched digest? Pinging is more responsive,
  digesting is less interrupt-y. Suspect: weekly digest by default,
  with per-pattern-kind override (always-ping for `ACCESS`).
- **Auto-promotion thresholds.** Can an admin pre-authorize "any
  ROUTING pattern that crosses 0.95 confidence with 100+ support is
  auto-enforced without per-pattern ratification"? Tradeoffs: less
  human friction, more attack surface. Probably a per-tenant feature
  flag with conservative defaults.
- **Cross-tenant pattern sharing.** Two customers in the same industry
  may converge on similar approval brackets. Sharing anonymized
  patterns ("Coat Industry Benchmark — your ratified rules vs.
  manufacturing peers") is a feature with real product value and real
  privacy questions. Out of scope for v1.
- **Bridge resilience.** The bridge is a single point of failure
  inside the customer's network. Active/standby vs. quorum vs.
  resume-from-cursor on restart. Not hard, just has to be designed.
- **Ratification UX in low-trust orgs.** Some customers will not have
  a single "controller who can ratify" — they have a committee.
  Multi-signer ratification on high-stakes scopes
  (`ACCESS`, `coat:invoice:approve@max>$X`) is a real ask, and it
  changes the schema (`RATIFICATIONS` becomes a chain).

---

## What lands when

- **Today's MVP:** scoped tool surface + adapter + Tier-1 learner +
  scripted demo. Visibility is `python run.py inspect`.
- **Next sprint:** the seven visibility tools land on the MCP server.
  Pattern catalog and pattern detail are renderable in CLI. Sandbox
  customers can ratify from the terminal.
- **Sprint after:** bridge agent (single ERP), tenant config loader,
  `coat init` happy-path. First real customer onboardable.
- **Beta:** web UI, multi-ERP, multi-reviewer ratification, proactive
  surfacing, the Slack/email correlator.

The sprints are sized so each one is independently demoable. Nothing
above requires a flag day or a rewrite.
