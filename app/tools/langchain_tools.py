"""
LangChain tool wrappers for the autonomous Traversal Agent.

Wraps existing tools (neo4j_tool, bkg_tool, python_sandbox) as
@tool functions that the ReAct agent can call.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from langchain_core.tools import tool, StructuredTool

from tools.neo4j_tool import neo4j_tool
from tools.bkg_tool import BKGTool
from tools.python_sandbox import execute_python, PythonSandbox


# ─────────────────────────────────────────────
# GROUP BY dimension extraction
# ─────────────────────────────────────────────

_PYTHON_FUNC_KEYS = ("kpi_python_function", "map_python_function")


def _extract_group_by_dimensions(python_function: str) -> dict | None:
    """
    Parse a kpi_python_function / map_python_function string to:
    1. Extract the GROUP BY column list
    2. Replace the GROUP BY line with a placeholder comment

    Returns dict with extracted info, or None if no GROUP BY found.
    """
    if not python_function or not isinstance(python_function, str):
        return None

    pattern = r'(GROUP\s+BY\s+)([\w\s,\.]+?)(\s*(?:\n|"""|\'\'\'|\)|$))'
    match = re.search(pattern, python_function, re.IGNORECASE)
    if not match:
        return None

    raw_columns = match.group(2)
    dimensions = [col.strip() for col in raw_columns.split(",") if col.strip()]
    if not dimensions:
        return None

    placeholder = "-- GROUP BY: <SELECT from available_dimensions based on your query granularity>"
    modified_function = (
        python_function[:match.start()]
        + placeholder
        + python_function[match.end():]
    )

    return {
        "available_dimensions": dimensions,
        "modified_function": modified_function,
    }


def _process_python_functions(result: dict) -> dict:
    """
    For any dict containing kpi_python_function or map_python_function,
    extract GROUP BY dimensions, replace the clause with a placeholder,
    and add a ⚠️_GROUP_BY_DECISION field so the traversal agent picks
    the right granularity instead of blindly copying the full GROUP BY.
    """
    all_dimensions: list[str] = []
    for key in _PYTHON_FUNC_KEYS:
        func_str = result.get(key)
        if not func_str:
            continue
        extracted = _extract_group_by_dimensions(func_str)
        if extracted:
            result[key] = extracted["modified_function"]
            all_dimensions.extend(extracted["available_dimensions"])

    if all_dimensions:
        # Deduplicate while preserving order
        seen = set()
        unique = []
        for d in all_dimensions:
            if d not in seen:
                seen.add(d)
                unique.append(d)
        result["⚠️_GROUP_BY_DECISION"] = {
            "available_dimensions": unique,
            "instruction": (
                "Do NOT use all dimensions. SELECT only the ones your "
                "sub-query needs. See STEP 2a in your system prompt."
            ),
        }
    return result


# ─────────────────────────────────────────────
# Per-tool character limits (context overflow defense)
# ─────────────────────────────────────────────

_TOOL_CHAR_LIMITS = {
    "get_kpi":        50000,
    "get_node":       50000,
    "find_relevant":  6000,
    "traverse_graph": 6000,
    "run_sql_python": 10000,
    "run_python":     10000,
    "run_cypher":     6000,
}


def _truncate_tool_output(tool_name: str, raw_json: str) -> str:
    """
    Truncate a tool's JSON output to fit within the tool's char budget.
    Preserves structure: for list results, keeps first N rows + total count.
    For errors, always returns full output (errors are small and needed for retry).
    """
    limit = _TOOL_CHAR_LIMITS.get(tool_name, 3000)

    if len(raw_json) <= limit:
        return raw_json

    try:
        parsed = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return raw_json[:limit] + '\n... (truncated by tool trimmer)'

    if isinstance(parsed, dict):
        if parsed.get("status") == "error" or "error" in parsed:
            return raw_json

        # run_sql_python / run_python: truncate the 'result' list
        if "result" in parsed and isinstance(parsed["result"], list):
            rows = parsed["result"]
            total = len(rows)
            keep = total
            while keep > 0:
                parsed["result"] = rows[:keep]
                parsed["_truncated"] = {
                    "total_rows": total,
                    "rows_shown": keep,
                    "message": f"Showing {keep} of {total} rows. Use aggregations/GROUP BY to reduce."
                }
                candidate = json.dumps(parsed, default=str)
                if len(candidate) <= limit:
                    return candidate
                keep = keep // 2
            parsed["result"] = []
            parsed["_truncated"] = {"total_rows": total, "rows_shown": 0}
            return json.dumps(parsed, default=str)[:limit]

        # run_cypher: truncate 'records' list
        if "records" in parsed and isinstance(parsed["records"], list):
            rows = parsed["records"]
            total = len(rows)
            keep = total
            while keep > 0:
                parsed["records"] = rows[:keep]
                parsed["count"] = total
                parsed["_truncated"] = f"Showing {keep} of {total} records"
                candidate = json.dumps(parsed, default=str)
                if len(candidate) <= limit:
                    return candidate
                keep = keep // 2

        # get_kpi / get_node / find_relevant: truncate large string fields
        compact = json.dumps(parsed, default=str)
        if len(compact) <= limit:
            return compact
        return compact[:limit] + '\n... (truncated by tool trimmer)'

    return raw_json[:limit] + '\n... (truncated by tool trimmer)'


