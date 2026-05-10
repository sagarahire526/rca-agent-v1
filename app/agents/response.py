"""
Analysis Agent (formerly Response Agent) — analyzes traversal findings and
generates a PM-readable RCA report via a single direct LLM call.

No tools are bound: all numeric work must come from pre-computed traversal
aggregates in the input data.

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

from models.state import RCAState
from services.llm_provider import LLMProvider
from prompts.response_prompt import RESPONSE_SYSTEM
from prompts.algorithm_prompt import ALGORITHM_SYSTEM
from prompts.chart_prompt import CHART_SYSTEM


logger = logging.getLogger(__name__)


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


def _generate_charts(llm, user_query: str, data_context: str) -> dict[str, Any]:
    """
    Ask a fast-tier LLM to produce Highcharts-compatible chart specs from the
    traversal data. Runs in parallel with the main response LLM. Returns
    {"charts": [], "rationale": ""} on any failure — never blocks the main
    response. The full {charts, rationale} payload is stored as-is so the
    /chart/{query_id} endpoint can serve it directly to the frontend.
    """
    empty: dict[str, Any] = {"charts": [], "rationale": ""}
    raw = ""
    try:
        resp = llm.invoke([
            SystemMessage(content=CHART_SYSTEM),
            HumanMessage(content=(
                f"## User Query\n{user_query}\n\n"
                f"## Traversal Data\n{data_context}\n\n"
                "Generate Highcharts specs now. Return ONLY JSON."
            )),
        ])
        raw = (resp.content or "").strip()
        if raw.startswith("```"):
            # Strip an accidental ```json ... ``` fence if the model emits one
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            logger.warning("Chart LLM returned non-dict JSON: %.200s", raw)
            return empty
        charts = parsed.get("charts", [])
        rationale = parsed.get("rationale", "")
        return {
            "charts":    charts if isinstance(charts, list) else [],
            "rationale": rationale if isinstance(rationale, str) else "",
        }
    except json.JSONDecodeError as exc:
        logger.warning("Chart JSON parse failed: %s | raw=%.500s", exc, raw)
        return empty
    except Exception as exc:
        logger.warning("Chart generation failed: %s", exc)
        return empty


def _print_divider(char: str = "-", width: int = 70):
    print(f"{_DIM}{char * width}{_RESET}")


def response_node(state: RCAState) -> dict[str, Any]:
    """
    LangGraph node: Analysis Agent for RCA.

    Generates a PM-readable RCA report via a single direct LLM call.
    No tools are bound — all numbers must come from pre-computed
    aggregates in the traversal data.

    Reads: refined_query (or user_query), traversal/planner data, errors
    Writes: final_response, calculations, data_summary, current_phase, messages
    """
    provider = LLMProvider(model="gpt-5-mini", temperature=0.1, reasoning_effort="medium")
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

    # Start Highcharts spec generation in parallel as well — same fast tier,
    # low reasoning effort. Joined just before we return.
    chart_result: dict[str, dict[str, Any]] = {
        "value": {"charts": [], "rationale": ""},
    }

    def _chart_worker() -> None:
        try:
            fast_llm = LLMProvider(
                model="gpt-5.4-mini", temperature=0.1, reasoning_effort="low",
            ).get_llm()
            chart_result["value"] = _generate_charts(
                fast_llm, user_query, data_context,
            )
        except Exception as exc:
            logger.warning("Chart worker setup failed: %s", exc)

    chart_thread = threading.Thread(target=_chart_worker, daemon=True)
    chart_thread.start()

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
        "\nAnalyze the collected investigation data above. All numbers you need "
        "must come from the traversal data — use the pre-computed aggregates "
        "directly. Do NOT invent numbers and do NOT attempt any computation "
        "outside what is already present in the data. "
        "Generate a concise, PM-readable RCA report with data-backed root "
        "causes and **quantified** recommendations. Every recommendation row "
        "MUST include a numeric Current → Projected delta on a named metric — "
        "never plain-English-only suggestions. If you cannot derive a numeric "
        "projection for an action from the data, drop that recommendation."
    )

    human_message = "\n".join(user_message_parts)

    # ── Direct LLM call (no tools) ──
    print(f"\n{_BOLD}{'=' * 70}")
    print(f"  ANALYSIS AGENT — Generating Data-Backed RCA Report")
    print(f"{'=' * 70}{_RESET}")
    print(f"  {_DIM}Query: {user_query[:80]}{_RESET}\n")

    start_time = time.perf_counter()
    final_response = ""

    try:
        response_msg = llm.invoke([
            SystemMessage(content=RESPONSE_SYSTEM),
            HumanMessage(content=human_message),
        ])
        final_response = response_msg.content or ""

        elapsed = time.perf_counter() - start_time

        _print_divider("=")
        print(f"  {_BOLD}Analysis complete in {elapsed:.1f}s{_RESET}")
        _print_divider("=")
        print()

        logger.info("Analysis agent completed in %.1fs", elapsed)

        algorithm_thread.join(timeout=30)
        execution_algorithm = algorithm_result["value"]
        chart_thread.join(timeout=30)
        generated_charts = chart_result["value"]

        return {
            "final_response": final_response,
            "execution_algorithm": execution_algorithm,
            "generated_charts": generated_charts,
            "calculations": "",
            "data_summary": {},
            "current_phase": "complete",
            "messages": [{
                "agent": "analysis",
                "content": f"Analysis complete in {elapsed:.1f}s",
            }],
        }

    except Exception as e:
        elapsed = time.perf_counter() - start_time
        print(f"\n  {_RED}Analysis failed after {elapsed:.1f}s: {e}{_RESET}\n")
        logger.error("Analysis agent failed: %s", e)
        algorithm_thread.join(timeout=5)
        chart_thread.join(timeout=5)
        return {
            "final_response": f"Analysis failed: {e}",
            "execution_algorithm": algorithm_result["value"],
            "generated_charts": chart_result["value"],
            "calculations": "",
            "data_summary": {},
            "current_phase": "complete",
            "errors": [f"Analysis agent error: {e}"],
            "messages": [{
                "agent": "analysis",
                "content": f"Analysis failed after {elapsed:.1f}s: {e}",
            }],
        }
