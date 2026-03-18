"""
LangChain tool wrappers for the autonomous Traversal Agent.

Wraps existing tools (neo4j_tool, bkg_tool, python_sandbox) as
@tool functions that the ReAct agent can call.
"""
from __future__ import annotations

import json
from typing import Optional

from langchain_core.tools import tool

from tools.neo4j_tool import neo4j_tool
from tools.bkg_tool import BKGTool
from tools.python_sandbox import execute_python, PythonSandbox


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
    return json.dumps(result, default=str)


# ─────────────────────────────────────────────
# BKG High-Level Tools
# ─────────────────────────────────────────────

@tool
def get_node(node_id: str) -> str:
    """Fetch a single BKGNode from the Knowledge Graph by its node_id.
    Returns all properties plus incoming and outgoing relationships.
    Supports aliases like 'GC' for general_contractor, 'NAS' for nas_session, etc.
    Large properties (map_python_function, map_contract, kpi_python_function, etc.)
    are excluded — use get_kpi to fetch KPI details explicitly.
    Use this when you know the exact node you want to inspect.
    """
    result = _get_bkg().query({"mode": "get_node", "node_id": node_id})
    return json.dumps(result, default=str)


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
    return json.dumps(result, default=str)


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
    return json.dumps(result, default=str)


@tool
def get_kpi(node_id: str) -> str:
    """Get detailed information about a KPI node including its definition,
    formula description, business logic, Python function, source tables/columns,
    dimensions, filters, output schema, and related core nodes.
    Use this when you need to understand how a KPI metric is computed or what drives it.
    If called on a non-KPI node, returns KPIs that reference that node.
    """
    result = _get_bkg().query({"mode": "get_kpi", "node_id": node_id})
    return json.dumps(result, default=str)


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
    return json.dumps(result, default=str)


@tool
def run_sql_python(code: str, timeout_seconds: int = 30) -> str:
    """Execute Python code with access to a PostgreSQL database connection.
    Pre-imported: conn (psycopg2 read-only), pd (pandas), np (numpy),
    go (plotly.graph_objects), px (plotly.express), json.
    Set result = {...} to return data. DataFrames are auto-converted to records.
    Use this when you need to query PostgreSQL for actual operational data
    (as opposed to the Neo4j Knowledge Graph which describes the data model).
    """
    sandbox = PythonSandbox()
    result = sandbox.execute(code, timeout_seconds)
    return json.dumps(result, default=str)


# ─────────────────────────────────────────────
# Tool registry
# ─────────────────────────────────────────────

def get_all_tools() -> list:
    """Return all tools for the traversal agent."""
    return [
        run_cypher,
        get_node,
        find_relevant,
        traverse_graph,
        get_kpi,
        run_python,
        run_sql_python,
    ]
