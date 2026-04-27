"""
Analysis Agent (formerly Response Agent) — ReAct agent that analyzes
traversal findings, performs data-backed calculations via Python sandbox,
and generates a PM-readable RCA report.

Handles two upstream paths:
  - Direct traversal path: reads traversal_findings + traversal_tool_calls
  - Planner path: reads planner_steps + planner_step_results (N parallel traversals)
"""
from __future__ import annotations

import json
import threading
import time
import logging
from typing import Any

from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.prebuilt import create_react_agent

from models.state import RCAState
from services.llm_provider import LLMProvider
from tools.langchain_tools import get_analysis_tools
from prompts.response_prompt import RESPONSE_SYSTEM
from prompts.algorithm_prompt import ALGORITHM_SYSTEM


logger = logging.getLogger(__name__)

MAX_TOOL_CALLS = 3


def _unwrap_string_encoded_json(value: Any) -> Any:
    """
    Recursively walk a value and convert any string that is itself a JSON-encoded
    object/array into the real parsed structure. Ensures the payload we send to
    the response agent is valid JSON — never JSON enclosed in a string.
    """
    if isinstance(value, dict):
        return {k: _unwrap_string_encoded_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap_string_encoded_json(item) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")) and stripped.endswith(("}", "]")):
            try:
                parsed = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                return value
            if isinstance(parsed, (dict, list)):
                return _unwrap_string_encoded_json(parsed)
    return value


def _build_analysis_json(semantic_data: dict[str, Any]) -> str:
    """
    Build the {"analysis": ...} JSON payload for the response agent.

    Unwraps any nested string-encoded JSON, then round-trips through
    json.loads(json.dumps(...)) to verify the result is valid JSON.
    Raises ValueError if validation fails.
    """
    cleaned = _unwrap_string_encoded_json(semantic_data or {})
    payload = {"analysis": cleaned}
    serialized = json.dumps(payload, ensure_ascii=False, default=str, indent=2)
    json.loads(serialized)  # round-trip validation
    return serialized

# ── ANSI colors for terminal output ──
_CYAN = "\033[96m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _format_traversal_data(state: RCAState) -> str:
    """
    Format traversal findings into a context string for the analysis agent.
    """
    planner_steps = state.get("planner_steps", [])
    planner_results = state.get("planner_step_results", [])

    # ── Planner path ──
    if planner_steps and planner_results:
        lines = [f"## Investigation Execution — {len(planner_steps)} Parallel Steps\n"]

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

            # Include raw successful tool outputs for the analysis agent to parse
            successful_data = []
            for tc in tool_calls:
                if tc["status"] == "success" and tc["tool_output"]:
                    successful_data.append({
                        "tool": tc["tool_name"],
                        "input": tc["tool_input"],
                        "output": tc["tool_output"],
                    })

            if successful_data:
                lines.append(f"\n**Raw Data from Step {idx}** ({len(successful_data)} successful calls):")
                for sd in successful_data:
                    lines.append(f"\n`{sd['tool']}` result:")
                    output = sd["output"]
                    # Truncate very large outputs but keep enough for analysis
                    if len(str(output)) > 8000:
                        output = str(output)[:8000] + "\n... (truncated)"
                    lines.append(f"```json\n{output}\n```")

            lines.append("")

        return "\n".join(lines)

    # ── Direct traversal path ──
    lines = ["## Traversal Agent Findings\n"]

    findings = state.get("traversal_findings", "")
    lines.append(findings if findings else "No findings were recorded by the traversal agent.")

    tool_calls = state.get("traversal_tool_calls", [])
    if tool_calls:
        successful_data = []
        for tc in tool_calls:
            if tc["status"] == "success" and tc["tool_output"]:
                successful_data.append({
                    "tool": tc["tool_name"],
                    "input": tc["tool_input"],
                    "output": tc["tool_output"],
                })

        if successful_data:
            lines.append(f"\n**Raw Data** ({len(successful_data)} successful calls):")
            for sd in successful_data:
                lines.append(f"\n`{sd['tool']}` result:")
                output = sd["output"]
                if len(str(output)) > 8000:
                    output = str(output)[:8000] + "\n... (truncated)"
                lines.append(f"```json\n{output}\n```")

    return "\n".join(lines)


