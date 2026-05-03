"""Atlas — specialized inventory planning agent.

Atlas is a configured agent that plugs into Coat over MCP. It has:

  • A specialized system prompt with inventory-planning expertise
    (lead-time-aware reorder logic, demand-modifier reasoning,
    learned-routing-aware allocation).
  • Access to ONE tool — `get_inventory_context` — which is Coat's
    pre-assembled InventoryContext bundle. Atlas does not call other
    tools to plumb context together. Atlas reasons.
  • An output shape designed for procurement decisions: per-SKU
    forecast with uncertainty, stockout risk band, reorder qty
    recommendation, and per-item reasoning notes.

The model provider is configurable (see `agents.provider`) — the demo
uses OpenAI o3 or Gemini 2.5 Pro to make the 'Coat is provider-
agnostic' point land. Falls back to Anthropic if no other key is set.
Falls back to deterministic scripted reasoning if no provider is
reachable, so the demo runs offline.

Atlas is the proof point for the Coat thesis: the specialization is
the configuration. Same MCP rails. Different brains. Same output.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

# Make the parent package importable when run as `python agents/atlas.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402

from agents.provider import detect_provider, make_provider  # noqa: E402
from mcp_server.bundles import inventory as inventory_bundle  # noqa: E402
from mcp_server import adapter as primitives_adapter  # noqa: E402
from mcp_server.dispatch import dispatch as scoped_dispatch  # noqa: E402

console = Console()

DEFAULT_AGENT_ID = "atlas@coat.io/v1"


SYSTEM_PROMPT = """You are Atlas, a specialized inventory planning agent.

You receive an InventoryContext bundle from Coat. That bundle is your
ONLY source of context. You do NOT call any other tools to assemble
data. You reason over the bundle Coat assembled and produce a
procurement-shaped recommendation.

For each item in the bundle:

1. Pick a primary warehouse — usually the highest available_after_reservations.
   If the item is fragile and the bundle has a learned_routing pattern,
   surface it explicitly.
2. Estimate demand for the window using
   `out_avg_per_day * window_days * (weather_demand_modifier or 1.0)
   * risk_amp`, where `risk_amp` reflects supply_chain_risk_band:
       high   → 1.40   (panic-buy / safety stock bump)
       medium → 1.15
       low / none → 1.00
   Express uncertainty as ±15% if movement_count < 6, else ±10%.
3. Compute stockout_risk:
   - HIGH if available < projected demand
   - MEDIUM if available < 1.2 × projected demand
   - LOW otherwise
4. Recommend reorder_qty = max(0, round(1.3 × projected_demand − available, 0))
   or 0 when stockout_risk is LOW.
5. Confidence: 0.6 if movement_count <= 3, 0.75 if <= 8, else 0.85.
   Subtract 0.10 if any external_signals.supply_chain_risk_band is HIGH.

Output a markdown table with columns:
SKU | warehouse | available | projected_demand | stockout_risk | reorder_qty | confidence

Then a 'Reasoning' section listing each at-risk item (MEDIUM or HIGH)
with a one-line explanation citing the specific external signal,
movement trend, or learned pattern that drove the call. Cite
`context_origin` for the bundle.

