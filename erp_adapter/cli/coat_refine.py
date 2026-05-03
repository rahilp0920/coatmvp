"""`coat refine` — raise confidence on the concept catalog from observed work.

Day-one discovery is a hypothesis. Coat asks Claude (or its curated
fallback) to label each table semantically and computes a structural
confidence per concept (PK shape, FK referrer count, sample-value
distribution match — see `discovery/confidence.py`). That number is
honest but it's bounded: there are concepts you can only be sure
about after you watch the system actually get used.

`coat refine` closes that loop. It scans recent `WORKFLOW_OBS` rows,
identifies which concepts each event touched, and bumps confidence
on concepts with sustained operator activity. The refinement
preserves the structural score and adds an "+ N recent observations
confirm" evidence line so the catalog tells the same story whether
you're looking at day-one or day-thirty.

The architecture point: structural confidence is a starting prior;
operator workflows are evidence; confidence updates as the system
gets used. That is exactly the OBSERVABILITY thesis at the
schema-understanding layer.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "erp.db"
CONTEXT_PATH = ROOT / "context" / "context.yaml"

console = Console()


# Mapping from observed tool/event name to concepts that get touched.
# When Coat sees a `consume_stock` event, it counts as evidence on
# `item`, `warehouse`, `stock_by_warehouse`, `stock_by_bin`, and
# `stock_movement` because the operation must read or write each.
TOOL_TOUCHES_CONCEPTS: dict[str, list[str]] = {
    "consume_stock":             ["item", "warehouse", "stock_by_warehouse",
                                  "stock_by_bin", "stock_movement", "reservation"],
    "move_stock":                ["item", "warehouse", "stock_by_warehouse",
                                  "stock_by_bin", "stock_movement"],
    "get_stock":                 ["item", "warehouse", "stock_by_warehouse",
                                  "stock_by_bin", "reservation"],
    "suggest_source_warehouse":  ["item", "warehouse", "stock_by_warehouse",
                                  "stock_by_bin"],
    "post_invoice":              ["vendor", "ap_invoice_header", "ap_invoice_line",
                                  "gl_entry", "approval_rule"],
    "request_approval":          ["ap_invoice_header", "user", "approval_rule"],
    "list_pending_invoices":     ["ap_invoice_header", "vendor"],
    "submit_feedback":           [],
    "get_inventory_context":     ["item", "warehouse", "stock_by_warehouse",
                                  "stock_by_bin", "reservation",
                                  "stock_movement"],
    "find_item":                 ["item"],
    "list_concepts":             [],
    "external_signal_ingest":    [],
}


# --- Bump policy ---------------------------------------------------------
# +0.02 of confidence per 10 observations on a concept's backing table,
# capped at +0.10. This is intentionally conservative — observations are
# evidence, not proof, and we never overwrite the structural score.
BUMP_PER_TEN_OBS = 0.02
MAX_BUMP = 0.10
MIN_OBS_FOR_BUMP = 10
CONFIDENCE_CAP = 0.99


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_window(spec: str) -> timedelta:
    m = re.match(r"^\s*(\d+)\s*([hdmw])\s*$", spec, re.IGNORECASE)
    if not m:
        return timedelta(hours=2)
    n, unit = int(m.group(1)), m.group(2).lower()
    return {
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
        "w": timedelta(weeks=n),
    }[unit]


def _count_touches(window: timedelta) -> dict[str, int]:
    """Walk WORKFLOW_OBS within the window and tally per-concept touches."""
    import sqlite3
    cutoff = (_now_utc() - window).isoformat(timespec="seconds")
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT TOOL FROM WORKFLOW_OBS WHERE TS >= ?",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    counts: dict[str, int] = defaultdict(int)
    for (tool,) in rows:
        for concept in TOOL_TOUCHES_CONCEPTS.get(tool or "", []):
            counts[concept] += 1
    return dict(counts)


def _bump_confidence(old: float, n_obs: int) -> float:
    if n_obs < MIN_OBS_FOR_BUMP:
        return old
    bumps = n_obs // 10
    raw_bump = min(MAX_BUMP, BUMP_PER_TEN_OBS * bumps)
    new = min(CONFIDENCE_CAP, old + raw_bump)
    return round(new, 3)


def _delete_prior_observation_evidence(evidence: list[str]) -> list[str]:
    """Strip any evidence line previously added by a refine pass so a re-run
    doesn't accumulate stale lines."""
    return [
        e for e in evidence
        if not e.startswith("+ ") or "operator observations" not in e
    ]


