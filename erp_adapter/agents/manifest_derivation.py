"""Manifest derivation — turn a plain-English description into an agent manifest.

This is the "as easy as wearing a coat" UX layer. The admin types one
paragraph describing what the agent does. Coat reads it, infers the
minimal scope set + bundle set + mode, surfaces a proposal, and the
human ratifies with one keystroke.

Three stages:

    Stage 1 — Task extraction
        LLM call (when ANTHROPIC_API_KEY is set) returns structured
        JSON: tasks[], data_classes_read[], data_classes_written[],
        excluded_actions[]. If LLM is unreachable, fall back to a
        keyword scanner that errs on denying.

    Stage 2 — Scope projection
        Deterministic mapper from the structured task description to
        scope tuples and bundle names. No LLM in this stage; auditable.

    Stage 3 — Proposal assembly
        Compose the granted/denied/bundle sets, set trial-mode defaults,
        return the proposal dict. The CLI renders it; the human ratifies.

This file has no UI and no DB I/O — it's pure mapping logic.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Catalog: what scopes and bundles exist
# ---------------------------------------------------------------------------

# Human-readable hints attached to each scope so denial messages make sense.
SCOPE_CATALOG: dict[str, str] = {
    "coat:concepts:read": "discover the shape of the ERP",
    "coat:inventory:read": "read stock primitives (get_stock, suggest_source_warehouse)",
    "coat:inventory:write": "post stock transfers",
    "coat:invoice:read": "read parked/posted AP invoices",
    "coat:invoice:post": "park or post an AP invoice",
    "coat:invoice:approve": "approve or reject a parked invoice",
    "coat:vendor:read": "read vendor master data",
    "coat:vendor:write": "create/update vendor records",
    "coat:procurement:read": "read purchase orders + GR history",
    "coat:procurement:write": "create or amend purchase orders",
    "coat:patterns:read": "see what Coat has learned (routing/approval patterns)",
    "coat:patterns:ratify": "promote a learned pattern to enforced",
    "coat:audit:read": "query the audit log",
    "coat:context:inventory:read": "receive Coat's assembled InventoryContext bundle",
    "coat:context:vendor:read": "receive Coat's assembled VendorContext bundle",
    "coat:context:invoice:read": "receive Coat's assembled InvoiceContext bundle",
}

# Bundles available in this build. Add to this when a new bundle ships.
BUNDLE_CATALOG: dict[str, str] = {
    "inventory_context": "ERP stock + recent movements + learned routing + external signals",
    # Future:
    # "vendor_context": "vendor master + history + sanctions + DUNS + risk",
    # "invoice_context": "PO/GR/invoice match + approval policy + vendor risk",
}


# ---------------------------------------------------------------------------
# Stage 1 — Task extraction
# ---------------------------------------------------------------------------

@dataclass
class ExtractedDescription:
    """Structured shape returned by stage 1."""

    summary: str = ""
    tasks: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)         # e.g. ["inventory", "vendor"]
    needs_read: list[str] = field(default_factory=list)      # high-level data classes
    needs_write: list[str] = field(default_factory=list)
    excluded_actions: list[str] = field(default_factory=list)
    external_signals_mentioned: list[str] = field(default_factory=list)


_LLM_PROMPT = """You analyze a plain-English description of an external agent
that plugs into Coat (an AI-native ERP layer) and return a STRICT JSON object
describing what the agent does. Be conservative: if a capability is not in
the description, omit it. Pay close attention to negations ("doesn't",
"won't", "never", "is not allowed to") — those go into excluded_actions.

Return JSON exactly matching:
{
  "summary": "<one-line summary>",
  "tasks": ["...one short verb-phrase per task..."],
  "domains": ["inventory" | "vendor" | "invoice" | "procurement" | "audit"],
  "needs_read": ["stock", "movement", "patterns", "vendors", "invoices", "purchase_orders", "audit"],
  "needs_write": ["stock", "invoices", "vendors", "purchase_orders"],
  "excluded_actions": ["post invoices", "create vendors", ...],
  "external_signals_mentioned": ["weather", "shipping", "sanctions", "news", "market"]
}

