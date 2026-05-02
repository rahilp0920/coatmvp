"""End-to-end agent demo.

A Claude agent receives natural-language tasks and uses the ERP adapter's
high-level tools to execute them. The agent doesn't see the messy schema —
it only sees business concepts and tools, courtesy of the MCP layer.

Two modes:
  - With ANTHROPIC_API_KEY: real Claude agent loop with tool use.
  - Without: scripted "demo agent" that calls the same tools directly so the
    MVP is demoable offline.

Demo scenarios:
  1. Inventory restock: "We're low on SKU-441 at WH01. Move 80 units from the
     best source." -> agent uses learned-routing to source from WH02.
  2. Invoice posting: "Post a $2,500 invoice from V1001." -> learned fast-track
     auto-approves it. Then post the same amount from V1002 -> parks for review.
  3. Live correction: user submits feedback "actually, prefer WH03 for SKU-200
     transfers because it's closer to the customer". Run learner. Re-route.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Windows: force UTF-8 stdout so rich box-drawing/arrows don't crash on cp1252.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.rule import Rule  # noqa: E402
from rich.table import Table  # noqa: E402

from mcp_server import adapter  # noqa: E402
from learner.miner import run_once as relearn  # noqa: E402

console = Console()

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-7")

SYSTEM_PROMPT = """You are an ERP automation agent. You drive an unfamiliar
company's ERP through a small set of business-level tools. The schemas of the
underlying tables are messy and company-specific — never assume column names
or table layout, and never write SQL. Only use the provided tools.

Workflow:
  1. Call list_concepts first to learn what business objects this ERP exposes.
  2. For inventory tasks, prefer suggest_source_warehouse before move_stock so
     learned routing patterns can guide the choice. Always show the user the
     "reason" returned by suggest_source_warehouse.
  3. For invoice tasks, post_invoice handles routing + approval automatically.
     Surface the `approval.learned_note` field if present.
  4. After every action, give the user a one-line summary of what happened
     and any learned-pattern note that influenced the decision.