# Lazy singleton for BKGTool
_bkg: BKGTool | None = None


def _get_bkg() -> BKGTool:
    global _bkg
    if _bkg is None:
        _bkg = BKGTool()
    return _bkg


# ─────────────────────────────────────────────
# Neo4j Tools
# ─────────────────────────────────────────────

@tool
def run_cypher(query: str) -> str:
    """Execute a read-only Cypher query against the Neo4j Business Knowledge Graph.
    Use this for custom queries when the higher-level BKG tools don't cover your needs.
    Returns JSON with 'status', 'records', 'count', and 'elapsed_ms'.
    Only READ operations are allowed — no CREATE, MERGE, DELETE, SET, or REMOVE.
    All nodes use the BKGNode label with node_id property. Relationships use RELATES_TO
    with relationship_type property.
    """
    result = neo4j_tool.run_cypher_safe(query)
    return _truncate_tool_output("run_cypher", json.dumps(result, default=str))


# ─────────────────────────────────────────────
# BKG High-Level Tools
# ─────────────────────────────────────────────

@tool
def get_node(node_id: str) -> str:
    """Fetch a single BKGNode from the Knowledge Graph by its node_id.
    Returns all properties plus incoming and outgoing relationships.
    Supports aliases like 'GC' for general_contractor, 'NAS' for nas_session, etc.
    If the node has a map_python_function, its GROUP BY is replaced with a
    placeholder — check available_dimensions to choose the right granularity.
    Use this when you know the exact node you want to inspect.
    """
    result = _get_bkg().query({"mode": "get_node", "node_id": node_id})
    result = _process_python_functions(result)
    return _truncate_tool_output("get_node", json.dumps(result, default=str))


@tool
def find_relevant(question: str) -> str:
    """Keyword search across all BKGNodes in the Knowledge Graph.
    Searches across node_id, name, label, definition, nl_description, entity_type,
    kpi_name, kpi_description, and kpi_formula_description fields.
    Returns up to 10 nodes ranked by relevance score, with entity_type indicating
    whether each is a core, context, transaction, reference, or kpi node.
    Use this as your FIRST tool when you don't know which nodes to look at.
    """
    result = _get_bkg().query({"mode": "find_relevant", "question": question})
    return _truncate_tool_output("find_relevant", json.dumps(result, default=str))


@tool
def traverse_graph(start: str, depth: int = 2, rel_type: Optional[str] = None) -> str:
    """Walk the Knowledge Graph starting from a BKGNode, following RELATES_TO
    relationships up to a given depth (1-4). Optionally filter by relationship_type
    property on the edges.
    Returns discovered paths and node details (label, entity_type, definition,
    map_table_name, kpi_name).
    Use this to explore the neighborhood of a concept — e.g., to find what tables,
    KPIs, or related entities connect to a starting node.
    """
    req: dict = {"mode": "traverse", "start": start, "depth": depth}
    if rel_type:
        req["rel_type"] = rel_type
    result = _get_bkg().query(req)
    return _truncate_tool_output("traverse_graph", json.dumps(result, default=str))


@tool
def get_kpi(node_id: str) -> str:
    """Get detailed information about a KPI node including its definition,
    formula description, business logic, Python function, source tables/columns,
    dimensions, filters, output schema, and related core nodes.
    The python function's GROUP BY is replaced with a placeholder — check
    available_dimensions to choose the right granularity for your query.
    Use this when you need to understand how a KPI metric is computed or what drives it.
    If called on a non-KPI node, returns KPIs that reference that node.
    """
    result = _get_bkg().query({"mode": "get_kpi", "node_id": node_id})
    result = _process_python_functions(result)
    return _truncate_tool_output("get_kpi", json.dumps(result, default=str))


# ─────────────────────────────────────────────
# Python Sandbox Tools
# ─────────────────────────────────────────────

@tool
def run_python(code: str) -> str:
    """Execute Python code in a sandboxed environment for calculations.
    Available modules: math, json, statistics, collections, datetime, itertools, functools.
    Set a variable named 'result' to return structured data.
    Print statements will be captured as 'output'.
    Use this for arithmetic, aggregations, data transformations, or any computation
    that should not be done in your head.
    """
    result = execute_python(code)
    return _truncate_tool_output("run_python", json.dumps(result, default=str))


