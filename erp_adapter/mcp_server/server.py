"""MCP server that exposes the ERP adapter to any MCP-speaking client (Claude
Desktop, Claude Code, custom agents). Each tool is a thin wrapper over
`adapter.py` so the same logic powers the in-process agent demo too.

Run with:
    python -m mcp_server.server
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Allow `python mcp_server/server.py` from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server import Server  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402
from mcp.types import TextContent, Tool  # noqa: E402

from mcp_server import adapter  # noqa: E402

server: Server = Server("erp-adapter")


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[Tool] = [
    Tool(
        name="list_concepts",
        description=("List all business concepts the adapter knows about (item, "
                     "stock_by_warehouse, ap_invoice_header, etc.) and which "
                     "physical tables back them. Use this first to learn the shape "
                     "of THIS company's ERP."),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="find_item",
        description="Search for items (materials/SKUs) by id or partial name.",
        inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    ),
    Tool(
        name="get_stock",
        description=("Return per-warehouse stock for a material, including the *true* "
                     "available quantity (unrestricted minus active reservations) "
                     "and bin-level breakdown."),
        inputSchema={
            "type": "object",
            "properties": {"matnr": {"type": "string", "description": "Material ID, e.g. SKU-441"}},
            "required": ["matnr"],
        },
    ),
    Tool(
        name="suggest_source_warehouse",
        description=("Pick a source warehouse for a transfer. Considers learned "
                     "routing patterns (e.g., fragile items from WH02)."),
        inputSchema={
            "type": "object",
            "properties": {
                "matnr": {"type": "string"},
                "qty": {"type": "number"},
            },
            "required": ["matnr", "qty"],
        },
    ),
    Tool(
        name="move_stock",
        description=("Post a stock transfer between two warehouses. Updates bins, "
                     "rollups, and writes a material document (MSEG)."),
        inputSchema={
            "type": "object",
            "properties": {
                "matnr": {"type": "string"},
                "qty": {"type": "number"},
                "from_warehouse": {"type": "string"},
                "to_warehouse": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["matnr", "qty", "from_warehouse", "to_warehouse"],
        },
    ),
    Tool(
        name="post_invoice",
        description=("Park (and where eligible auto-approve) an AP invoice. Routing "
                     "and approval are governed by the discovered approval rules and "
                     "any learned per-vendor fast-tracks."),
        inputSchema={
            "type": "object",
            "properties": {
                "vendor": {"type": "string"},
                "amount": {"type": "number"},
                "currency": {"type": "string"},
                "lines": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["vendor", "amount"],
        },
    ),
    Tool(
        name="request_approval",
        description="Approve or reject a parked AP invoice on behalf of a named user.",
        inputSchema={
            "type": "object",
            "properties": {
                "belnr": {"type": "string"},
                "decided_by": {"type": "string"},
                "decision": {"type": "string", "enum": ["APPROVE", "REJECT"]},
            },
            "required": ["belnr", "decided_by", "decision"],
        },
    ),
    Tool(
        name="list_pending_invoices",
        description="List invoices currently parked or awaiting approval.",
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="submit_feedback",
        description=("Attach corrective feedback to a prior observation. The learner "
                     "uses this to refine routing and approval patterns."),
        inputSchema={
            "type": "object",
            "properties": {
                "obs_id": {"type": "integer"},
                "feedback": {"type": "string"},
            },
            "required": ["obs_id", "feedback"],
        },
    ),
]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOL_SCHEMAS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    actor = arguments.pop("_actor", "agent")
    fn_map = {
        "list_concepts": lambda **kw: adapter.list_concepts(actor=actor, **kw),
        "find_item": lambda **kw: adapter.find_item(actor=actor, **kw),
        "get_stock": lambda **kw: adapter.get_stock(actor=actor, **kw),
        "suggest_source_warehouse": lambda **kw: adapter.suggest_source_warehouse(actor=actor, **kw),
        "move_stock": lambda **kw: adapter.move_stock(actor=actor, **kw),
        "post_invoice": lambda **kw: adapter.post_invoice(actor=actor, **kw),
        "request_approval": lambda **kw: adapter.request_approval(actor=actor, **kw),
        "list_pending_invoices": lambda **kw: adapter.list_pending_invoices(actor=actor, **kw),
        "submit_feedback": lambda **kw: adapter.submit_feedback(actor=actor, **kw),
    }
    if name not in fn_map:
        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool {name}"}))]
    try:
        result = fn_map[name](**arguments)
    except Exception as e:  # surface as tool error rather than crash the server
        result = {"error": str(e), "type": type(e).__name__}
    return [TextContent(type="text", text=json.dumps(result, default=str))]


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
