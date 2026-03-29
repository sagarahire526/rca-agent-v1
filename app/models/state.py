"""
Shared state models for the LangGraph RCA (Root Cause Analysis) agent system.
All agents read/write to this shared state as it flows through the graph.
"""
from __future__ import annotations

import operator
from typing import Any, Literal, Optional, TypedDict, Annotated


# ─────────────────────────────────────────────
# Traversal Agent output types
# ─────────────────────────────────────────────

class ToolCallRecord(TypedDict):
    """Record of a single tool invocation by the traversal agent."""
    tool_name: str
    tool_input: dict[str, Any]
    tool_output: Any
    status: Literal["success", "error"]
    execution_time_ms: float


# ─────────────────────────────────────────────
# Main Graph State  (shared across all nodes)
# ─────────────────────────────────────────────

class RCAState(TypedDict):
    """
    The shared state that flows through the LangGraph.
    Uses Annotated + operator.add for list fields so that
    each node *appends* rather than overwrites.
    """
    # ── Input ──
    user_query: str
    refined_query: str

    # ── Phase tracking ──
    current_phase: Literal[
        "query_refinement", "orchestration", "discovery",
        "planning", "traversal", "response", "complete", "error"
    ]

    # ── Project type filter ──
    project_type: str            # "NTM" | "AHLOB Modernization" | ""

    # ── Orchestrator routing ──
    routing_decision: str        # "greeting" | "rca" | "traversal"
    routing_context: str         # For greeting: direct response text

    # ── Planner Agent ──
    planning_rationale: str
    planner_steps: list[str]
    planner_step_results: Annotated[list[dict], operator.add]

    # ── Knowledge Graph Schema (discovered once) ──
    kg_schema: str

    # ── Pre-fetched semantic context ──
    planner_semantic_context: str

    # ── Traversal Agent ──
    traversal_findings: str
    traversal_tool_calls: Annotated[list[ToolCallRecord], operator.add]
    traversal_steps_taken: int
    max_traversal_steps: int  # Safety ceiling (default 20)

    # ── RCA Scenario Guidance ──
    rca_scenario_guidance: str

    # ── Response Agent ──
    final_response: str
    calculations: str
    data_summary: dict[str, Any]

    # ── Error handling ──
    errors: Annotated[list[str], operator.add]

    # ── Metadata ──
    created_at: str
    messages: Annotated[list[dict], operator.add]
