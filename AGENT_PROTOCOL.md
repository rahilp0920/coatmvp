# Coat — agent protocol

This is how any agent — Claude Code, GSD, a custom plugin, a scheduled
task, an external partner's system — plugs into Coat's MCP-exposed
context. The point is not "we expose an API." The point is **a
protocol with verifiable identity, scoped capabilities, and an audit
trail that ties every action back to the agent that took it.**

API keys are not enough. The Coat Agent Protocol (CAP) is what makes
the difference between "an agent can call Coat" and "an agent can call
*these* tools, on *these* entities, up to *these* limits, with every
call signed and logged."

This doc specifies the protocol, maps it to the current repo, and lists
the open research that has to ship before production.

---

## Why a protocol, not just an API key

A bearer token says *who is calling*. It does not say *what they're
allowed to do*. For Coat that distinction matters because:

- The same MCP server surface answers a vendor-onboarding plugin, a
  CFO's monthly close agent, a third-party AP automation tool, and a
  scheduled stock-rebalancer. Each of these has dramatically different
  blast radius if it misbehaves. They cannot all share a key.
- Permissions are *learned*, not statically assigned. A capability that
  did not exist last Tuesday — "auto-post V1001 invoices ≤ $4,260" —
  may exist next Tuesday because the human ratified a learned pattern.
  The protocol has to plumb that lifecycle into agent permissions in
  near-real-time.
- Every call has to leave a forensic trail. "An agent did this" is not
  enough; "agent `vendor-onboard@coat.io/v3` acting under capability
  `coat:vendor:write@scope=country=US` at 14:11:08 UTC" is the bar.

So the protocol layers four things on top of MCP: **identity,
capabilities, handshake, enforcement.**

---

## Identity

Every agent that talks to Coat has a stable, registered identity.

The protocol's primary UX is **inferred-scope onboarding** — the admin
describes the agent's job in plain English, Coat derives the manifest,
the human ratifies. This is the "as easy as wearing a coat" path and
is the default for first-party plugins, in-house agents, and recorded
demos.

Explicit-manifest registration (the schema below) still exists for
production-grade plugins authored by a third party who needs the
contract to be a static artifact. Both paths land at the same
`manifest.yaml`; the difference is who wrote it.

The inferred-scope path is documented in §"Manifest derivation"
below. The schema and handshake apply to both paths identically.

Registration produces:

- An `agent_id` — opaque to the agent, the canonical handle inside
  Coat (`vendor-onboard@coat.io/v3`, `gsd@coat.io/local-001`).
- A keypair — the agent holds the private key, Coat keeps the public
  key. All handshake messages from the agent are signed.
- A *declared scope manifest* — what the agent claims it needs.

Registration is *not* permission to act. It's permission to *ask*.
The actual scopes granted are decided at handshake time, against the
declared manifest plus current learned-permission state plus any
explicit human ratifications.

```
coat agent register \
    --id "vendor-onboard@coat.io/v3" \
    --pubkey ./agent.pub \
    --manifest ./manifest.yaml
```

Where `manifest.yaml` declares:

```yaml
agent:
  id: vendor-onboard@coat.io/v3
  description: "Onboards new vendors from W-9 PDFs and bank-detail emails."

requested_scopes:
  - coat:concepts:read
  - coat:vendor:read
  - coat:vendor:write
  - coat:invoice:read
  - coat:patterns:read       # to surface "what has Coat learned about this vendor"

resource_constraints:
  - vendor.country: ["US", "CA"]
  - invoice.max_amount: 0    # this agent never posts invoices

duration:
  max_session_seconds: 3600
  rotate_keys_after_days: 30
```

Coat persists the manifest. Granting it is a separate step.

---

## Manifest derivation — how scopes are inferred from a description

The protocol's primary onboarding UX is a single command:

