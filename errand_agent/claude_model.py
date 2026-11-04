"""Live Claude adapter for the agent's own tool loop.

The orchestrator runs the loop (it must stop at proposals, enforce budgets, and inject
state), so this is a single `messages.create` per step rather than the SDK tool runner.
"""

from __future__ import annotations

import os

import anthropic

from .model import ModelResponse, ToolCall, Usage

DEFAULT_MODEL = "claude-opus-5-5"


class ClaudeModel:
    def __init__(
        self,
        model: str | None = None,
        effort: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 1,
    ) -> None:
        self.name = model or os.environ.get("ERRAND_MODEL", DEFAULT_MODEL)
        # Medium suits multi-step tool use here; raise it if planning quality drops.
        self.effort = effort or os.environ.get("ERRAND_EFFORT", "medium")
        key = api_key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
        self.client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=max_retries)

    def complete(self, *, system: str, messages: list[dict], tools: list[dict]) -> ModelResponse:
        resp = self.client.beta.messages.create(
            model=self.name,
            max_tokens=16000,
            # Breakpoint on the frozen system prompt (tools render before it), so every turn of every
            # session reuses the cached tools + system prefix even though chat history changes.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=tools,
            messages=messages,
            output_config={"effort": self.effort},
            # Second breakpoint at the end, so tool-loop steps within a turn hit the cache too.
            cache_control={"type": "ephemeral"},
            # On a safety-classifier decline, the API re-runs the request on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use"]
        u = resp.usage
        return ModelResponse(
            text=text,
            tool_calls=calls,
            usage=Usage(
                input_tokens=u.input_tokens or 0,
                output_tokens=u.output_tokens or 0,
                cache_read_input_tokens=u.cache_read_input_tokens or 0,
                cache_creation_input_tokens=u.cache_creation_input_tokens or 0,
            ),
            model=resp.model,
            stop_reason=resp.stop_reason,
            content=[b.to_dict() for b in resp.content],
        )
