"""Model interface plus a scripted model for offline tests and evals.

Messages use the Anthropic Messages format so the live adapter is a thin wrapper.
"""

from __future__ import annotations

import itertools
import json
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol, Union

# USD per million tokens: (input, output, cache read, cache write).
PRICING: dict[str, tuple[float, float, float, float]] = {
    "scripted": (0.0, 0.0, 0.0, 0.0),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
}


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class ModelResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = "scripted"
    stop_reason: str | None = None
    # Raw assistant content blocks (thinking included) to replay verbatim within a turn.
    # None for scripted responses, which are rebuilt from text and tool_calls.
    content: list[dict] | None = None


@dataclass
class ModelRequest:
    system: str
    messages: list[dict]
    tools: list[dict]


class Model(Protocol):
    name: str

    def complete(self, *, system: str, messages: list[dict], tools: list[dict]) -> ModelResponse: ...


def estimate_cost(model: str, usage: Usage) -> float:
    price_in, price_out, cache_read, cache_write = PRICING.get(model, (0.0, 0.0, 0.0, 0.0))
    return (
        usage.input_tokens * price_in
        + usage.output_tokens * price_out
        + usage.cache_read_input_tokens * cache_read
        + usage.cache_creation_input_tokens * cache_write
    ) / 1_000_000


def estimate_tokens(obj: object) -> int:
    """Rough count (4 chars per token) for offline runs and context budgeting."""
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    return max(1, len(text) // 4)


class ScriptExhausted(Exception):
    pass


Step = Union[ModelResponse, Callable[[ModelRequest], ModelResponse]]


class ScriptedModel:
    """Replays a fixed list of responses. A step may be a callable that inspects the request."""

    name = "scripted"

    def __init__(self, steps: Iterable[Step] = ()) -> None:
        self.steps: deque[Step] = deque(steps)
        self.requests: list[ModelRequest] = []

    def add(self, *steps: Step) -> None:
        self.steps.extend(steps)

    def complete(self, *, system: str, messages: list[dict], tools: list[dict]) -> ModelResponse:
        request = ModelRequest(system, json.loads(json.dumps(messages, default=str)), tools)
        self.requests.append(request)
        if not self.steps:
            raise ScriptExhausted("scripted model has no more steps")
        step = self.steps.popleft()
        response = step(request) if callable(step) else step
        response.usage = Usage(
            input_tokens=estimate_tokens(system) + estimate_tokens(messages),
            output_tokens=estimate_tokens(response.text or " ") + estimate_tokens([c.args for c in response.tool_calls]),
        )
        return response


_ids = itertools.count(1)


def say(text: str) -> ModelResponse:
    return ModelResponse(text=text)


def use(name: str, text: str = "", **args: object) -> ModelResponse:
    return ModelResponse(text=text, tool_calls=[ToolCall(f"call_{next(_ids)}", name, dict(args))])


def use_many(*calls: tuple[str, dict], text: str = "") -> ModelResponse:
    return ModelResponse(text=text, tool_calls=[ToolCall(f"call_{next(_ids)}", n, dict(a)) for n, a in calls])


class RecordingModel:
    """Wraps a live model and saves every response, so the run can be replayed offline."""

    def __init__(self, inner: Model, path: str | Path) -> None:
        self.inner = inner
        self.name = inner.name
        self.path = Path(path)
        self.responses: list[dict] = []
        self.requests: list[ModelRequest] = []

    def complete(self, *, system: str, messages: list[dict], tools: list[dict]) -> ModelResponse:
        self.requests.append(ModelRequest(system, json.loads(json.dumps(messages, default=str)), tools))
        response = self.inner.complete(system=system, messages=messages, tools=tools)
        self.responses.append(asdict(response))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"model": self.name, "responses": self.responses}, indent=1,
                                        ensure_ascii=False))
        return response


class ReplayModel(ScriptedModel):
    """Replays a recording made by RecordingModel, in order."""

    def __init__(self, path: str | Path) -> None:
        data = json.loads(Path(path).read_text())
        steps = []
        for r in data["responses"]:
            steps.append(ModelResponse(
                text=r["text"],
                tool_calls=[ToolCall(**c) for c in r["tool_calls"]],
                usage=Usage(**r["usage"]),
                model=r["model"],
                stop_reason=r["stop_reason"],
                content=r["content"],
            ))
        super().__init__(steps)
        self.name = f"replay:{data['model']}"

    def complete(self, *, system: str, messages: list[dict], tools: list[dict]) -> ModelResponse:
        # Keep the recorded token usage instead of re-estimating it.
        self.requests.append(ModelRequest(system, json.loads(json.dumps(messages, default=str)), tools))
        if not self.steps:
            raise ScriptExhausted("recording has no more responses")
        return self.steps.popleft()
