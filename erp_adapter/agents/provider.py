"""Provider-agnostic LLM client factory for external Coat agents.

Coat's MCP surface is provider-agnostic by design. Atlas (and any other
specialized agent shipped here) works the same way whether it's running
on Claude, OpenAI o3, or Gemini 2.5 Pro — the system prompt, tool
catalog, and output shape are the specialization, not the model.

Pick the provider via env:

    ATLAS_PROVIDER=anthropic     ATLAS_MODEL=claude-opus-4-6
    ATLAS_PROVIDER=openai        ATLAS_MODEL=o3
    ATLAS_PROVIDER=google        ATLAS_MODEL=gemini-2.5-pro

If ATLAS_PROVIDER is unset, the factory auto-picks based on which
*_API_KEY is in the environment, preferring non-Claude providers so the
'Coat is model-agnostic' demo lands. If no key is set, callers should
fall back to scripted mode.

Each provider class implements one method:

    .tool_use_loop(system_prompt, user_message, tools, dispatch) -> str

That keeps the agent code itself indifferent to which provider's
tool-call API is in use.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ProviderChoice:
    name: str
    model: str


def detect_provider() -> ProviderChoice | None:
    """Pick a provider based on env. Returns None if no provider is reachable.

    Auto-picks prefer non-Claude providers so the demo defaults to showing
    Coat's provider-agnostic posture. Set ATLAS_PROVIDER explicitly to
    override.
    """
    explicit = os.environ.get("ATLAS_PROVIDER")
    if explicit:
        model = os.environ.get("ATLAS_MODEL") or _default_model(explicit)
        return ProviderChoice(name=explicit, model=model)

    # Auto-pick — prefer non-Claude when keys are present.
    if os.environ.get("OPENAI_API_KEY"):
        return ProviderChoice(name="openai", model=os.environ.get("ATLAS_MODEL") or "o3")
    if os.environ.get("GOOGLE_API_KEY"):
        return ProviderChoice(name="google", model=os.environ.get("ATLAS_MODEL") or "gemini-2.5-pro")
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ProviderChoice(name="anthropic", model=os.environ.get("ATLAS_MODEL") or "claude-opus-4-6")
    return None


def _default_model(provider: str) -> str:
    return {
        "anthropic": "claude-opus-4-6",
        "openai": "o3",
        "google": "gemini-2.5-pro",
    }.get(provider, "")


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str):
        self.model = model

    def tool_use_loop(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[dict[str, Any]],
        dispatch: Callable[[str, dict[str, Any]], dict[str, Any]],
        max_turns: int = 6,
    ) -> str:
        import anthropic
        client = anthropic.Anthropic()
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]

        for _ in range(max_turns):
            resp = client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system_prompt,
                tools=tools,
                messages=messages,
            )
            if resp.stop_reason != "tool_use":
                # Final answer — concatenate any text blocks
                return "\n".join(
                    blk.text for blk in resp.content if getattr(blk, "type", None) == "text"
                )

            tool_results: list[dict[str, Any]] = []
            for blk in resp.content:
                if getattr(blk, "type", None) != "tool_use":
                    continue
                try:
                    out = dispatch(blk.name, blk.input or {})
                except Exception as e:  # noqa: BLE001
                    out = {"error": str(e)}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": blk.id,
                    "content": json.dumps(out, default=str),
                })
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": tool_results})

        return "(max turns exceeded)"


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------

class OpenAIProvider:
    name = "openai"

    def __init__(self, model: str):
        self.model = model

    def tool_use_loop(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[dict[str, Any]],
        dispatch: Callable[[str, dict[str, Any]], dict[str, Any]],
        max_turns: int = 6,
    ) -> str:
        import openai
        client = openai.OpenAI()
        # Translate Anthropic-shape tool defs to OpenAI-shape on the fly
        oa_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
        ]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        for _ in range(max_turns):
            resp = client.chat.completions.create(
                model=self.model,
                tools=oa_tools,
                messages=messages,
            )
            choice = resp.choices[0]
            tc = choice.message.tool_calls or []
            if not tc:
                return choice.message.content or ""
            messages.append({"role": "assistant", "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in tc
            ], "content": choice.message.content or ""})
            for c in tc:
                args = json.loads(c.function.arguments or "{}")
                try:
                    out = dispatch(c.function.name, args)
                except Exception as e:  # noqa: BLE001
                    out = {"error": str(e)}
                messages.append({
                    "role": "tool",
                    "tool_call_id": c.id,
                    "content": json.dumps(out, default=str),
                })

        return "(max turns exceeded)"


# ---------------------------------------------------------------------------
# Google (Gemini)
# ---------------------------------------------------------------------------

class GoogleProvider:
    name = "google"

    def __init__(self, model: str):
        self.model = model

    def tool_use_loop(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[dict[str, Any]],
        dispatch: Callable[[str, dict[str, Any]], dict[str, Any]],
        max_turns: int = 6,
    ) -> str:
        # Lazy import — google-genai is optional.
        try:
            from google import genai  # type: ignore
            from google.genai import types  # type: ignore
        except ImportError as e:  # noqa: BLE001
            raise RuntimeError(
                "google-genai not installed. `pip install google-genai` to use the google provider."
            ) from e

        client = genai.Client()
        google_tools = [
            types.Tool(function_declarations=[
                types.FunctionDeclaration(
                    name=t["name"],
                    description=t.get("description", ""),
                    parameters=t.get("input_schema", {"type": "object", "properties": {}}),
                )
                for t in tools
            ])
        ]
        chat = client.chats.create(
            model=self.model,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=google_tools,
            ),
        )
        msg: Any = user_message
        for _ in range(max_turns):
            resp = chat.send_message(msg)
            fcs = getattr(resp, "function_calls", None) or []
            if not fcs:
                return resp.text or ""
            tool_responses: list[Any] = []
            for fc in fcs:
                args = dict(fc.args) if fc.args else {}
                try:
                    out = dispatch(fc.name, args)
                except Exception as e:  # noqa: BLE001
                    out = {"error": str(e)}
                tool_responses.append(types.Part.from_function_response(
                    name=fc.name,
                    response={"result": out},
                ))
            msg = tool_responses

        return "(max turns exceeded)"


def make_provider(choice: ProviderChoice):
    if choice.name == "anthropic":
        return AnthropicProvider(choice.model)
    if choice.name == "openai":
        return OpenAIProvider(choice.model)
    if choice.name == "google":
        return GoogleProvider(choice.model)
    raise ValueError(f"unknown provider {choice.name!r}")