@tool
def run_sql_python(code: str, timeout_seconds: int = 30) -> str:
    """Execute Python code with access to a PostgreSQL database connection.
    Pre-imported: conn (psycopg2 read-only), pd (pandas), np (numpy),
    go (plotly.graph_objects), px (plotly.express), json,
    execute_query (helper: execute_query(sql) -> list[dict]).
    Set result = {...} to return data. DataFrames are auto-converted to records.
    Use this when you need to query PostgreSQL for actual operational data
    (as opposed to the Neo4j Knowledge Graph which describes the data model).
    """
    sandbox = PythonSandbox()
    result = sandbox.execute(code, timeout_seconds)
    return _truncate_tool_output("run_sql_python", json.dumps(result, default=str))


# ─────────────────────────────────────────────
# Project-type filter enforcement on SQL tool
# ─────────────────────────────────────────────

_MACRO_TABLE = "stg_ndpd_mbt_tmobile_macro_combined"
_BOTH_SMP_VALUES = ("NTM", "AHLOB Modernization")


def _check_macro_combined_filter(code: str, project_type: str) -> str | None:
    """
    If code references the macro_combined table, verify that the correct
    smp_name filter is present. Returns an error message if missing, else None.
    """
    if _MACRO_TABLE not in code:
        return None

    if project_type == "Both":
        # Must have IN clause with both values
        has_in = re.search(
            r"smp_name\s+IN\s*\(",
            code,
            re.IGNORECASE,
        )
        has_both = (
            _BOTH_SMP_VALUES[0] in code and _BOTH_SMP_VALUES[1] in code
        )
        if has_in and has_both:
            return None
        return (
            f"ERROR: Your SQL references `{_MACRO_TABLE}` but is missing the "
            f"mandatory filter: smp_name IN ('{_BOTH_SMP_VALUES[0]}', '{_BOTH_SMP_VALUES[1]}'). "
            f"Add this WHERE/AND condition and retry."
        )

    # Single project type
    pattern = re.compile(
        rf"smp_name\s*=\s*'{re.escape(project_type)}'",
        re.IGNORECASE,
    )
    if pattern.search(code):
        return None
    return (
        f"ERROR: Your SQL references `{_MACRO_TABLE}` but is missing the "
        f"mandatory filter: smp_name = '{project_type}'. "
        f"Add this WHERE/AND condition and retry."
    )


def _make_filtered_run_sql_python(project_type: str) -> StructuredTool:
    """Create a run_sql_python tool that validates smp_name filter before execution."""

    @tool
    def run_sql_python_filtered(code: str, timeout_seconds: int = 30) -> str:
        """Execute Python code with access to a PostgreSQL database connection.
        Pre-imported: conn (psycopg2 read-only), pd (pandas), np (numpy),
        go (plotly.graph_objects), px (plotly.express), json,
        execute_query (helper: execute_query(sql) -> list[dict]).
        Set result = {...} to return data. DataFrames are auto-converted to records.
        Use this when you need to query PostgreSQL for actual operational data
        (as opposed to the Neo4j Knowledge Graph which describes the data model).
        """
        # Validate filter before execution
        err = _check_macro_combined_filter(code, project_type)
        if err:
            return json.dumps({"status": "error", "error": err})

        sandbox = PythonSandbox()
        result = sandbox.execute(code, timeout_seconds)
        return _truncate_tool_output("run_sql_python", json.dumps(result, default=str))

    # Keep the tool name as run_sql_python so the agent sees the same name
    run_sql_python_filtered.name = "run_sql_python"
    return run_sql_python_filtered


# ─────────────────────────────────────────────
# Tool registry
# ─────────────────────────────────────────────

def get_all_tools(project_type: str = "") -> list:
    """Return all tools for the traversal agent.
    When project_type is set, run_sql_python gets filter validation."""
    sql_tool = (
        _make_filtered_run_sql_python(project_type)
        if project_type
        else run_sql_python
    )
    return [
        run_cypher,
        get_node,
        find_relevant,
        traverse_graph,
        get_kpi,
        run_python,
        sql_tool,
    ]


def get_fast_tools(project_type: str = "") -> list:
    """Minimal tool set for the traversal agent.

    Removes find_relevant (schema is in prompt), traverse_graph (never needed
    when schema is embedded), and run_cypher (run_sql_python covers all cases).
    Smaller decision surface = faster LLM reasoning per round trip.
    """
    sql_tool = (
        _make_filtered_run_sql_python(project_type)
        if project_type
        else run_sql_python
    )
    return [
        get_kpi,
        get_node,
        sql_tool,
        run_python,
    ]


def get_analysis_tools() -> list:
    """Return tools for the analysis agent (python sandbox only)."""
    return [
        run_python,
    ]