```
$ coat agent onboard
What does this agent do? Describe it like you'd describe it to a new hire.

> Atlas — an inventory planning specialist. Looks at our stock,
> recent movements, and outside data like weather and shipping
> disruptions to predict next week's stockout risk per SKU and
> recommend reorder quantities. It needs to read inventory and
> movements; it doesn't post anything to the ERP itself.

PROPOSED AGENT — atlas@coat.io/v1
Inferred scopes (least privilege):
  ✓ coat:concepts:read
  ✓ coat:inventory:read
  ✓ coat:patterns:read
  ✗ coat:inventory:write    NOT granted (description: "doesn't post anything")
  ✗ coat:invoice:*          NOT granted (out of role)
Mode: trial — 50 calls or 7 days, whichever first
  [r]atify   [e]dit   [c]ancel
```

The admin never types a scope string. The cognitive load is the
description — same load as briefing a new hire. The mechanics are
Coat's job.

### How the inference works

Three stages, in order, with each stage's output gating the next:

**Stage 1 — Task extraction.** A small Claude call takes the
description and Coat's full tool catalog (with descriptions) and
returns: a list of high-level tasks the agent will perform; the
data classes it needs to read and write; an explicit list of things
the description says it will *not* do. Output is structured JSON,
not free text.

**Stage 2 — Scope projection.** A deterministic mapper takes the
task list and projects it onto the scope grammar. `(read inventory
data) → coat:inventory:read`. `(reads vendor records, US-only) →
coat:vendor:read@country=US`. The mapper is rule-based and auditable;
no LLM in this step. The "NOT granted" lines are explicit — every
verb the description ruled out is listed with the reason.

**Stage 3 — Ratification.** The admin sees the proposal in
plain English next to the formal scope strings, presses one key,
and the agent is registered with that manifest. Trial mode is on
by default; promotion to enforced requires re-ratification after
the trial budget is consumed.

### Why this is safe

Three properties make inferred-scope onboarding *more* secure than
manual scope authoring, not less.

- **Least privilege by construction.** The description names what
  the agent will do. Anything not in the description is denied. The
  mapper does not invent capabilities. There is no "or maybe also
  this one, just in case."
- **Explicit denial list.** Every common adjacent capability the
  agent could reasonably want is enumerated and explicitly denied —
  not just absent. The admin sees the negative space.
- **Trial mode is the default.** No inferred manifest enforces a
  high-stakes capability on day one. The agent runs advisory-only
  for a budgeted period; usage data feeds the next ratification
  decision.
- **Mid-flight ratification, not silent escalation.** When an agent
  hits the edge of its granted scope at runtime, the protocol does
  not fail silently and it does not auto-grant. It surfaces a
  request to the admin: *"Atlas is asking for capability X. Approve?"*
  One human decision per scope expansion. Audited end to end.

### Fallback when offline

If Claude is unreachable during stage 1, Coat falls back to a
curated keyword-to-scope mapping (e.g., "inventory" → inventory:read,
"post invoice" → invoice:post, "approve" → invoice:approve). The
fallback errs on the side of denying — when the keyword mapping is
ambiguous, the scope is left unrequested and the admin is prompted
to add it manually. This guarantees `coat agent onboard` always
completes, even in air-gapped environments, without ever
over-granting.

### When to use explicit-manifest registration instead

The inferred path is right for ~90% of agents. Use the explicit
schema (§Identity above) when:

- The agent is authored by a third-party vendor and the manifest is
  contractual — must be reviewable as a static artifact in the
  vendor's repo.
- The agent's capability set is unusually narrow or unusual (a
  break-glass emergency agent that only fires on incident; a
  dry-run audit agent with read-only access to specific entities).
- Policy requires a signed manifest reviewed by a security
  committee before any registration.

Both paths land at the same `manifest.yaml` schema and the same
handshake protocol. The difference is who authored the file.

---

## Capabilities — the scope language

A capability is a tuple `(verb, resource, constraints)` that the agent
is allowed to exercise. The base scope grammar:

