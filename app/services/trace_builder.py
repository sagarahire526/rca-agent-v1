"""
Trace Builder — constructs the execution trace JSON for persistent storage.

Captures which nodes ran, what tool calls happened at each step,
and overall metrics. Stored as JSONB in rca_agent_queries.traces.
"""
from __future__ import annotations

from typing import Any

_MAX_TOOL_OUTPUT_LEN = 2000


def _truncate_tool_output(output: Any) -> str:
    """Cap tool_output to _MAX_TOOL_OUTPUT_LEN chars for DB storage."""
    s = str(output) if not isinstance(output, str) else output
    if len(s) > _MAX_TOOL_OUTPUT_LEN:
        return s[:_MAX_TOOL_OUTPUT_LEN] + "...[truncated]"
    return s


def _sanitize_tool_calls(tool_calls: list[dict]) -> list[dict]:
    """Return tool call records with truncated outputs."""
    return [
        {**tc, "tool_output": _truncate_tool_output(tc.get("tool_output", ""))}
        for tc in tool_calls
    ]


def build_traces(
    final_state: dict,
    nodes_executed: list[str],
    total_duration_ms: float,
) -> dict:
    """
    Build the traces dict from the completed graph state.

    Returns:
        {
            "nodes_executed": [...],
            "steps": [{"step": "...", "tool_calls": [...]}],
            "total_tool_calls": int,
            "total_execution_time_ms": float,
        }
    """
    routing = final_state.get("routing_decision", "")
    steps: list[dict] = []

    if routing == "rca":
        # Planner path: one step per planner sub-query
        planner_steps = final_state.get("planner_steps", [])
        planner_results = final_state.get("planner_step_results", [])

        for i, label in enumerate(planner_steps):
            result = planner_results[i] if i < len(planner_results) else {}
            tool_calls = _sanitize_tool_calls(
                result.get("traversal_tool_calls", [])
            )
            steps.append({"step": label, "tool_calls": tool_calls})

    elif routing == "traversal":
        # Direct traversal: single step
        query = (
            final_state.get("refined_query")
            or final_state.get("user_query", "")
        )
        tool_calls = _sanitize_tool_calls(
            final_state.get("traversal_tool_calls", [])
        )
        steps.append({"step": query, "tool_calls": tool_calls})

    # Greeting path: steps stays empty

    total_tool_calls = sum(len(s["tool_calls"]) for s in steps)

    return {
        "nodes_executed": nodes_executed,
        "project_type": final_state.get("project_type", ""),
        "steps": steps,
        "total_tool_calls": total_tool_calls,
        "total_execution_time_ms": total_duration_ms,
    }