def _generate_algorithm(llm, user_query: str, data_context: str) -> str:
    """
    Ask a fast-tier LLM to turn the agent's tool trace into a numbered,
    plain-English algorithm narrative. Runs in parallel with the main response
    LLM. Returns "" on any failure — never blocks the main response.
    """
    try:
        resp = llm.invoke([
            SystemMessage(content=ALGORITHM_SYSTEM),
            HumanMessage(content=(
                f"## User Query\n{user_query}\n\n"
                f"## Tool Trace\n{data_context}\n\n"
                "Write the numbered algorithm now."
            )),
        ])
        return (resp.content or "").strip()
    except Exception as exc:
        logger.warning("Algorithm generation failed: %s", exc)
        return ""


def _print_divider(char: str = "-", width: int = 70):
    print(f"{_DIM}{char * width}{_RESET}")


def _print_tool_call(step_num: int, tool_name: str, tool_input: dict):
    _print_divider()
    print(f"{_BOLD}{_CYAN}  Analysis Step {step_num}: {tool_name}{_RESET}")
    for key, val in tool_input.items():
        val_str = str(val)
        if key == "code":
            print(f"     {_DIM}{key}:{_RESET}")
            for line in val_str.splitlines():
                print(f"       {_DIM}{line}{_RESET}")
        else:
            if len(val_str) > 200:
                val_str = val_str[:200] + "..."
            print(f"     {_DIM}{key}:{_RESET} {val_str}")


def _print_tool_result(status: str, output: str):
    if status == "error":
        icon, color = "X", _RED
    else:
        icon, color = "OK", _GREEN

    display = output
    try:
        parsed = json.loads(output)
        if isinstance(parsed, dict):
            if "error" in parsed:
                display = f"Error: {parsed['error']}"
                status = "error"
            elif "status" in parsed and parsed["status"] == "success":
                result_val = parsed.get("result", parsed.get("output", ""))
                display = f"Success: {json.dumps(result_val, default=str)[:500]}"
            else:
                display = json.dumps(parsed, indent=2, default=str)
                if len(display) > 1000:
                    display = display[:1000] + "\n     ...(truncated)"
        else:
            display = str(parsed)
            if len(display) > 1000:
                display = display[:1000] + "...(truncated)"
    except (json.JSONDecodeError, TypeError):
        if len(display) > 1000:
            display = display[:1000] + "...(truncated)"

    color_out = _RED if status == "error" else _GREEN
    print(f"     {color_out}{icon} Result:{_RESET} {display}")


def _print_agent_thinking(content: str):
    if not content.strip():
        return
    text = content.strip()
    if len(text) > 500:
        text = text[:500] + "..."
    print(f"  {_YELLOW}Analysis:{_RESET} {text}")