```
scope := <namespace>:<resource>:<verb>[@<constraint_set>]

namespace := "coat"
resource  := concepts | vendor | item | inventory | invoice
           | patterns | feedback | audit | agents
verb      := read | write | post | approve | ratify | revoke
constraint_set := key=value [, key=value]*
```

Examples:

```
coat:concepts:read
coat:inventory:read
coat:inventory:write@warehouse=WH02
coat:invoice:post@vendor=V1001,max_amount=4260
coat:invoice:approve@max_amount=10000
coat:patterns:read
coat:patterns:ratify           # high-trust; usually only humans
coat:agents:revoke
```

The verbs are deliberately small. New verbs require schema review and
a major version bump on the protocol.

### Resource-scoped capabilities

The *constraint set* is what makes the protocol expressive enough to
encode learned permissions. A capability like

```
coat:invoice:post@vendor=V1001,cost_center=4400,max_amount=4260
```

is exactly the shape of an *enforced* learned pattern from
`OBSERVABILITY.md`. Granting that capability to an agent is the
mechanical equivalent of ratifying the pattern.

This is the bridge: **enforced learned patterns become agent
capabilities.** The promotion gate (human ratification) and the
authorization gate are the same gate.

---

## Handshake

When an agent connects to Coat's MCP server, three things happen
before the first tool call.

### Step 1 — present identity

The agent sends a signed `agent.hello` envelope:

```json
{
  "agent_id": "vendor-onboard@coat.io/v3",
  "nonce": "b2c8...",
  "requested_scopes": [
    "coat:concepts:read",
    "coat:vendor:read",
    "coat:vendor:write",
    "coat:invoice:read",
    "coat:patterns:read"
  ],
  "resource_constraints": {
    "vendor.country": ["US", "CA"]
  },
  "signed": "<ed25519 signature over the canonicalized envelope>"
}
```

Coat verifies the signature against the registered public key. A
failed signature drops the connection with `auth.invalid_identity`.

### Step 2 — evaluate permissions

Coat computes the *granted scope set* for this session as the
intersection of:

1. The agent's manifest (what it pre-declared on registration).
2. The requested scopes in this `agent.hello` (it can ask for less,
   never more).
3. The currently *enforced* capabilities that match (from the learned
   permission lifecycle).
4. Any policy overrides set by the human admin (deny-list, throttles).

If the granted set is empty, the connection drops with
`auth.no_grants`. Otherwise Coat issues a session.

### Step 3 — issue session

```json
{
  "session_id": "sess_01HV3...",
  "granted_scopes": [
    "coat:concepts:read",
    "coat:vendor:read",
    "coat:vendor:write@country=US,country=CA"
  ],
  "tool_catalog": [ /* MCP tools restricted to granted_scopes */ ],
  "expires_at": "2026-05-03T15:11:08Z",
  "audit_id": "aud_01HV3..."
}
```

Two things to notice:

- **The `tool_catalog` is restricted.** An agent that lacks
  `coat:invoice:post` does not see `post_invoice` in its MCP tool
  list at all. We do not return tools and reject calls — we return a
  reduced surface. This prevents the model from even forming a plan
  that requires unavailable tools.
- **`audit_id` opens an audit chain.** Every tool call in this session
  is bound to this audit_id. Revocation, replay, and forensic queries
  all key off it.

Sessions are short-lived (default 1 hour). Long-running agents
re-handshake.

---

## Per-call enforcement

Every tool call carries the session token. Inside `mcp_server/server.py`
the dispatch order is:

```
call_tool(name, args, session_token)
  ├─ verify session_token signature + expiry
  ├─ confirm tool 'name' is in this session's granted scopes
  ├─ confirm args satisfy the resource constraints
  │     e.g. post_invoice with vendor=V9999 fails if scope is
  │     coat:invoice:post@vendor=V1001
  ├─ confirm any amount/quantity in args is below the constraint ceiling
  ├─ adapter.<name>(actor=session.agent_id, **args)
  └─ _log_obs(..., audit_id=session.audit_id)
```

