"""`coat agent onboard` — derive an agent manifest from a plain-English description
and persist it after human ratification.

This is the philosophy-on-screen moment from the demo runbook. The admin
describes the agent's job. Coat infers scopes + bundles + denied lines.
The human ratifies with one keystroke. The agent goes live in trial mode.

The admin never types a scope string. The cognitive load is the
description — same load as briefing a new hire.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

# Make sibling packages importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import manifest_derivation  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "erp.db"

console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug_from_description(desc: str) -> str:
    """Pull the first proper-noun-ish word as the agent slug."""
    # Match 'Atlas — ...' or 'Atlas: ...' or 'Atlas, ...' or just the first
    # capitalized token.
    m = re.match(r"\s*([A-Z][A-Za-z0-9_-]{2,})\b", desc)
    if m:
        return m.group(1).lower()
    return "agent-" + uuid.uuid4().hex[:8]


def _suggested_id(desc: str) -> str:
    return f"{_slug_from_description(desc)}@coat.io/v1"


def _read_description(from_file: str | None) -> str:
    if from_file:
        return Path(from_file).read_text().strip()

    console.print(
        "[bold]Describe what this agent does.[/bold] "
        "Plain English. End with an empty line."
    )
    lines: list[str] = []
    while True:
        try:
            line = input("> ")
        except EOFError:
            break
        if line == "":
            if lines:
                break
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _bullet(text: str, color: str = "default") -> Text:
    return Text("• ", style="dim") + Text(text, style=color)


def _render_proposal(agent_id: str, proposal: dict[str, Any]) -> None:
    manifest = proposal["manifest"]

    # Header
    console.print()
    console.print(Text(f"PROPOSED AGENT — {agent_id}", style="bold"))
    if proposal.get("summary"):
        console.print(Text(proposal["summary"], style="italic dim"))
    console.print()

    # Description (echoed back)
    console.print(Panel.fit(proposal["description"], title="description", border_style="dim"))

    # Domains + signals
    if proposal.get("domains"):
        console.print(
            Text("Domains: ", style="dim") + Text(", ".join(proposal["domains"]))
        )
    if proposal.get("external_signals_mentioned"):
        console.print(
            Text("External signals mentioned: ", style="dim")
            + Text(", ".join(proposal["external_signals_mentioned"]))
        )

    console.print()

    # Granted
    console.print(Text("Inferred scopes (least privilege)", style="bold green"))
    if not manifest["granted_scopes"]:
        console.print("  [dim]none — description didn't imply any read/write capability[/dim]")
    for s in manifest["granted_scopes"]:
        hint = manifest_derivation.SCOPE_CATALOG.get(s, "")
        console.print(
            Text("  ✓ ", style="green")
            + Text(s, style="bold")
            + (Text("    " + hint, style="dim") if hint else Text(""))
        )

    # Bundles
    if manifest["bundles"]:
        console.print()
        console.print(Text("Context bundles this agent will receive", style="bold cyan"))
        for b in manifest["bundles"]:
            hint = manifest_derivation.BUNDLE_CATALOG.get(b, "")
            console.print(
                Text("  ⊕ ", style="cyan")
                + Text(b, style="bold")
                + (Text("    " + hint, style="dim") if hint else Text(""))
            )

    # Denied
    console.print()
    console.print(Text("NOT granted", style="bold red"))
    if not manifest["denied_scopes"]:
        console.print("  [dim](nothing explicitly denied)[/dim]")
    for d in manifest["denied_scopes"]:
        console.print(
            Text("  ✗ ", style="red")
            + Text(d["scope"], style="bold")
            + Text("    " + d["reason"], style="dim")
        )

    # Mode + budget
    console.print()
    console.print(
        Text("Mode: ", style="dim")
        + Text(manifest["mode"], style="bold yellow")
        + Text(
            f"   trial budget: {manifest['trial_max_calls']} calls / {manifest['trial_max_days']}d",
            style="dim",
        )
    )

    # Source line
    console.print(
        Text(
            f"\n[dim]Manifest derived via {proposal['source']}; "
            f"all scopes are explicit, deny is the default.[/dim]"
        )
    )


def _ratify_prompt() -> str:
    console.print()
    console.print(
        "[bold][r]atify[/bold]   [bold][e]dit description[/bold]   [bold][c]ancel[/bold]"
    )
    while True:
        try:
            choice = input("> ").strip().lower()
        except EOFError:
            return "c"
        if choice in ("r", "e", "c", "ratify", "edit", "cancel"):
            return choice[0]


def _persist(
    agent_id: str,
    proposal: dict[str, Any],
    provider: str | None,
    model: str | None,
    registered_by: str = "u_admin",
) -> str:
    audit_id = "aud_" + uuid.uuid4().hex
    expires = (datetime.now(timezone.utc) + timedelta(days=proposal["manifest"]["trial_max_days"])).isoformat(
        timespec="seconds"
    )
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            INSERT INTO AGENTS (
                AGENT_ID, DESCRIPTION, PROVIDER, MODEL, MANIFEST_JSON, STATUS,
                TRIAL_CALLS_USED, TRIAL_CALLS_MAX, TRIAL_EXPIRES_AT,
                REGISTERED_AT, REGISTERED_BY, AUDIT_ID
            ) VALUES (?, ?, ?, ?, ?, 'trial', 0, ?, ?, ?, ?, ?)
            ON CONFLICT(AGENT_ID) DO UPDATE SET
                DESCRIPTION   = excluded.DESCRIPTION,
                PROVIDER      = excluded.PROVIDER,
                MODEL         = excluded.MODEL,
                MANIFEST_JSON = excluded.MANIFEST_JSON,
                STATUS        = 'trial',
                TRIAL_CALLS_USED = 0,
                TRIAL_CALLS_MAX  = excluded.TRIAL_CALLS_MAX,
                TRIAL_EXPIRES_AT = excluded.TRIAL_EXPIRES_AT,
                REGISTERED_AT    = excluded.REGISTERED_AT,
                REGISTERED_BY    = excluded.REGISTERED_BY,
                AUDIT_ID         = excluded.AUDIT_ID
            """,
            (
                agent_id,
                proposal["description"],
                provider,
                model,
                json.dumps(proposal["manifest"], default=str),
                proposal["manifest"]["trial_max_calls"],
                expires,
                _now_iso(),
                registered_by,
                audit_id,
            ),
        )
        # Capability grant audit rows
        for scope in proposal["manifest"]["granted_scopes"]:
            conn.execute(
                """
                INSERT INTO CAPABILITY_GRANTS
                    (AGENT_ID, SCOPE, ORIGIN, GRANTED_AT, GRANTED_BY, NOTE)
                VALUES (?, ?, 'manifest', ?, ?, ?)
                """,
                (agent_id, scope, _now_iso(), registered_by,
                 "granted at onboarding via inferred manifest"),
            )
        conn.commit()
    finally:
        conn.close()
    return audit_id


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def run(
    from_file: str | None = None,
    explicit_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    auto_yes: bool = False,
    registered_by: str = "u_admin",
) -> None:
    description = _read_description(from_file)
    if not description:
        console.print("[red]No description provided. Aborting.[/red]")
        sys.exit(1)

    agent_id = explicit_id or _suggested_id(description)

    console.print(
        Panel.fit(
            f"[dim]Coat is reading the description…[/dim]",
            border_style="dim",
        )
    )

    proposal = manifest_derivation.derive_manifest(description)
    _render_proposal(agent_id, proposal)

    if auto_yes:
        choice = "r"
    else:
        choice = _ratify_prompt()

    if choice == "c":
        console.print("[yellow]Cancelled. No agent registered.[/yellow]")
        return
    if choice == "e":
        console.print("[dim]Re-run `coat agent onboard` with the revised description.[/dim]")
        return

    audit_id = _persist(agent_id, proposal, provider, model, registered_by)

    expires_in = proposal["manifest"]["trial_max_days"]
    console.print()
    console.print(
        Panel.fit(
            Text.assemble(
                Text("✓ ", style="bold green"),
                Text(agent_id, style="bold"),
                Text(" onboarded.", style="default"),
                Text(f"\n  audit_id={audit_id}", style="dim"),
                Text(
                    f"\n  trial budget: {proposal['manifest']['trial_max_calls']} calls / "
                    f"{expires_in}d",
                    style="dim",
                ),
                Text(
                    f"\n  next ratification due: after budget consumed OR "
                    f"{(datetime.now(timezone.utc) + timedelta(days=expires_in)).date().isoformat()}",
                    style="dim",
                ),
            ),
            title="onboarded",
            border_style="green",
        )
    )


if __name__ == "__main__":
    run()
