"""
Schema Discovery node — Fetches the PostgreSQL table list only.

The full KG schema is no longer fetched here. Instead, each traversal
agent performs a per-query embedding search via schema_embedding_service
to get only the relevant nodes/paths for its specific sub-query.
"""
from __future__ import annotations

import logging
from typing import Any

from models.state import RCAState
from tools.bkg_tool import BKGTool

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# PostgreSQL table list
# ─────────────────────────────────────────────

def _fetch_table_list() -> str:
    """Fetch all available PostgreSQL tables from the KG and format as a prompt section."""
    try:
        bkg = BKGTool()
        result = bkg.query({"mode": "schema"})
        tables = result.get("tables", [])
        if not tables:
            return ""

        lines = ["\n\n=== AVAILABLE POSTGRESQL TABLES ==="]
        lines.append("Use ONLY these table names in SQL queries (use get_node on mapped nodes for full details):\n")

        for t in tables:
            name = t.get("table_name", "")
            db = t.get("database_name", "")
            nodes = t.get("nodes", [])
            node_ids = ", ".join(n.get("node_id", "") for n in nodes)
            key_cols = ", ".join(
                filter(None, set(n.get("key_column", "") for n in nodes))
            )

            detail = f"  nodes: {node_ids}" if node_ids else ""
            if key_cols:
                detail += f"  key_column(s): {key_cols}"
            if db:
                detail += f"  db: {db}"
            lines.append(f"  - {name}{detail}")

        lines.append("\nDo NOT invent table names. If you need a table not listed here, the data does not exist.")
        return "\n".join(lines)
    except Exception as e:
        logger.warning("Failed to fetch table list: %s", e)
        return ""


# ─────────────────────────────────────────────
# LangGraph node
# ─────────────────────────────────────────────

def discover_schema_node(state: RCAState) -> dict[str, Any]:
    """
    LangGraph node: Fetch PostgreSQL table list only.

    The KG node/path context is now fetched per-query in the traversal
    agent via embedding search (schema_embedding_service.search_schema).
    """
    try:
        table_list = _fetch_table_list()

        logger.info(f"Schema discovery: table list fetched ({len(table_list)} chars)")

        return {
            "kg_schema": table_list,
            "current_phase": "traversal",
            "messages": [{
                "agent": "schema_discovery",
                "content": f"Table list fetched ({len(table_list)} chars). KG context will be fetched per-query via embeddings.",
            }],
        }
    except Exception as e:
        logger.error(f"Schema discovery failed: {e}")
        return {
            "kg_schema": "",
            "current_phase": "traversal",
            "errors": [f"Schema discovery error: {e}"],
            "messages": [{
                "agent": "schema_discovery",
                "content": f"Schema discovery failed: {e}",
            }],
        }