Be terse. No filler. No questions back to the user."""


# ---------------------------------------------------------------------------
# Tools — Atlas only sees one tool. That's the whole point.
# ---------------------------------------------------------------------------

ATLAS_TOOLS = [
    {
        "name": "get_inventory_context",
        "description": (
            "Return Coat's pre-assembled InventoryContext bundle. Single source of "
            "context — do not call other tools to plumb data together."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "window_days": {"type": "integer", "default": 7},
                "top_n": {"type": "integer", "default": 10},
                "lookback_days": {"type": "integer", "default": 60},
            },
        },
    },
]

# Tool implementations Atlas could invoke. Scope checking decides whether
# each call actually runs — see `_make_dispatch_for_agent`.
_TOOL_IMPLS: dict[str, Any] = {
    "get_inventory_context": inventory_bundle.get_inventory_context,
    "move_stock":             primitives_adapter.move_stock,
    "post_invoice":           primitives_adapter.post_invoice,
}


def _make_dispatch_for_agent(agent_id: str):
    """Build the dispatch closure used by the LLM tool-use loop.

    Every tool call routes through scope-aware `scoped_dispatch`. Calls
    outside the agent's granted scopes return a structured cap.denied
    payload — the LLM sees it in the tool result and is expected to
    surface the denial to the user in plain English."""

    def _dispatch(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        impl = _TOOL_IMPLS.get(tool_name)
        if impl is None:
            return {"error": f"unknown tool {tool_name!r}"}
        return scoped_dispatch(agent_id, tool_name, args, impl)

    return _dispatch


# ---------------------------------------------------------------------------
# Scripted fallback — deterministic reasoning over the bundle
# ---------------------------------------------------------------------------

def _scripted_reasoning(bundle: dict[str, Any], window_days: int) -> str:
    items = bundle.get("items", [])
    rows: list[dict[str, Any]] = []
    for it in items:
        avail = it.get("available_after_reservations") or {}
        if not avail:
            continue
        primary_wh = max(avail, key=lambda w: avail[w])
        primary_avail = avail[primary_wh]

        movement = it.get("movement_last_n_days") or {}
        out_per_day = float(movement.get("out_avg_per_day") or 0.0)
        ext = it.get("external_signals") or {}
        wmod = float(ext.get("weather_demand_modifier") or 1.0)
        risk_band = (ext.get("supply_chain_risk_band") or "").lower()
        risk_amp = {"high": 1.40, "medium": 1.15, "low": 1.0}.get(risk_band, 1.0)
        proj = round(out_per_day * window_days * wmod * risk_amp, 0)

        unc_pct = 15 if movement.get("movement_count", 0) < 6 else 10
        unc = round(proj * unc_pct / 100, 0)

        if proj == 0:
            risk = "LOW"
        elif primary_avail < proj:
            risk = "HIGH"
        elif primary_avail < 1.2 * proj:
            risk = "MEDIUM"
        else:
            risk = "LOW"

        reorder = max(0, math.ceil(1.3 * proj - primary_avail)) if risk in ("HIGH", "MEDIUM") else 0

        confidence = 0.6 if movement.get("movement_count", 0) <= 3 else (
            0.75 if movement.get("movement_count", 0) <= 8 else 0.85
        )
        if (ext.get("supply_chain_risk_band") or "").lower() == "high":
            confidence = max(0.5, confidence - 0.10)

        rows.append({
            "sku": it.get("sku"),
            "name": it.get("name"),
            "wh": primary_wh,
            "avail": primary_avail,
            "proj": proj,
            "unc": unc,
            "risk": risk,
            "reorder": reorder,
            "confidence": confidence,
            "fragile": it.get("fragile"),
            "routing": it.get("learned_routing"),
            "ext": ext,
            "trend": movement.get("trend"),
            "movement_count": movement.get("movement_count", 0),
        })

    return _format_output(rows, bundle, window_days)


def _format_output(rows: list[dict[str, Any]], bundle: dict[str, Any], window_days: int) -> str:
    """Render the inventory outlook table + reasoning + context_origin."""
    table = Table(
        title=f"INVENTORY OUTLOOK — next {window_days}d  (atlas)",
        title_justify="left",
        show_lines=False,
    )
    table.add_column("SKU", style="cyan", no_wrap=True)
    table.add_column("WH", style="dim")
    table.add_column("AVAIL", justify="right")
    table.add_column("PROJ DEMAND", justify="right")
    table.add_column("RISK")
    table.add_column("REORDER", justify="right")
    table.add_column("CONF", justify="right")

    for r in rows:
        risk_color = {"HIGH": "bold red", "MEDIUM": "yellow", "LOW": "green"}[r["risk"]]
        table.add_row(
            r["sku"] or "?",
            r["wh"],
            f"{r['avail']:.0f}",
            f"~{r['proj']:.0f}±{r['unc']:.0f}",
            f"[{risk_color}]{r['risk']}[/{risk_color}]",
            f"{r['reorder']:.0f}" if r["reorder"] else "—",
            f"{r['confidence']:.2f}",
        )

    console.print(table)

    # Reasoning notes for at-risk items
    at_risk = [r for r in rows if r["risk"] != "LOW"]
    if at_risk:
        lines = ["\n[bold]Reasoning[/bold]"]
        for r in at_risk:
            parts: list[str] = []
            if r["routing"]:
                parts.append(r["routing"])
            ext = r["ext"]
            if ext.get("weather_summary"):
                parts.append(f"weather: {ext['weather_summary']}")
            if ext.get("news_summary"):
                parts.append(f"news: {ext['news_summary']}")
            wmod = ext.get("weather_demand_modifier")
            if wmod and wmod != 1.0:
                parts.append(f"demand modifier {wmod:.2f}")
            if r["trend"] == "rising":
                parts.append("movement trend rising")
            line = f"  • {r['sku']} ({r['wh']}): " + (
                "; ".join(parts) if parts else "no external context"
            )
            lines.append(line)
        console.print("\n".join(lines))

    origin = bundle.get("context_origin", []) or []
    if origin:
        console.print(
            Panel.fit(
                "\n".join(f"• {o}" for o in origin),
                title="context_origin (provenance)",
                border_style="dim",
            )
        )

    return "rendered"


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def run(
    question: str,
    window_days: int = 7,
    top_n: int = 10,
    scripted: bool = False,
    agent_id: str = DEFAULT_AGENT_ID,
) -> None:
    """Run Atlas. Prints the recommendation."""
    provider_choice = None if scripted else detect_provider()

    if provider_choice is None or scripted:
        # Offline / no provider — call the bundle directly through the
        # scope-aware dispatcher so the trial-budget counter and audit
        # rows still land. Reasoning is deterministic.
        bundle = scoped_dispatch(
            agent_id, "get_inventory_context", {
                "window_days": window_days, "top_n": top_n,
            }, inventory_bundle.get_inventory_context,
        )
        if isinstance(bundle, dict) and bundle.get("error") == "cap.denied":
            console.print(
                Panel.fit(
                    Text.assemble(
                        Text("cap.denied  ", style="bold red"),
                        Text("get_inventory_context  ", style="bold"),
                        Text(f"agent={agent_id}\n", style="dim"),
                        Text(f"missing: {bundle.get('missing_scope')}\n", style="default"),
                        Text(f"reason : {bundle.get('reason')}\n", style="default"),
                        Text(f"advise : {bundle.get('advise')}", style="dim"),
                    ),
                    border_style="red",
                )
            )
            return

        console.print(
            Panel.fit(
                f"[bold]atlas[/bold] (scripted — no LLM provider available)  "
                f"agent_id={agent_id}\n"
                f"[dim]tool call → get_inventory_context(window_days={window_days}, top_n={top_n})  "
                f"audit_id={bundle.get('_audit', {}).get('audit_id', '?')}[/dim]",
                border_style="cyan",
            )
        )
        _scripted_reasoning(bundle, window_days)
        return

    provider = make_provider(provider_choice)
    console.print(
        Panel.fit(
            f"[bold]atlas[/bold] (provider: {provider_choice.name} • model: {provider_choice.model})  "
            f"agent_id={agent_id}\n"
            f"[dim]Same MCP surface. Different brain. Same answer shape.[/dim]",
            border_style="cyan",
        )
    )

    # The agent literally only knows about one tool — that's the contract.
    dispatch_fn = _make_dispatch_for_agent(agent_id)
    final = provider.tool_use_loop(
        system_prompt=SYSTEM_PROMPT,
        user_message=question,
        tools=ATLAS_TOOLS,
        dispatch=dispatch_fn,
    )
    console.print(final)


def demo_denial(agent_id: str = DEFAULT_AGENT_ID) -> None:
    """Have Atlas try a write operation it isn't authorized for.

    Used in scene 5 of the demo runbook: Atlas attempts to rebalance
    stock via `move_stock`, the protocol denies it because the agent's
    manifest doesn't include `coat:inventory:write`, and the admin sees
    a clean cap.denied payload with the exact `coat agent grant` command
    that would unlock it.
    """
    console.print(
        Panel.fit(
            f"[bold]atlas[/bold] demo: out-of-scope write attempt  "
            f"agent_id={agent_id}\n"
            f"[dim]Atlas tries `move_stock` to rebalance stock — outside its "
            f"manifest, which only granted read scopes.[/dim]",
            border_style="cyan",
        )
    )

    result = scoped_dispatch(
        agent_id,
        "move_stock",
        {"matnr": "SKU-441", "qty": 30, "from_warehouse": "WH02",
         "to_warehouse": "WH01", "reason": "atlas demo: rebalance"},
        primitives_adapter.move_stock,
    )

    if isinstance(result, dict) and result.get("error") == "cap.denied":
        # Inline-ratify path either declined or unavailable — surface the
        # admin-side resolution path explicitly.
        console.print(
            Panel.fit(
                Text.assemble(
                    Text("Atlas was denied and the admin did not approve.\n", style="bold red"),
                    Text("\nadmin can still grant out-of-band:\n", style="default"),
                    Text(
                        f"  $ coat agent grant {agent_id} "
                        f"{result.get('missing_scope')} --reason 'atlas demo'",
                        style="bold yellow",
                    ),
                ),
                title="cap.denied",
                border_style="red",
            )
        )
    else:
        # Either the admin approved inline (mid-flight ratification) or the
        # capability was already granted on a prior run.
        scope = (result.get("_audit") or {}).get("scope") if isinstance(result, dict) else None
        console.print(
            Panel.fit(
                Text.assemble(
                    Text("✓ ", style="bold green"),
                    Text("move_stock posted ", style="default"),
                    Text(str(result.get("doc", "?")), style="bold"),
                    Text(f"  qty={result.get('moved')} {result.get('from')}→{result.get('to')}\n",
                         style="dim"),
                    Text(f"  scope used: {scope}\n", style="dim") if scope else Text(""),
                    Text(f"  every step is in the audit chain — `coat audit --entity SKU-441`",
                         style="dim"),
                ),
                title="atlas resolved its own permission ask",
                border_style="green",
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "question",
        nargs="?",
        default="What's our stockout risk for next week, and what should we reorder?",
    )
    parser.add_argument("--window", type=int, default=7, help="forecast window in days")
    parser.add_argument("--top-n", type=int, default=10, help="how many items to consider")
    parser.add_argument(
        "--scripted",
        action="store_true",
        help="force scripted (offline) mode — bypass any LLM provider",
    )
    parser.add_argument(
        "--agent-id",
        default=DEFAULT_AGENT_ID,
        help=f"agent identity to run as (default {DEFAULT_AGENT_ID})",
    )
    parser.add_argument(
        "--demo-denial",
        action="store_true",
        help=(
            "demo scene 5 cap.denied path: Atlas tries to call move_stock "
            "(out-of-scope) and surfaces the scope-expansion request"
        ),
    )
    args = parser.parse_args()
    if args.demo_denial:
        demo_denial(agent_id=args.agent_id)
        return
    run(
        args.question,
        window_days=args.window,
        top_n=args.top_n,
        scripted=args.scripted,
        agent_id=args.agent_id,
    )


if __name__ == "__main__":
    main()
