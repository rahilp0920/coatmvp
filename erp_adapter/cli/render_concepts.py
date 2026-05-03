"""Render the discovered concept catalog with confidence bars.

Reads `context/context.yaml`, prints a clean table of business concepts
with their backing tables, confidence scores (visualized as bars), and
the structural evidence behind each score.

This is the still frame of scene 2 in the demo runbook — the moment
that lands "Coat verifies, it doesn't search."

Usage:
    python -m cli.render_concepts                    # default: full catalog
    python -m cli.render_concepts --evidence         # show full evidence per row
    python -m cli.render_concepts --concept item     # just one concept, full detail
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
CONTEXT_PATH = ROOT / "context" / "context.yaml"

console = Console()


def _bar(confidence: float, width: int = 5) -> Text:
    """Five-block bar with a colored gradient based on confidence band."""
    filled = round(confidence * width)
    filled = max(0, min(width, filled))
    bar_str = "█" * filled + "░" * (width - filled)
    if confidence >= 0.85:
        color = "bright_green"
    elif confidence >= 0.70:
        color = "yellow"
    else:
        color = "red"
    return Text(bar_str, style=color)


def _summarize_evidence(evidence: list[str], width: int = 70) -> str:
    """Single-line evidence summary, truncated if needed."""
    if not evidence:
        return ""
    parts: list[str] = []
    for ev in evidence:
        # Drop "X rows in TABLE" — that's noise in the summary
        if " rows in " in ev:
            continue
        # Compress redundant column-name prefixes
        parts.append(ev)
    joined = "; ".join(parts)
    if len(joined) > width:
        joined = joined[: width - 1].rstrip(" ;,") + "…"
    return joined


def render_catalog(ctx: dict[str, Any], show_full_evidence: bool = False) -> None:
    """Render the full concept catalog as a rich table."""
    cmap = ctx.get("concept_map", {})
    if not cmap:
        console.print("[red]context.yaml has no concept_map. Run discovery first.[/red]")
        return

    # Sort by confidence desc so the strongest signals lead
    rows = sorted(
        cmap.items(),
        key=lambda kv: kv[1].get("confidence", 0.0),
        reverse=True,
    )

    title = Text("DISCOVERED CONCEPTS", style="bold")
    table = Table(
        title=title,
        title_justify="left",
        box=box.SIMPLE_HEAD,
        show_lines=False,
        pad_edge=False,
        padding=(0, 1),
    )
    table.add_column("CONCEPT", style="cyan", no_wrap=True)
    table.add_column("TABLE", style="dim", no_wrap=True)
    table.add_column("CONFIDENCE", no_wrap=True)
    table.add_column("EVIDENCE", overflow="fold")

    for name, binding in rows:
        confidence = float(binding.get("confidence", 0.0))
        evidence = binding.get("evidence", []) or []
        bar = _bar(confidence)
        conf_cell = Text.assemble(bar, Text(f"  {confidence:.2f}", style="bold"))
        evidence_cell = (
            "\n".join(f"• {e}" for e in evidence)
            if show_full_evidence
            else _summarize_evidence(evidence)
        )
        table.add_row(name, binding.get("table", "?"), conf_cell, evidence_cell)

    console.print(table)

    derived = ctx.get("derived_views", []) or []
    if derived:
        console.print()
        for d in derived:
            console.print(
                Panel.fit(
                    Text.assemble(
                        Text(d.get("name", "?"), style="bold cyan"),
                        Text("  "),
                        Text(d.get("definition", ""), style="default"),
                    ),
                    title="derived view",
                    border_style="dim",
                )
            )

    n = len(cmap)
    if n:
        avg = sum(float(b.get("confidence", 0.0)) for b in cmap.values()) / n
        console.print(
            f"\n[dim]{n} concepts • average confidence {avg:.2f} • "
            f"all scores are structural, computed from the dossier[/dim]"
        )


def render_concept_detail(ctx: dict[str, Any], concept: str) -> None:
    binding = ctx.get("concept_map", {}).get(concept)
    if not binding:
        console.print(f"[red]No such concept: {concept}[/red]")
        sys.exit(1)

    confidence = float(binding.get("confidence", 0.0))
    bar = _bar(confidence, width=10)

    header = Text.assemble(
        Text(concept, style="bold cyan"),
        Text("  "),
        Text(f"backed by ", style="dim"),
        Text(binding.get("table", "?"), style="default"),
        Text("  "),
        bar,
        Text(f"  {confidence:.2f}", style="bold"),
    )

    console.print()
    console.print(header)

    if binding.get("notes"):
        console.print(f"[italic]{binding['notes']}[/italic]")

    cols = binding.get("columns") or {}
    if cols:
        console.print("\n[bold]Columns[/bold]")
        for role, col in cols.items():
            console.print(f"  {role:18s} → {col}")

    ev = binding.get("evidence") or []
    if ev:
        console.print("\n[bold]Evidence[/bold]")
        for line in ev:
            console.print(f"  • {line}")

    console.print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence",
        action="store_true",
        help="show full evidence list per concept (multi-line)",
    )
    parser.add_argument(
        "--concept",
        help="render detail for a single concept by name",
    )
    parser.add_argument(
        "--context",
        default=str(CONTEXT_PATH),
        help="path to context.yaml",
    )
    args = parser.parse_args()

    path = Path(args.context)
    if not path.exists():
        console.print(
            f"[red]{path} does not exist. Run discovery first:[/red]\n"
            "  python discovery/introspect.py && python -m discovery.semantic_map"
        )
        sys.exit(1)

    ctx = yaml.safe_load(path.read_text())
    if args.concept:
        render_concept_detail(ctx, args.concept)
    else:
        render_catalog(ctx, show_full_evidence=args.evidence)


if __name__ == "__main__":
    main()
