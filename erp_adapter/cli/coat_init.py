"""`coat init` — install Coat into a workspace.

This is the one-time setup the customer (or, in our demo, you) runs to
turn an empty workspace into a working Coat install. It:

  1. Builds (or rebuilds) the mock ERP database with realistic seed
     data + historical movements for the learner.
  2. Runs schema introspection to dump a dossier of every table.
  3. Asks Claude (or, offline, falls back to the curated mapping) to
     label the dossier semantically and produces context.yaml — Coat's
     concept map for THIS ERP.
  4. Computes structural confidence per concept and bakes it into the
     context map so the customer can see Coat's grounding.
  5. Mines historical observations into LEARNED_PATTERNS so the agent
     has something to work with on day one.
  6. Renders the discovered concept catalog so the customer sees,
     immediately, what Coat now understands about their ERP.

After this, run `coat watch` in a side pane to keep eyes on what Coat
sees, then onboard your first agent with `coat agent onboard`.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent

console = Console()


def _step(label: str) -> None:
    console.rule(f"[bold]{label}[/bold]", style="dim")


def _run(cmd: list[str]) -> None:
    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    subprocess.run(cmd, check=True, cwd=ROOT, env=env)


def init() -> None:
    console.print(
        Panel.fit(
            Text.assemble(
                Text("coat init", style="bold"),
                Text("\nturning this workspace into a Coat install", style="dim"),
            ),
            border_style="cyan",
        )
    )

    _step("1/5  Build the mock ERP")
    _run([sys.executable, "mock_erp/seed.py"])

    _step("2/5  Introspect + semantically map")
    _run([sys.executable, "discovery/introspect.py"])
    _run([sys.executable, "-m", "discovery.semantic_map"])

    _step("3/5  Render the discovered concept catalog")
    _run([sys.executable, "-m", "cli.render_concepts"])

    _step("4/5  Mine historical observations into patterns")
    _run([sys.executable, "-m", "learner.miner"])

    _step("5/5  Done")
    console.print(
        Panel.fit(
            Text.assemble(
                Text("✓ Coat is installed.\n", style="bold green"),
                Text("\n  next:  ", style="dim"),
                Text("coat watch", style="bold cyan"),
                Text("           keep an eye on changes (run in another pane)\n", style="dim"),
                Text("         ", style="dim"),
                Text("coat agent onboard", style="bold cyan"),
                Text("   describe and register your first agent\n", style="dim"),
                Text("         ", style="dim"),
                Text("coat audit --entity <id>", style="bold cyan"),
                Text("    inspect anything Coat saw", style="dim"),
            ),
            border_style="green",
        )
    )


if __name__ == "__main__":
    init()