Output ONLY the JSON, no prose."""


def _extract_via_llm(description: str) -> ExtractedDescription | None:
    """Stage 1 via Claude. Returns None if the LLM is unreachable."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=os.environ.get("CLAUDE_MODEL", "claude-opus-4-7"),
            max_tokens=1024,
            system=_LLM_PROMPT,
            messages=[{"role": "user", "content": description}],
        )
        text = "".join(b.text for b in msg.content if hasattr(b, "text"))
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(text)
        return ExtractedDescription(
            summary=data.get("summary", ""),
            tasks=data.get("tasks", []) or [],
            domains=data.get("domains", []) or [],
            needs_read=data.get("needs_read", []) or [],
            needs_write=data.get("needs_write", []) or [],
            excluded_actions=data.get("excluded_actions", []) or [],
            external_signals_mentioned=data.get("external_signals_mentioned", []) or [],
        )
    except Exception:  # noqa: BLE001 — fall through to deterministic scanner
        return None


# Deterministic fallback — keyword scanner that errs on denying.
_NEGATION_PATTERNS = [
    r"\bdoesn['’]?t\b",
    r"\bdoes\s+not\b",
    r"\bwon['’]?t\b",
    r"\bwill\s+not\b",
    r"\bnever\b",
    r"\bis\s+not\s+allowed\b",
    r"\bnot\s+permitted\b",
    r"\bread[- ]only\b",
]
_NEGATION_RE = re.compile("|".join(_NEGATION_PATTERNS), re.IGNORECASE)

_DOMAIN_KEYWORDS = {
    "inventory": [r"\binventor", r"\bstock\b", r"\bstockout", r"\breorder", r"\bware ?house",
                  r"\bSKU", r"\bbin\b"],
    "vendor": [r"\bvendor", r"\bsupplier", r"\bonboard"],
    "invoice": [r"\binvoice", r"\bAP\b", r"\bbill\b"],
    "procurement": [r"\bpurchase order", r"\bPO\b", r"\bprocurement"],
    "audit": [r"\baudit", r"\baudit chain", r"\bcomplianc"],
}

_READ_KEYWORDS = {
    "stock": [r"\bstock\b", r"\binventory\b", r"\bSKU\b", r"\bavailable\b"],
    "movement": [r"\bmovement", r"\bvelocity", r"\btransfer", r"\boutbound"],
    "patterns": [r"\bpattern", r"\blearned"],
    "vendors": [r"\bvendor", r"\bsupplier"],
    "invoices": [r"\binvoice"],
    "purchase_orders": [r"\bpurchase order", r"\bPO\b"],
    "audit": [r"\baudit"],
}

_WRITE_KEYWORDS = {
    "stock": [r"\bmove stock\b", r"\btransfer stock", r"\bship out\b"],
    "invoices": [r"\bpost invoice", r"\bbook invoice", r"\bsubmit invoice"],
    "vendors": [r"\bcreate vendor", r"\bonboard vendor", r"\bregister vendor"],
    "purchase_orders": [r"\bcreate PO\b", r"\bplace order", r"\bsubmit purchase"],
}

_EXCLUDED_PHRASES = [
    "post anything", "post invoices", "post any invoice", "create vendor",
    "create vendors", "place orders", "place a po", "approve invoices",
]

_EXTERNAL_SIGNALS = {
    "weather": [r"\bweather", r"\bclimate", r"\bheat dome", r"\bstorm", r"\bsnow"],
    "shipping": [r"\bshipping", r"\bport\b", r"\blogistics", r"\bcustoms"],
    "sanctions": [r"\bsanction", r"\bOFAC\b", r"\bAML\b", r"\bKYC\b"],
    "news": [r"\bnews", r"\bRSS\b", r"\barticle"],
    "market": [r"\bmarket", r"\bcommodity", r"\bbenchmark"],
}


