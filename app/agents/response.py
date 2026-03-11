"""
Response Agent — Interprets traversal findings, performs calculations
via Python sandbox, and generates a PM-readable RCA report.

Handles two upstream paths:
  - Direct traversal path: reads traversal_findings + traversal_tool_calls
  - Planner path: reads planner_steps + planner_step_results (N parallel traversals)
"""
from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import SystemMessage, HumanMessage

from models.state import RCAState
from services.llm_provider import LLMProvider
from tools.python_sandbox import execute_python
from prompts.response_prompt import RESPONSE_SYSTEM


logger = logging.getLogger(__name__)


def _format_traversal_data(state: RCAState) -> tuple[str, list]:
    """
    Format traversal findings for the response LLM.
    Returns (formatted_context_string, effective_tool_calls_list).
    """
    planner_steps = state.get("planner_steps", [])
    planner_results = state.get("planner_step_results", [])

    # ── Planner path ──
    if planner_steps and planner_results:
        lines = [f"## Investigation Execution — {len(planner_steps)} Parallel Steps\n"]
        all_tool_calls: list = []

        for idx, (step, result) in enumerate(zip(planner_steps, planner_results), 1):
            findings = result.get("traversal_findings", "No findings.")
            tool_calls = result.get("traversal_tool_calls", [])
            steps_taken = result.get("traversal_steps_taken", 0)
            step_errors = result.get("errors", [])

            lines.append(f"### Step {idx}: {step}")
            lines.append(f"*Tool calls: {steps_taken}*\n")
            lines.append(findings)

            if step_errors:
                lines.append("\n*Errors in this step:*")
                for err in step_errors:
                    lines.append(f"- {err}")
            lines.append("")
            all_tool_calls.extend(tool_calls)

        if all_tool_calls:
            lines.append(f"\n## Tool Call Summary ({len(all_tool_calls)} calls)\n")
            for i, tc in enumerate(all_tool_calls, 1):
                status_icon = "OK" if tc["status"] == "success" else "ERR"
                lines.append(f"- {tc['tool_name']} [{status_icon}]: {_compact_output(tc['tool_output'])}")

        return "\n".join(lines), all_tool_calls

    # ── Direct traversal path ──
    lines = ["## Traversal Agent Findings\n"]

    findings = state.get("traversal_findings", "")
    lines.append(findings if findings else "No findings were recorded by the traversal agent.")

    tool_calls = state.get("traversal_tool_calls", [])
    if tool_calls:
        lines.append(f"\n## Tool Call Summary ({len(tool_calls)} calls)\n")
        for i, tc in enumerate(tool_calls, 1):
            status_icon = "OK" if tc["status"] == "success" else "ERR"
            lines.append(f"- {tc['tool_name']} [{status_icon}]: {_compact_output(tc['tool_output'])}")

    return "\n".join(lines), tool_calls


def _compact_output(raw: str, max_len: int = 200) -> str:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            if "records" in parsed:
                return f"{parsed.get('count', len(parsed['records']))} records"
            if "error" in parsed:
                return f"Error: {str(parsed['error'])[:120]}"
            if "relevant_nodes" in parsed:
                return f"{len(parsed['relevant_nodes'])} nodes, {len(parsed.get('relevant_metrics', []))} metrics"
            if "paths" in parsed:
                return f"{len(parsed['paths'])} paths"
            if "status" in parsed and parsed["status"] == "success":
                return f"OK — {str(parsed.get('result', parsed.get('output', '')))[:150]}"
    except (json.JSONDecodeError, TypeError):
        pass
    text = str(raw)
    return text[:max_len] + "..." if len(text) > max_len else text


def response_node(state: RCAState) -> dict[str, Any]:
    """
    LangGraph node: Response Agent for RCA.

    Reads: refined_query (or user_query), traversal/planner data, errors
    Writes: final_response, calculations, data_summary, current_phase, messages
    """
    provider = LLMProvider(model="gpt-4o", temperature=0.1)
    llm = provider.get_llm()

    user_query = state.get("refined_query") or state["user_query"]

    data_context, effective_tool_calls = _format_traversal_data(state)
    errors = state.get("errors", [])

    user_message_parts = [
        f"## Original User Query\n{user_query}",
        f"\n{data_context}",
    ]

    if errors:
        user_message_parts.append(
            "\n## Errors Encountered\n" +
            "\n".join(f"- {e}" for e in errors)
        )

    rca_guidance = state.get("rca_scenario_guidance", "").strip()
    if rca_guidance:
        user_message_parts.append(f"\n{rca_guidance}")

    user_message_parts.append(
        "\n## Instructions"
        "\nAnalyze the collected investigation data above and generate a comprehensive, "
        "PM-readable RCA report. Use the RCA Guidance above (if provided) "
        "as a reference for structuring your analysis — "
        "adapt it to what was actually retrieved. Use Python sandbox for any "
        "calculations — write the code and I will execute it. Include specific "
        "numbers from the data. If data is missing or queries failed, acknowledge "
        "it explicitly. Focus on identifying ROOT CAUSES backed by data evidence "
        "and providing ACTIONABLE recommendations with specific targets."
    )

    user_message = "\n".join(user_message_parts)

    response = llm.invoke([
        SystemMessage(content=RESPONSE_SYSTEM),
        HumanMessage(content=user_message),
    ])

    final_response = response.content

    # Execute any Python calculation blocks embedded in the response
    calculations_output = ""
    if "```python" in final_response:
        code_blocks = final_response.split("```python")
        for block in code_blocks[1:]:
            code = block.split("```")[0].strip()
            if not code:
                continue
            exec_context = {}
            for i, tc in enumerate(effective_tool_calls):
                if tc["status"] == "success" and tc["tool_output"]:
                    try:
                        parsed = json.loads(tc["tool_output"])
                        exec_context[f"call_{i}_{tc['tool_name']}"] = parsed
                    except (json.JSONDecodeError, TypeError):
                        exec_context[f"call_{i}_{tc['tool_name']}"] = tc["tool_output"]

            calc_result = execute_python(code, exec_context)
            if calc_result["status"] == "success":
                calculations_output += (
                    f"Calculation:\n{code}\n"
                    f"Output: {calc_result.get('output', '')}\n"
                    f"Result: {calc_result.get('result')}\n\n"
                )

    # Build data summary from all successful tool calls
    data_summary: dict[str, Any] = {}
    for i, tc in enumerate(effective_tool_calls):
        if tc["status"] == "success" and tc["tool_output"]:
            try:
                data_summary[f"call_{i}_{tc['tool_name']}"] = json.loads(tc["tool_output"])
            except (json.JSONDecodeError, TypeError):
                data_summary[f"call_{i}_{tc['tool_name']}"] = tc["tool_output"]

    logger.info("Response agent generated RCA report")

    return {
        "final_response": final_response,
        "calculations": calculations_output,
        "data_summary": data_summary,
        "current_phase": "complete",
        "messages": [{"agent": "response", "content": "Generated RCA report"}],
    }