A scope-mismatch failure does not leak which constraint failed by
default — it returns `cap.denied` with no detail. (Optional verbose
mode for development.) The full reason is written to the audit log so
the human admin can investigate.

The `actor` field that already exists on every adapter call now
carries the agent identity, not a generic string. `WORKFLOW_OBS` rows
become first-class audit records.

---

## How learned permissions become agent capabilities

The bridge from `OBSERVABILITY.md` to here:

```
WORKFLOW_OBS observations
        │
        ▼
Tier 1 statistical mining
        │
        ▼
LEARNED_PATTERNS (candidate / trial)
        │
        ▼  human ratification (Tier 4)
        │
        ▼
ENFORCED PATTERN
        │
        ▼  protocol layer reads enforced patterns at handshake
        │
        ▼
GRANTED CAPABILITY
        │
        ▼
agent's session tool catalog reflects it
```

Concretely: when the human ratifies the `vendor_fast_track` pattern
for V1001 ≤ $4,260, the protocol layer can now grant
`coat:invoice:post@vendor=V1001,max_amount=4260` to any agent whose
manifest declared a need for it. No code change. The next time that
agent handshakes, it picks up the new capability and can act on it.

Demoting a pattern (oscillation, contradiction) revokes the capability
on the next handshake. Active sessions that were already issued the
revoked capability finish — or, for high-stakes scopes
(`coat:invoice:post`, `coat:invoice:approve`, `coat:agents:revoke`),
Coat may push a `session.revoke` event over the MCP transport and the
session ends mid-flight.

---

## Audit and revocation

Three audit primitives:

- **Per-call:** every `WORKFLOW_OBS` row carries `audit_id` and
  `agent_id` and the granted scope that authorized the call.
- **Per-session:** a `SESSIONS` table records issuance, granted
  scopes, expiry, and revoke events.
- **Per-capability:** a `CAPABILITY_GRANTS` table records which
  agent_id was granted which scope, when, by which ratification or
  policy.

Revocation paths:

- Admin manual revoke: `coat agent revoke --id ... --reason ...`.
  Effective immediately for new handshakes; pushes `session.revoke`
  for any active high-stakes session.
- Pattern demotion: any *enforced* pattern that is demoted
  automatically revokes its derived capability.
- Time-decay: capabilities with `rotate_keys_after_days` reach expiry
  and force re-handshake.
- Anomaly trigger: an agent whose call pattern in the last K minutes
  matches a known abuse signature is auto-quarantined.

---

## Protocol over MCP transports

MCP today supports stdio (local) and HTTP/SSE (network) transports.
The Coat Agent Protocol layers on both, with different defaults.

