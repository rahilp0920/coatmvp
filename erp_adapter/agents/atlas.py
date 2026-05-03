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

from agents.provider import detect_provider, make_provider  # noqa: E402
from mcp_server.bundles import inventory as inventory_bundle  # noqa: E402

console = Console()


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
   `out_avg_per_day * window_days * (weather_demand_modifier or 1.0)`.
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


def _dispatch(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "get_inventory_context":
        return inventory_bundle.get_inventory_context(**args)
    return {"error": f"unknown tool {tool_name!r}"}


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
        proj = round(out_per_day * window_days * wmod, 0)

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

def run(question: str, window_days: int = 7, top_n: int = 10, scripted: bool = False) -> None:
    """Run Atlas. Prints the recommendation."""
    provider_choice = None if scripted else detect_provider()

    if provider_choice is None or scripted:
        # Offline / no provider — call the bundle directly and reason
        # deterministically over it.
        bundle = inventory_bundle.get_inventory_context(
            window_days=window_days, top_n=top_n
        )
        console.print(
            Panel.fit(
                f"[bold]atlas[/bold] (scripted — no LLM provider available)\n"
                f"[dim]tool call → get_inventory_context(window_days={window_days}, top_n={top_n})[/dim]",
                border_style="cyan",
            )
        )
        _scripted_reasoning(bundle, window_days)
        return

    provider = make_provider(provider_choice)
    console.print(
        Panel.fit(
            f"[bold]atlas[/bold] (provider: {provider_choice.name} • model: {provider_choice.model})\n"
            f"[dim]Same MCP surface. Different brain. Same answer shape.[/dim]",
            border_style="cyan",
        )
    )

    # The agent literally only knows about one tool — that's the contract.
    final = provider.tool_use_loop(
        system_prompt=SYSTEM_PROMPT,
        user_message=question,
        tools=ATLAS_TOOLS,
        dispatch=_dispatch,
    )
    console.print(final)


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
    args = parser.parse_args()
    run(args.question, window_days=args.window, top_n=args.top_n, scripted=args.scripted)


if __name__ == "__main__":
    main()
