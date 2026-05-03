# Coat — security model

Coat is the layer between an autonomous agent and a company's ERP. The
threat model that matters is not someone breaking in from outside; it's the
agent inside the loop doing the wrong thing — too quickly, on too many
records, against the wrong account. The architecture below is built around
that assumption.

This document describes what's enforced today in the MVP and where the
production extensions slot in.

---

## Threat model

What we are protecting against:

- An agent invents a column name, generates a malformed query, and corrupts
  records in a table it had no business touching.
- An agent posts a financial transaction outside the human-approval bracket
  for that vendor, that amount, that company code.
- An agent over-pulls master data, sample rows, or PII and leaks it to the
  model context or to logs.
- An agent makes a series of small, individually-defensible moves that add
  up to a large, undefensible action.
- A misconfigured plugin invokes a tool with arguments that bypass the
  intended approval flow.

What we are explicitly not protecting against in this MVP, and which the
production deployment must address:

- A compromised credential at the ERP layer.
- An adversary with write access to the host file system.
- Side-channel exfiltration through the LLM provider.

---

## Defense layers

The MVP enforces six layers, in order of how often they fire.

### 1. Tool surface — the agent only sees business concepts

The agent never receives a SQL string, a table name, or a column name in
its tool inputs or outputs. It calls `get_stock("SKU-441")`, not
`SELECT … FROM WH_STOCK …`. The MCP server in `mcp_server/server.py`
exposes a closed list of nine tools, each with a JSON-Schema-validated
input. Anything outside that surface is unreachable.

The relevant guardrails live in `CLAUDE.md` at the repo root: *no SQL, no
direct schema introspection, always start with `list_concepts`.* These are
loaded as agent context the moment the model starts a session inside the
project.

### 2. Adapter — sole write path to the ERP

Every state-changing operation on the ERP goes through one of five
functions in `mcp_server/adapter.py`:

- `move_stock` — updates `BIN_DETAIL`, `WH_STOCK` rollups, writes an `MSEG` doc.
- `post_invoice` — writes `AP_HEAD`, `AP_LINES`, posts paired `GL_ENTRIES` only when status reaches `APPR`.
- `request_approval` — flips an invoice's `STATUS` and writes the matching GL pair.
- `submit_feedback` — only mutates `WORKFLOW_OBS` (the audit table).
- `find_item`, `get_stock`, `list_concepts`, `list_pending_invoices`, `suggest_source_warehouse` are read-only.

There is no general "execute SQL" tool. Adding one is a deliberate code
change, reviewable in diff.

### 3. Pre-conditions on every mutation

Each mutation runs structural checks before it commits:

- `post_invoice` rejects vendors with `SPERR=1` (blocked vendor) and
  returns `outcome="DENIED"`.
- `move_stock` rejects requests where source = destination, or where
  available stock at the source is below the requested quantity. It
  returns `outcome="DENIED"` and writes the failed attempt to the
  observation log so the learner can spot recurring near-misses.
- `request_approval` rejects any decision other than `APPROVE` / `REJECT`.

These are not advisory — they're conditions inside the adapter functions
themselves, so a plugin cannot bypass them by calling the underlying
tools directly.

### 4. Approval ceilings — Z_APPR_RULES + learned fast-tracks

`_resolve_approval_role` walks `Z_APPR_RULES` to determine whether a
posted invoice needs `AUTO`, `MANAGER`, or `CFO` sign-off. Above the bracket
threshold the invoice parks at status `PARK` and stays there until a real
user (named in `request_approval`) decides.

Learned vendor fast-tracks in `LEARNED_PATTERNS` can short-cut this only
under explicit constraints: the matching pattern must have been mined from
≥5 prior approvals, with ≥0.6 confidence, and the invoice amount must be
at or below the observed-max ceiling × 1.1. Anything outside those
bounds reverts to the rule-based path. The fast-track is also surfaced
back to the user as `approval.learned_note` so the audit chain is
human-readable, not implicit.

### 5. Audit trail — WORKFLOW_OBS

Every adapter call writes a row to `WORKFLOW_OBS` containing the actor,
the tool name, the full args, the result summary, the outcome
(`OK` / `DENIED` / `FEEDBACK`), and a timestamp. This is the audit log,
and it is also what the learner mines — so every approval pattern the
system codifies is grounded in a queryable history.

The observation log is intentionally write-once. The MVP does not expose
a "delete observation" tool. To remove an observation in production you
would write a tombstone row, not edit the original.

### 6. Claude Code permission allowlist

`.claude/settings.local.json` declares the exact bash commands the agent
is permitted to spawn during a Claude Code session. The default
allowlist contains only the pipeline runners and the MCP server
launcher. There is no wildcarded `Bash(*)` or `Bash(rm *)` entry. A
plugin that wants to introduce a new shell command must add it
explicitly, which is reviewable.

---

## Data isolation

### Inside the adapter

The adapter is the only thing that reads from `MAT_MASTER`, `LFA1`, and
the rest of the ERP tables. It returns dictionaries keyed by business
roles (`"item"`, `"warehouse"`, `"available"`), not raw column names.
The agent therefore cannot see whether `MATNR` or `MAT_NUM` or
`MaterialId` is the column on disk — only that the concept `item` has
an `id`.

### Sent to the LLM (discovery)

`discovery/introspect.py` produces a dossier that includes table names,
column names, foreign keys, and **sample rows** (default 5 per table).
That sample is sent to Claude in `discovery/semantic_map.py` to drive the
business-concept mapping.

For the MVP and any sandbox-data demo, this is fine. For production a
redaction step is required: hash any column flagged as PII before
sampling, or sample only against a synthetic shadow row generated from
the column type. The redaction layer sits on the boundary between
`introspect.py` and `semantic_map.py` and is the next line of work for a
real-world deployment.

### Logged

`WORKFLOW_OBS.RESULT_JSON` truncates large result bodies to the
summarized fields the learner needs (e.g. for `get_stock` we log
`{"total_available": ..., "wh_count": ...}`, not the per-bin
breakdown). The full body never lands in the audit table.

---

## What changes in production

The MVP runs everything as a single process against a SQLite file. A
production deployment adds:

- An ERP adapter per backend (S/4HANA Cloud, on-prem ECC via OData,
  Oracle Fusion, NetSuite, etc.) sitting behind the same business-concept
  surface. The agent and tools do not change.
- Service-account scoping at the ERP. Coat's adapter authenticates with a
  least-privileged communication user that has CRUD only on the entities
  it owns.
- Row-level security joined to the user identity passed in `_actor`, so
  a clerk and a CFO see different `list_pending_invoices` results from
  the same call.
- TLS + mTLS between the MCP transport and the adapter.
- Encryption-at-rest for `WORKFLOW_OBS` (it contains arguments and
  results, which can carry PII).
- A redaction layer in `discovery/` so column samples sent to Claude are
  hashed or synthesized for any tagged PII column.
- A deny-list of tools that cannot be exposed to specific plugins,
  enforced at the MCP server boundary.

---

## How to verify the model is doing what we think

Run `python run.py` and then `python run.py inspect`. The inspect output
prints `LEARNED_PATTERNS` (every codified rule, its support, its
confidence) and the most recent `WORKFLOW_OBS` rows (every call the agent
made, with arguments and outcome). That's the audit chain. If anything
the agent did is not in those tables, the system has misbehaved and the
incident is investigable.
