"""Minimal, protocol-faithful omp JSON transcripts that report model usage.

Shapes follow omp's ``AgentEvent`` and ``Usage`` types: one finalized assistant
``message_end`` per model call, preceded by ``message_start``/``message_update`` streaming
snapshots and repeated in ``turn_end`` and ``agent_end``. Only the ``message_end`` copy is
a distinct report; every other copy exists here to prove it is not counted again.
"""

from __future__ import annotations

import json


def omp_usage(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
    cost: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0),
) -> dict[str, object]:
    """An omp ``Usage`` object whose totals are the reported components' sums."""
    cost_input, cost_output, cost_read, cost_write = cost
    return {
        "input": input_tokens,
        "output": output_tokens,
        "cacheRead": cache_read,
        "cacheWrite": cache_write,
        "totalTokens": input_tokens + output_tokens + cache_read + cache_write,
        "reasoningTokens": output_tokens // 2,
        "cost": {
            "input": cost_input,
            "output": cost_output,
            "cacheRead": cost_read,
            "cacheWrite": cost_write,
            "total": cost_input + cost_output + cost_read + cost_write,
        },
    }


_UNREPORTED = object()
UNREPORTED: object = _UNREPORTED
"""Pass as a call's usage to finalize an assistant message that carries no ``usage``."""


def _assistant(usage: object, *, stop_reason: str = "toolUse") -> dict[str, object]:
    message: dict[str, object] = {
        "role": "assistant",
        "content": [{"type": "text", "text": "working"}],
        "api": "openai-responses",
        "provider": "openai",
        "model": "gpt-5.2",
        "stopReason": stop_reason,
        "timestamp": 1,
    }
    if usage is not _UNREPORTED:
        message["usage"] = usage
    return message


def omp_transcript(*calls: object, terminal: bool = True) -> list[dict[str, object]]:
    """A session with one finalized assistant message per call, plus every duplicate copy."""
    records: list[dict[str, object]] = [
        {"type": "session", "version": 3},
        {"type": "agent_start"},
    ]
    finalized: list[dict[str, object]] = []
    for index, usage in enumerate(calls):
        message = _assistant(usage, stop_reason="stop" if index == len(calls) - 1 else "toolUse")
        partial = {**message, "usage": omp_usage(input_tokens=1, output_tokens=1)}
        tool_result = {
            "role": "toolResult",
            "toolCallId": f"call-{index}",
            "toolName": "bash",
            "content": [{"type": "text", "text": "ok"}],
            "isError": False,
            "timestamp": 2,
        }
        records += [
            {"type": "turn_start"},
            {"type": "message_start", "message": partial},
            {
                "type": "message_update",
                "message": partial,
                "assistantMessageEvent": {"type": "text_delta", "delta": "working"},
            },
            {"type": "message_end", "message": message},
            {"type": "tool_execution_start", "toolCallId": f"call-{index}", "toolName": "bash"},
            {"type": "tool_execution_end", "toolCallId": f"call-{index}", "toolName": "bash"},
            {"type": "message_start", "message": tool_result},
            {"type": "message_end", "message": tool_result},
            {"type": "turn_end", "message": message, "toolResults": [tool_result]},
        ]
        finalized += [message, tool_result]
    if terminal:
        records.append({"type": "agent_end", "messages": finalized})
    return records


def render(records: list[dict[str, object]]) -> str:
    """One JSON event per line, exactly as omp prints in ``--mode=json``."""
    return "\n".join(json.dumps(record) for record in records)


def message_end_lines(text: str) -> list[int]:
    """The 1-based lines of finalized assistant ``message_end`` records, read independently."""
    lines: list[int] = []
    for number, line in enumerate(text.splitlines(), start=1):
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        message = record.get("message") if isinstance(record, dict) else None
        if (
            record.get("type") == "message_end"
            and isinstance(message, dict)
            and message.get("role") == "assistant"
        ):
            lines.append(number)
    return lines