**Local stdio (today's default).** The MCP server is launched as a
subprocess of Claude Code or another local client. Identity is bound
to the OS user that launched the process. The handshake is still
performed (so the same code path applies) but the signature step uses
a key materialized from the local agent registration. This is the
trust posture for a developer's laptop.

**Network HTTP/SSE (production).** Full protocol applies. mTLS at the
transport layer authenticates the client; the in-protocol signing
authenticates the agent identity inside the client. (One Claude Code
instance can host multiple agent identities; mTLS alone can't
distinguish them.)

**Network with delegation.** A user's Claude Code instance acts as a
delegate for the user's identity. The handshake then includes a
*delegation chain* (`user U authorized agent A to act on their
behalf, signed at T, expiring at T+Δ`). The granted scopes are the
intersection of A's manifest and U's own scopes. Standard OAuth
On-Behalf-Of flow, lifted to the agent layer.

---

## How this maps to the current repo

| Concern | Where it goes |
|---------|---------------|
| Identity registration | `mcp_server/auth/registry.py` (new) — keypair store + manifest table |
| Handshake | `mcp_server/auth/handshake.py` (new) — `agent.hello` envelope, signature verification, scope evaluation |
| Scope grammar + parser | `mcp_server/auth/scopes.py` (new) — produces a `GrantedScope` value object |
| Per-call enforcement | extend `mcp_server/server.py` `call_tool` to take a session token, check `GrantedScope` before dispatch |
| Restricted tool catalog | extend `mcp_server/server.py` `list_tools` to filter by session granted scopes |
| Audit primitives | new tables `SESSIONS`, `CAPABILITY_GRANTS` (sibling to `WORKFLOW_OBS`); extend `_log_obs` to carry `audit_id`, `agent_id`, `granted_scope` |
| Bridge from learned patterns | `mcp_server/auth/grants.py` (new) — reads enforced LEARNED_PATTERNS, projects them onto scope tuples |
| Admin tools | new MCP tools `register_agent`, `revoke_agent`, `list_agent_grants`, `ratify_pattern` (the last one already exists in OBSERVABILITY's roadmap) |
| Local-stdio shim | `mcp_server/auth/local.py` (new) — derives identity from OS user, bypasses signature where appropriate |

The architectural invariant: **`actor`, `audit_id`, and `granted_scope`
are first-class arguments to every adapter function, not optional
metadata.** That is the difference between a protocol and a checklist.

The MVP today does *none* of this — every adapter call uses a default
`actor="agent"` with no scope check. Adding the protocol is additive:
the existing tool surface and discovery pipeline don't change.

---

## Open research

**Scope grammar's evolution.** The grammar above covers the obvious
cases. It does not cover well: temporal scopes ("only weekdays"),
correlated scopes ("post invoice only if a GR exists in the last 30
days"), and scopes that depend on values inside the result (post-hoc
limits). Designing this properly without re-inventing XACML is real
work.

**Capability inference at handshake.** When an agent declares
`requested_scopes`, Coat can either grant exactly those (strict) or
infer the minimal scope set that lets it do its job and grant only
that (least-privilege). Doing the latter requires knowing what the
agent will actually do. Static analysis of the agent's prompt + tools
is a research direction; a safer near-term approach is "strict by
default, log everything denied, surface frequent denials to admin for
manual scope expansion."

**Delegation under uncertainty.** When User U asks Agent A to do
something and A wants to do something not in U's scope, today the
right answer is "fail." But there are cases where A could reasonably
ask U for elevation mid-flight ("you didn't grant me invoice:post,
this task needs it, may I?"). The UX of mid-flight elevation through
MCP is unsolved.

**Cross-tenant agents.** If a single Claude Code instance is used by
a consultant working with three customers, the same agent identity
needs different capability sets in each tenant. The handshake has to
include tenant context and the registry has to be tenant-scoped from
day one. This is enterprise-table-stakes, but it bends the simple
single-tenant model above.

**Capability provenance.** When an audit asks "why was this agent
allowed to post this invoice?", the answer should chain back through:
agent_id → granted_scope → originating capability grant → ratified
pattern → underlying observations. We have all the rows; the readable
view is the work.

---

## What ships when

- **Now (MVP):** the doc you're reading. The adapter already takes an
  `actor` parameter; rewiring the rest of the protocol on top is
  staged code, not a fork of the current architecture.
- **Next sprint:** identity registration, handshake (signature +
  granted scope set), session-bound `audit_id`, restricted tool
  catalog. Skip resource constraints initially — a verb-level scope
  check on every call is already a major leap.
- **Sprint after:** resource-scoped constraints, the bridge from
  enforced learned patterns to capabilities, revocation propagation.
- **Post-beta:** delegation, cross-tenant, anomaly-triggered
  quarantine, mid-flight elevation.

The principle that makes this tractable: **every step of the protocol
is additive on top of an MCP server that works today.** No flag day,
no rewrite. Add identity. Add scopes. Add resource constraints. Each
gates the next, and each is independently shippable.
