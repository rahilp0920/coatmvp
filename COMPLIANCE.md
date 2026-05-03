# Coat — compliance posture and roadmap

This doc answers two questions a buyer's CISO will ask in the first
meeting:

1. *"What certifications do you hold today, and what's the roadmap?"*
2. *"What's already in your architecture that helps me satisfy my own
   compliance obligations when I deploy you?"*

The honest answer to (1) is: nothing yet, we're in private beta. The
honest answer to (2) is: **Coat's audit chain, immutable observation
log, ratification trail, capability provenance, and least-privilege
agent protocol are all designed against the controls these
frameworks ask for, so we are *enabling* customer compliance from day
one and *attaining* our own as we mature.**

---

## Where we are today

- **Stage:** private beta. No third-party certifications. Customer
  data lives on the customer's side; Coat-cloud sees normalized
  events under mTLS.
- **Architectural posture:** designed for SOC 2 / ISO 27001 control
  alignment, GDPR data-handling principles, and SOX-aware controls
  for finance workloads. The architecture matters here — most
  greenfield SaaS scrambles to retrofit controls; we're building
  with them in mind.
- **What we will not do:** retroactively claim certifications we
  don't hold, tell a customer "we're SOC 2 compliant" when we mean
  "we're building toward it," or hide behind a parent company's
  certifications.

---

## Frameworks that matter, by customer profile

### SOC 2 (Type I, then Type II) — *table stakes for B2B SaaS*

The single most-asked-about framework in mid-market and enterprise
sales. SOC 2 audits the *controls you operate* against five Trust
Services Criteria: Security, Availability, Processing Integrity,
Confidentiality, Privacy.

For a Coat customer, our SOC 2 report is the document their
procurement team will request before signing. Without it, deals
above ~$50k ARR stall. Type I attests "the controls exist as of a
point in time"; Type II attests "the controls operated effectively
over a 6-12 month period." Type II is what real buyers want.

**Timeline:** 0–2 months readiness assessment with Vanta or Drata,
2–6 months remediation + Type I audit, 6–18 months operating period
+ Type II audit. Practically: pick this up the day after a series A
closes.

### ISO 27001 — *international parity for SOC 2*

European, APAC, and some financial-services buyers ask for ISO 27001
in addition to or instead of SOC 2. Same control families, different
auditor pool, similar timeline. Often pursued in parallel with SOC 2
Type II since the evidence overlaps ~80%.

### GDPR — *required for EU data-subject presence*

Not a certification — a regulation. Required if Coat or any
customer's data subjects are in the EU. The practical asks:

- Data Processing Agreement (DPA) signed with every customer.
- Standard Contractual Clauses (SCCs) for any cross-border transfer.
- Right-to-erasure plumbing — Coat must be able to delete a data
  subject's records on request *including from `WORKFLOW_OBS`*. (The
  observation log is immutable for audit; this is the tension. We
  resolve it by *tombstoning* with a redaction marker rather than
  hard-deleting; the row remains for audit chain, with subject
  identity hashed.)
- Sub-processor list and notification on changes.
- Breach notification within 72 hours.

**Timeline:** day-one items. DPA template, SCC clauses, redaction
tombstone path. ~1 week of legal + engineering work to get to a
postable v1.

### SOX — *for any AP, GL, or financial-close workflow*

Sarbanes-Oxley applies to public-company customers and their finance
processes. Coat does not directly hold SOX certification (it's not
that kind of framework). Instead, when a customer uses Coat in a
SOX-relevant flow (AP automation, journal posting, close
acceleration), they need to demonstrate *internal controls over
financial reporting* (ICFR) for the work Coat does on their behalf.

What they need from us:

- An auditable record that every Coat-driven transaction was
  authorized by a *human-ratified rule*, not an autonomous decision
  the AI made. The capability lifecycle (candidate → trial →
  enforced via human ratification) directly answers this.
- Segregation of duties: an agent that posts an invoice cannot also
  approve it. Enforced via scope grammar — `coat:invoice:post` and
  `coat:invoice:approve` are distinct capabilities, granted to
  different agent identities.
- Tamper-evident audit log of every action and the capability that
  authorized it. `WORKFLOW_OBS` + `audit_id` + `granted_scope` chain
  is exactly this.
- A documented change-management path for the rules themselves
  (which is what `RATIFICATIONS` table provides).

This is the part where Coat's architecture is *more* SOX-friendly
than most agentic-AI alternatives, not less. We can talk about this
explicitly with finance buyers.

### HIPAA — *only if healthcare customers*

Not relevant for v1. Pick up if/when a healthcare customer is in the
pipeline. Adds BAA paperwork, encryption-at-rest mandates, audit
controls beyond what SOC 2 already requires.

### SAP Endorsed Apps program — *credibility marker for direct SAP partnership*

Not a regulatory framework — SAP's quality program. ~6–12 months
process: code review by SAP, integration testing against S/4HANA
Cloud, marketplace listing. Earning this is the difference between
"another app talking to SAP APIs" and "SAP-vetted partner." Worth
pursuing once the platform stabilizes and we have ≥3 reference
customers on S/4. Targets back half of 2026.

### NIST CSF / FedRAMP — *not for v1*

Federal customers will ask. Far higher bar (Authorization to Operate,
continuous monitoring, FIPS-validated crypto). Out of scope until
there is a federal champion willing to fund the path.

---

## What we already build into Coat that enables compliance

This is the architecture-level posture we lean on in the CISO
conversation. Each one ties to specific controls in SOC 2 / ISO /
SOX.

### Immutable observation log

`WORKFLOW_OBS` is append-only by design. The MVP does not expose a
"delete observation" tool. Production deployment uses tombstones
(redaction markers) instead of hard deletes for GDPR
right-to-erasure, preserving the chain-of-custody.