def response_node(state: RCAState) -> dict[str, Any]:
    """
    LangGraph node: Analysis Agent (ReAct) for RCA.

    Uses run_python tool to perform calculations on traversal data,
    then generates a comprehensive, data-backed RCA report.

    Streams agent execution so every thinking step, tool call, and tool
    result is logged to the terminal in real-time.

    Reads: refined_query (or user_query), traversal/planner data, errors
    Writes: final_response, calculations, data_summary, current_phase, messages
    """
    provider = LLMProvider(model="gpt-4.1-mini", temperature=0.1)
    llm = provider.get_llm()

    user_query = state.get("refined_query") or state["user_query"]
    data_context = _format_traversal_data(state)
    errors = state.get("errors", [])

    # Start algorithm narrative generation in parallel with the main response agent.
    # Costs no extra wall-clock time — joined just before we return.
    algorithm_result: dict[str, str] = {"value": ""}

    def _algorithm_worker() -> None:
        fast_llm = LLMProvider(model="gpt-5.4-mini", temperature=0.1).get_llm()
        algorithm_result["value"] = _generate_algorithm(
            fast_llm, user_query, data_context,
        )

    algorithm_thread = threading.Thread(target=_algorithm_worker, daemon=True)
    algorithm_thread.start()

    # Build the human message with all context
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

    semantic_data = state.get("semantic_context_data") or {}
    if isinstance(semantic_data, dict) and any(semantic_data.values()):
        try:
            analysis_json = _build_analysis_json(semantic_data)
            user_message_parts.append(
                "\n## Analysis (Semantic Context — Valid JSON)\n"
                "```json\n"
                f"{analysis_json}\n"
                "```"
            )
        except (ValueError, TypeError) as exc:
            logger.warning("Skipping analysis payload — JSON validation failed: %s", exc)

    user_message_parts.append(
        "\n## Instructions"
        "\nAnalyze the collected investigation data above. Most numbers you need "
        "are already in the traversal data — use them directly. Only use the "
        "run_python tool when you need to derive a non-obvious metric "
        "(e.g., correlations, trend slopes, weighted averages) that isn't already "
        "present in the data. Limit yourself to at most 2 run_python calls. "
        "After analysis, generate a concise, PM-readable RCA report with "
        "data-backed root causes and actionable recommendations."
    )

    human_message = "\n".join(user_message_parts)

    # Build the ReAct agent with python sandbox tool
    tools = get_analysis_tools()
    agent = create_react_agent(
        model=llm,
        tools=tools,
        prompt=RESPONSE_SYSTEM,
    )

    # ── Live-streaming execution ──
    print(f"\n{_BOLD}{'=' * 70}")
    print(f"  ANALYSIS AGENT — Generating Data-Backed RCA Report")
    print(f"{'=' * 70}{_RESET}")
    print(f"  {_DIM}Query: {user_query[:80]}{_RESET}")
    print(f"  {_DIM}Max tool calls: {MAX_TOOL_CALLS}{_RESET}\n")

    start_time = time.perf_counter()
    step_num = 0
    final_response = ""
    limit_hit = False
    tool_results: list[str] = []

    try:
        for chunk in agent.stream(
            {"messages": [("human", human_message)]},
            config={"recursion_limit": MAX_TOOL_CALLS * 3 + 10},
            stream_mode="updates",
        ):
            for node_name, node_output in chunk.items():
                messages = node_output.get("messages", [])

                for msg in messages:
                    # ── AI message: thinking or tool call ──
                    if msg.type == "ai":
                        text = getattr(msg, "content", "") or ""

                        # Agent reasoning (no tool call attached)
                        if text.strip() and not getattr(msg, "tool_calls", None):
                            _print_agent_thinking(text)
                            final_response = text

                        # Tool calls the agent wants to make
                        if hasattr(msg, "tool_calls") and msg.tool_calls:
                            for tc in msg.tool_calls:
                                step_num += 1
                                _print_tool_call(step_num, tc["name"], tc["args"])

                    # ── Tool result message ──
                    elif msg.type == "tool":
                        output = msg.content or ""
                        status = "error" if "error" in output.lower()[:200] else "success"
                        _print_tool_result(status, output)
                        tool_results.append(output)

            # ── Hard stop after MAX_TOOL_CALLS ──
            if step_num >= MAX_TOOL_CALLS:
                print(f"\n  {_YELLOW}Tool call limit reached ({MAX_TOOL_CALLS}). "
                      f"Finalizing report with data collected so far.{_RESET}")
                limit_hit = True
                break

        # ── Fallback: generate report if agent didn't produce one ──
        if not final_response.strip():
            print(f"  {_YELLOW}No final response from agent — generating via direct LLM call.{_RESET}")
            tool_context = ""
            if tool_results:
                tool_context = (
                    "\n\n## Computation Results\n"
                    + "\n\n".join(tool_results)
                )
            fallback_response = llm.invoke([
                SystemMessage(content=RESPONSE_SYSTEM),
                HumanMessage(content=human_message + tool_context
                             + "\n\nGenerate the final report now. Do NOT call any tools."),
            ])
            final_response = fallback_response.content

        elapsed = time.perf_counter() - start_time

        _print_divider("=")
        suffix = " (limit reached)" if limit_hit else ""
        print(f"  {_BOLD}Analysis complete: {step_num} calculations performed in {elapsed:.1f}s{suffix}{_RESET}")
        _print_divider("=")
        print()

        logger.info(
            "Analysis agent completed: %d calculations in %.1fs%s",
            step_num, elapsed, suffix,
        )

        algorithm_thread.join(timeout=30)
        execution_algorithm = algorithm_result["value"]

        return {
            "final_response": final_response,
            "execution_algorithm": execution_algorithm,
            "calculations": f"{step_num} python calculations executed",
            "data_summary": {},
            "current_phase": "complete",
            "messages": [{
                "agent": "analysis",
                "content": (
                    f"Analysis complete: {step_num} calculations, "
                    f"{elapsed:.1f}s elapsed"
                ),
            }],
        }

    except Exception as e:
        elapsed = time.perf_counter() - start_time
        print(f"\n  {_RED}Analysis failed after {elapsed:.1f}s: {e}{_RESET}\n")
        logger.error("Analysis agent failed: %s", e)
        algorithm_thread.join(timeout=5)
        return {
            "final_response": f"Analysis failed: {e}",
            "execution_algorithm": algorithm_result["value"],
            "calculations": "",
            "data_summary": {},
            "current_phase": "complete",
            "errors": [f"Analysis agent error: {e}"],
            "messages": [{
                "agent": "analysis",
                "content": f"Analysis failed after {elapsed:.1f}s: {e}",
            }],
        }