Be terse. No filler."""


# ---------------------------------------------------------------------------
# Tool dispatch shared between live agent and scripted demo
# ---------------------------------------------------------------------------

TOOLS = {
    "list_concepts": adapter.list_concepts,
    "find_item": adapter.find_item,
    "get_stock": adapter.get_stock,
    "suggest_source_warehouse": adapter.suggest_source_warehouse,
    "move_stock": adapter.move_stock,
    "post_invoice": adapter.post_invoice,
    "request_approval": adapter.request_approval,
    "list_pending_invoices": adapter.list_pending_invoices,
    "submit_feedback": adapter.submit_feedback,
}


def _run_tool(name: str, args: dict) -> dict:
    return TOOLS[name](**args)


# ---------------------------------------------------------------------------
# Live Claude agent (tool-use loop)
# ---------------------------------------------------------------------------

ANTHROPIC_TOOLS = [
    {
        "name": "list_concepts",
        "description": "List business concepts (item, stock, invoice, ...) and their backing tables.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "find_item",
        "description": "Search for items by id or partial name.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_stock",
        "description": "Per-warehouse stock with true available qty (after reservations).",
        "input_schema": {
            "type": "object",
            "properties": {"matnr": {"type": "string"}},
            "required": ["matnr"],
        },
    },
    {
        "name": "suggest_source_warehouse",
        "description": "Pick a source warehouse for a transfer (uses learned routing).",
        "input_schema": {
            "type": "object",
            "properties": {"matnr": {"type": "string"}, "qty": {"type": "number"}},
            "required": ["matnr", "qty"],
        },
    },
    {
        "name": "move_stock",
        "description": "Post a stock transfer.",
        "input_schema": {
            "type": "object",
            "properties": {
                "matnr": {"type": "string"}, "qty": {"type": "number"},
                "from_warehouse": {"type": "string"}, "to_warehouse": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["matnr", "qty", "from_warehouse", "to_warehouse"],
        },
    },
    {
        "name": "post_invoice",
        "description": "Post an AP invoice (routing+approval handled by adapter).",
        "input_schema": {
            "type": "object",
            "properties": {
                "vendor": {"type": "string"}, "amount": {"type": "number"},
                "currency": {"type": "string"},
                "lines": {"type": "array"},
            },
            "required": ["vendor", "amount"],
        },
    },
    {
        "name": "list_pending_invoices",
        "description": "List parked/unposted invoices.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def run_live_agent(task: str) -> None:
    import anthropic
    client = anthropic.Anthropic()
    messages: list = [{"role": "user", "content": task}]

    for _ in range(10):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=ANTHROPIC_TOOLS,
            messages=messages,
        )
        # Print any text the model produced
        for block in resp.content:
            if block.type == "text" and block.text.strip():
                console.print(Panel(block.text, title="agent", border_style="cyan"))

        if resp.stop_reason != "tool_use":
            break

        # Execute tool calls and feed results back
        tool_results: list = []
        for block in resp.content:
            if block.type == "tool_use":
                console.print(f"[dim]→ {block.name}({json.dumps(block.input)})[/dim]")
                try:
                    out = _run_tool(block.name, block.input)
                except Exception as e:  # noqa: BLE001
                    out = {"error": str(e)}
                console.print(f"[dim]  ← {json.dumps(out, default=str)[:300]}[/dim]")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(out, default=str),
                })
        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": tool_results})


# ---------------------------------------------------------------------------
# Scripted offline demo
# ---------------------------------------------------------------------------

def _print_call(name: str, args: dict, result: dict) -> None:
    console.print(f"[bold cyan]agent →[/bold cyan] [yellow]{name}[/yellow]({json.dumps(args)})")
    summary = json.dumps(result, default=str, indent=2)
    if len(summary) > 1200:
        summary = summary[:1200] + "\n  ... (truncated)"
    console.print(Panel(summary, border_style="dim", title="result"))


def scripted_scenario_inventory() -> None:
    console.print(Rule("[bold]SCENARIO 1 — Inventory restock[/bold]"))
    console.print("[italic]User: 'We're low on SKU-441 at WH01. Move 80 units from "
                  "the best source.'[/italic]\n")

    args = {}
    res = _run_tool("list_concepts", args)
    _print_call("list_concepts", args, {"concept_count": len(res["concepts"])})

    args = {"matnr": "SKU-441"}
    res = _run_tool("get_stock", args)
    _print_call("get_stock", args, res)

    args = {"matnr": "SKU-441", "qty": 80}
    suggestion = _run_tool("suggest_source_warehouse", args)
    _print_call("suggest_source_warehouse", args, suggestion)

    args = {"matnr": "SKU-441", "qty": 80,
            "from_warehouse": suggestion["chosen"], "to_warehouse": "WH01",
            "reason": "Restock per user request; sourced via learned routing"}
    res = _run_tool("move_stock", args)
    _print_call("move_stock", args, res)


def scripted_scenario_invoice() -> None:
    console.print(Rule("[bold]SCENARIO 2 — Invoice posting (learned fast-track)[/bold]"))
    console.print("[italic]User: 'Post a $2,500 invoice from V1001 and another "
                  "$2,500 from V1002.'[/italic]\n")

    for vendor in ("V1001", "V1002"):
        args = {"vendor": vendor, "amount": 2500.0}
        res = _run_tool("post_invoice", args)
        _print_call("post_invoice", args, res)


def scripted_scenario_feedback() -> None:
    console.print(Rule("[bold]SCENARIO 3 — Live correction via feedback[/bold]"))
    console.print(
        "[italic]User: 'For SKU-200 specifically, prefer WH03 — it's closer to "
        "our biggest customer.'[/italic]\n"
    )

    # Simulate a routing call that the user wants to override
    args = {"matnr": "SKU-200", "qty": 25}
    suggestion = _run_tool("suggest_source_warehouse", args)
    _print_call("suggest_source_warehouse (before feedback)", args, suggestion)

    # Pull the obs id of the call we just made
    import sqlite3
    conn = sqlite3.connect(ROOT / "data" / "erp.db")
    obs_id = conn.execute("SELECT MAX(OBS_ID) FROM WORKFLOW_OBS").fetchone()[0]
    conn.close()

    fb_args = {"obs_id": obs_id,
               "feedback": "For SKU-200 prefer WH03; closer to top customer."}
    fb_res = _run_tool("submit_feedback", fb_args)
    _print_call("submit_feedback", fb_args, fb_res)

    # The submit_feedback log triggered a re-mine inside the adapter, but to
    # show the pattern table contents:
    relearn(verbose=False)
    import sqlite3
    conn = sqlite3.connect(ROOT / "data" / "erp.db")
    rows = conn.execute(
        "SELECT KIND, KEY, SUPPORT, CONFIDENCE, VALUE_JSON FROM LEARNED_PATTERNS"
    ).fetchall()
    conn.close()
    table = Table(title="LEARNED_PATTERNS after feedback")
    for col in ("KIND", "KEY", "SUPPORT", "CONF", "VALUE"):
        table.add_column(col, overflow="fold")
    for r in rows:
        v = json.loads(r[4])
        table.add_row(r[0], r[1], str(r[2]), f"{r[3]:.2f}", json.dumps(v)[:120])
    console.print(table)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "scripted"

    if mode == "live":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            console.print("[red]ANTHROPIC_API_KEY not set; falling back to scripted mode.[/red]")
            mode = "scripted"

    if mode == "live":
        task = " ".join(sys.argv[2:]) or (
            "We're low on SKU-441 at WH01. Move 80 units from the best source, "
            "then post a $2,500 invoice from vendor V1001."
        )
        console.print(Panel(f"[bold]Task:[/bold] {task}", border_style="green"))
        run_live_agent(task)
        return

    scripted_scenario_inventory()
    scripted_scenario_invoice()
    scripted_scenario_feedback()


if __name__ == "__main__":
    main()