*Maps to:* SOC 2 CC7.2 (system monitoring), SOX ICFR audit trail,
ISO 27001 A.12.4 (logging and monitoring).

### Ratification trail

Every enforced learned permission carries a `RATIFICATIONS` row:
which pattern, which reviewer, what decision, what note, what time.
A pattern that ever influenced a decision is traceable to a human
sign-off.

*Maps to:* SOX ICFR (preventive controls), SOC 2 CC6.1 (logical
access management), GDPR Article 22 (right to explanation for
automated decisions).

### Capability provenance

Every tool call carries `audit_id` + `agent_id` + `granted_scope`.
The `granted_scope` traces back to a `CAPABILITY_GRANTS` row, which
traces back to either a `RATIFICATIONS` row (if the capability came
from a learned pattern) or a manual admin grant (if directly
authorized). Every action is explicable.

*Maps to:* SOC 2 CC6.2 (provisioning), SOX 404 (assessment of
internal controls), ISO 27001 A.9.2 (user access management).

### Segregation of duties via scope grammar

`coat:invoice:post`, `coat:invoice:approve`, `coat:agents:revoke`
are distinct capabilities. The protocol prevents granting both
post-and-approve to the same agent unless an admin explicitly
overrides — and that override is itself ratified.

*Maps to:* SOX SoD requirements, SOC 2 CC6.3 (segregation of
duties), ISO 27001 A.6.1.2.

### Least-privilege agent identity

Every agent registers with a manifest declaring requested scopes;
the granted set is the intersection of (manifest, requested,
enforced patterns, admin overrides). An agent's MCP tool catalog is
filtered to its granted scopes — it doesn't even *see* tools it
isn't authorized to use.

*Maps to:* SOC 2 CC6.6 (logical access — principle of least
privilege), ISO 27001 A.9.4.

### Permission lifecycle that requires human approval at the gate

No mined pattern enforces a real-world action until a human
ratifies. The architecture *prevents* autonomous-AI-makes-policy
scenarios by construction.

*Maps to:* GDPR Article 22 (no fully-automated decisions for
significant matters), SOX preventive controls, EU AI Act high-risk
system requirements as they finalize.

### Encryption in transit + at rest (production)

mTLS on the bridge tunnel and the MCP transport. AES-256 at rest
for `WORKFLOW_OBS` and `LEARNED_PATTERNS`. Key rotation hooked into
`coat rotate-keys`.

*Maps to:* SOC 2 CC6.7, ISO 27001 A.10.

### Tenant isolation

Every record carries a `tenant_id`. Coat-cloud is multi-tenant;
no cross-tenant query path exists. Bridge agents are tenant-scoped
at deployment.

*Maps to:* SOC 2 CC6.1 + the segregation requirements every
multi-tenant SaaS audit asks about.

---

## Roadmap

A realistic, ungilded plan. All months relative to a series-A close
funding the compliance work.

| Month | Milestone |
|-------|-----------|
| **0–1** | Vanta or Drata onboarded. Initial controls inventory. DPA + SCC templates published. GDPR right-to-erasure tombstone path implemented. |
| **2–3** | Internal controls in place (logging, MFA on all admin paths, encryption configured, vendor-management process). Sub-processor list public. Pen test #1. |
| **4–6** | SOC 2 Type I audit. ISO 27001 Stage 1. Begin operating controls for Type II observation period. |
| **6–12** | SOC 2 Type II observation window. ISO 27001 Stage 2. Pen test #2. SOC 2 Type II report published. |
| **9–15** | SAP Endorsed Apps application + integration testing (assumes ≥3 S/4 reference customers). |
| **12–18** | SOC 2 Type II report renewal cadence. HIPAA BAA template if healthcare customer is in the pipeline. |
| **18+** | NIST CSF mapping if federal interest. EU AI Act conformity assessment as that regulation finalizes. |

---

## What we say in the first CISO meeting

A clean script:

> *"We're in private beta — no SOC 2, no ISO yet. The architecture
> was built with those controls in mind, not as a retrofit. Our
> audit chain is end-to-end traceable: every action ties to a
> capability, which ties to a ratified pattern, which ties to a
> named reviewer. We give you tombstone-based GDPR erasure that
> preserves the audit chain. Segregation of duties is enforced in
> the protocol's scope grammar, not as a manual review process. Our
> SOC 2 Type I is on the roadmap behind the next funding round; we
> can offer a contractual commitment to the timeline. Until then,
> here's our pen test report, here's our DPA, here's our
> sub-processor list. We're not asking you to trust us — we're
> asking you to read what we've already built."*

That's the line. It's honest, it doesn't oversell, and it gives the
CISO the concrete artifacts they need to advance the deal under
their own internal procurement track.

---

## What this implies for the build

A handful of features listed in `OBSERVABILITY.md`, `AGENT_PROTOCOL.md`,
and `DEPLOYMENT.md` exist *because* of compliance. Calling them out
explicitly:

- `RATIFICATIONS`, `SESSIONS`, `CAPABILITY_GRANTS` tables — required
  for SOC 2 + SOX evidence.
- Tombstone-redaction in `WORKFLOW_OBS` (GDPR Art. 17).
- `audit_query` MCP tool — required for auditor-facing exports.
- Tenant ID propagation through every adapter call — required for
  multi-tenant audit isolation.
- Scope grammar's enforcement of post/approve separation — required
  for SOX SoD.
- Restricted MCP tool catalog per session — required for least
  privilege.

None of these are compliance-theater. They are the right architecture
*and* they happen to satisfy the frameworks. That alignment is the
posture that lets us get to certification on a normal timeline
instead of a death-march retrofit.
