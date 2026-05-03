"""External agents that plug into Coat over MCP.

Each module here is a *configured* agent — a specialized agent is the
combination of a system prompt, a recommended tool/bundle catalog, an
output shape, and a model provider. The model itself is not the
specialization; the configuration is.

Adding a new agent here means:
  1. Pick a domain (inventory, vendor risk, AP triage, ...).
  2. Write the system prompt that turns a generic LLM into a domain
     specialist.
  3. Pick the bundles/primitives the agent should call.
  4. Define the output shape.
  5. Wire to the provider of choice (Anthropic, OpenAI, Google) via the
     factory in `agents.provider`.
"""