def refine(*, window: str = "2h") -> None:
    if not CONTEXT_PATH.exists():
        console.print(
            "[red]No context.yaml. Run [bold]coat init[/bold] first.[/red]"
        )
        return

    delta = _parse_window(window)
    counts = _count_touches(delta)

    ctx = yaml.safe_load(CONTEXT_PATH.read_text())
    cmap = ctx.get("concept_map", {}) or {}
    if not cmap:
        console.print("[red]context.yaml has no concept_map.[/red]")
        return

    table = Table(
        title=Text(
            f"COAT REFINE  •  observation window: last {window}",
            style="bold",
        ),
        title_justify="left",
        box=box.SIMPLE_HEAD,
        pad_edge=False,
        padding=(0, 1),
    )
    table.add_column("CONCEPT", style="cyan", no_wrap=True)
    table.add_column("OBS", justify="right")
    table.add_column("CONFIDENCE", no_wrap=True)
    table.add_column("Δ", no_wrap=True)

    bumped = 0
    total_obs = 0
    for name, binding in cmap.items():
        n = counts.get(name, 0)
        old = float(binding.get("confidence", 0.0))
        new = _bump_confidence(old, n)
        delta_val = round(new - old, 3)
        if delta_val > 0:
            bumped += 1
            total_obs += n
            evidence = list(binding.get("evidence") or [])
            evidence = _delete_prior_observation_evidence(evidence)
            evidence.append(
                f"+ {n} recent operator observations confirm operator workflow"
            )
            binding["evidence"] = evidence
            binding["confidence"] = new

        # Render — even if no bump, show the obs count
        bar_old = "█" * round(old * 5) + "░" * (5 - round(old * 5))
        bar_new = "█" * round(new * 5) + "░" * (5 - round(new * 5))
        if delta_val > 0:
            conf_cell = Text.assemble(
                Text(bar_old, style="dim"),
                Text(f" {old:.2f}  →  ", style="dim"),
                Text(bar_new, style="bright_green"),
                Text(f" {new:.2f}", style="bold"),
            )
            delta_cell = Text(f"+{delta_val:.2f}", style="bold bright_green")
        else:
            conf_cell = Text.assemble(
                Text(bar_old, style="dim"),
                Text(f" {old:.2f}", style="dim"),
            )
            delta_cell = Text("—" if n == 0 else f" {n} obs (< {MIN_OBS_FOR_BUMP})",
                              style="dim")

        table.add_row(name, str(n) if n else "—", conf_cell, delta_cell)

    # Persist updated context
    CONTEXT_PATH.write_text(yaml.safe_dump(ctx, sort_keys=False, default_flow_style=False))
    console.print(table)

    if bumped == 0:
        console.print(
            f"\n[dim]No concept reached the {MIN_OBS_FOR_BUMP}-observation "
            f"threshold yet. Run [bold]coat sim activity[/bold] to simulate "
            f"operator work, then re-run refine.[/dim]"
        )
        return

    console.print(
        Panel.fit(
            Text.assemble(
                Text(f"✓ refined {bumped} concept(s) from ", style="bold green"),
                Text(f"{total_obs} recent observations.\n", style="bold"),
                Text(
                    f"  Operator activity is now evidence on the concept catalog.\n"
                    f"  Discovery was the prior; observations are the posterior.\n"
                    f"  Run [bold]python -m cli.render_concepts[/bold] to see the "
                    f"updated catalog,\n  or re-run [bold]atlas[/bold] — agents using "
                    f"the bundle inherit the refined confidence.",
                    style="dim",
                ),
            ),
            title="Coat — confidence refined from operator workflows",
            border_style="green",
        )
    )
