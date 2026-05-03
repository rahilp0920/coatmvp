"""Context bundles — the layer agents actually call.

Bundles are business-shaped, fully-assembled views over Coat's data plus
external signals Coat has registered. Agents call one bundle tool and
get a single payload to reason over — no plumbing, no API keys, no
joining in the agent's prompt.

Each bundle module exposes one or more assembler functions that compose
adapter primitives, change-history queries, learned patterns, and
external signals into a documented schema. Assemblers do NOT call
LLMs — bundles are data, not text generation.

Add a new bundle by:
  1. Define the bundle's data shape in <name>.py (TypedDict or pydantic).
  2. Implement the assembler reading from the adapter, learner,
     EXTERNAL_SIGNALS, and any other Coat-owned table.
  3. Register a tool in mcp_server/server.py that calls the assembler
     and exposes it under coat:context:<name>:read scope.
"""