def _hits(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def _extract_via_keywords(description: str) -> ExtractedDescription:
    """Stage 1 deterministic fallback. Errs on denying — anything ambiguous
    is left out, with a flag in excluded_actions if a negation is detected."""
    desc = description or ""

    domains = [d for d, pats in _DOMAIN_KEYWORDS.items() if _hits(pats, desc)]
    needs_read = [c for c, pats in _READ_KEYWORDS.items() if _hits(pats, desc)]
    needs_write = [c for c, pats in _WRITE_KEYWORDS.items() if _hits(pats, desc)]
    external = [s for s, pats in _EXTERNAL_SIGNALS.items() if _hits(pats, desc)]

    excluded: list[str] = []
    if _NEGATION_RE.search(desc):
        # If negation appears, presume any write capability is denied unless
        # explicitly named.
        for cls in ["stock", "invoices", "vendors", "purchase_orders"]:
            if cls in needs_write:
                continue
            excluded.append(f"write to {cls}")

    # Catch the specific phrases too, in case the description spelled them out
    for phrase in _EXCLUDED_PHRASES:
        if re.search(re.escape(phrase), desc, re.IGNORECASE):
            excluded.append(phrase)
    excluded = sorted(set(excluded))

    # Build a one-line summary if possible — just the first sentence
    summary = (desc.split(".")[0] or "").strip()[:160]

    return ExtractedDescription(
        summary=summary,
        tasks=[],
        domains=domains,
        needs_read=sorted(set(needs_read)),
        needs_write=sorted(set(needs_write)),
        excluded_actions=excluded,
        external_signals_mentioned=sorted(set(external)),
    )


def extract(description: str) -> tuple[ExtractedDescription, str]:
    """Run stage 1. Returns (extracted, source) where source is 'llm' or 'fallback'."""
    via_llm = _extract_via_llm(description)
    if via_llm is not None:
        return via_llm, "llm"
    return _extract_via_keywords(description), "fallback"


# ---------------------------------------------------------------------------
# Stage 2 — Scope projection
# ---------------------------------------------------------------------------

# Domain → (read_scopes, optional write_scopes by data_class, default bundles)
_DOMAIN_PROJECTIONS: dict[str, dict[str, Any]] = {
    "inventory": {
        "read_scopes": ["coat:concepts:read", "coat:inventory:read", "coat:patterns:read"],
        "context_bundles": ["inventory_context"],
        "write_scopes_by_class": {
            "stock": ["coat:inventory:write"],
        },
    },
    "vendor": {
        "read_scopes": ["coat:concepts:read", "coat:vendor:read", "coat:patterns:read"],
        "context_bundles": [],  # vendor_context not built yet
        "write_scopes_by_class": {
            "vendors": ["coat:vendor:write"],
        },
    },
    "invoice": {
        "read_scopes": ["coat:concepts:read", "coat:invoice:read"],
        "context_bundles": [],
        "write_scopes_by_class": {
            "invoices": ["coat:invoice:post"],
        },
    },
    "procurement": {
        "read_scopes": ["coat:concepts:read", "coat:procurement:read"],
        "context_bundles": [],
        "write_scopes_by_class": {
            "purchase_orders": ["coat:procurement:write"],
        },
    },
    "audit": {
        "read_scopes": ["coat:audit:read"],
        "context_bundles": [],
        "write_scopes_by_class": {},
    },
}


@dataclass
class Manifest:
    granted_scopes: list[str]
    denied_scopes: list[dict[str, str]]   # [{"scope": ..., "reason": ...}]
    bundles: list[str]
    mode: str = "trial"
    trial_max_calls: int = 50
    trial_max_days: int = 7

    def to_dict(self) -> dict[str, Any]:
        return {
            "granted_scopes": self.granted_scopes,
            "denied_scopes": self.denied_scopes,
            "bundles": self.bundles,
            "mode": self.mode,
            "trial_max_calls": self.trial_max_calls,
            "trial_max_days": self.trial_max_days,
        }


def project(extracted: ExtractedDescription) -> Manifest:
    """Stage 2: deterministic mapper from extracted shape to manifest."""
    granted: set[str] = set()
    denied: list[dict[str, str]] = []
    bundles: set[str] = set()
    write_classes_requested: set[str] = set(extracted.needs_write)
    excluded_classes: set[str] = set()

    # An excluded action like "post invoices" implies no invoice:post.
    EXCLUSION_TO_WRITE = {
        "post anything": ["stock", "invoices", "vendors", "purchase_orders"],
        "post invoices": ["invoices"],
        "post any invoice": ["invoices"],
        "create vendor": ["vendors"],
        "create vendors": ["vendors"],
        "place orders": ["purchase_orders"],
        "place a po": ["purchase_orders"],
        "approve invoices": [],   # not a class, but we'll deny the scope explicitly
    }
    for ex in extracted.excluded_actions:
        ex_lower = ex.lower()
        for phrase, classes in EXCLUSION_TO_WRITE.items():
            if phrase in ex_lower:
                excluded_classes.update(classes)
        if "approve invoices" in ex_lower:
            denied.append({
                "scope": "coat:invoice:approve",
                "reason": f'description says "{ex}"',
            })

    # Walk each domain the description hits
    for domain in extracted.domains:
        proj = _DOMAIN_PROJECTIONS.get(domain)
        if not proj:
            continue
        granted.update(proj.get("read_scopes", []))
        bundles.update(proj.get("context_bundles", []))
        # If a bundle exists, add its read scope explicitly
        for b in proj.get("context_bundles", []):
            granted.add(f"coat:context:{domain}:read")

        for cls, scopes in (proj.get("write_scopes_by_class") or {}).items():
            if cls in write_classes_requested and cls not in excluded_classes:
                granted.update(scopes)
            elif cls in excluded_classes or cls in write_classes_requested:
                # explicitly denied — surface in proposal
                for s in scopes:
                    denied.append({
                        "scope": s,
                        "reason": (
                            f'description excludes write to {cls}'
                            if cls in excluded_classes
                            else f'write to {cls} not granted by default in trial mode'
                        ),
                    })

    # Always-denied common write paths if not granted, with helpful reasons
    common_write = {
        "coat:inventory:write": "stock writes",
        "coat:invoice:post": "invoice posting",
        "coat:invoice:approve": "invoice approval",
        "coat:vendor:write": "vendor master changes",
        "coat:procurement:write": "purchase order creation",
        "coat:patterns:ratify": "ratifying learned patterns",
    }
    granted_set = set(granted)
    already_denied_scopes = {d["scope"] for d in denied}
    for scope, label in common_write.items():
        if scope in granted_set or scope in already_denied_scopes:
            continue
        denied.append({
            "scope": scope,
            "reason": f"{label} not implied by description",
        })

    return Manifest(
        granted_scopes=sorted(granted),
        denied_scopes=denied,
        bundles=sorted(bundles),
    )


# ---------------------------------------------------------------------------
# Top-level — extract + project + assemble proposal
# ---------------------------------------------------------------------------

def derive_manifest(description: str) -> dict[str, Any]:
    """Return a proposal dict with the extracted shape, manifest, and source."""
    extracted, source = extract(description)
    manifest = project(extracted)
    return {
        "description": description.strip(),
        "summary": extracted.summary,
        "domains": extracted.domains,
        "needs_read": extracted.needs_read,
        "needs_write": extracted.needs_write,
        "excluded_actions": extracted.excluded_actions,
        "external_signals_mentioned": extracted.external_signals_mentioned,
        "manifest": manifest.to_dict(),
        "source": source,  # 'llm' or 'fallback'
    }
