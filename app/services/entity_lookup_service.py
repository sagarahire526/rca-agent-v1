"""
Entity Lookup Service — fetches distinct entity names from the macro_combined table.

Used by the Query Refiner Agent to normalise informal user inputs
(e.g. "voxline" → "VERICORE LLC.") before downstream agents see the query.

Read-only; returns empty lists on failure (graceful degradation).
"""
from __future__ import annotations

import logging
from typing import Dict, List

import psycopg2

import config

logger = logging.getLogger(__name__)

_TABLE = "pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined"


def _fetch_distinct(column: str) -> List[str]:
    """Return sorted distinct non-null values for *column* from the macro table."""
    try:
        conn = psycopg2.connect(
            host=config.PG_HOST,
            port=config.PG_PORT,
            database=config.PG_DATABASE,
            user=config.PG_USER,
            password=config.PG_PASSWORD,
            connect_timeout=5,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT DISTINCT {column} FROM {_TABLE} "
                    f"WHERE {column} IS NOT NULL ORDER BY {column}"
                )
                return [row[0] for row in cur.fetchall()]
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("entity_lookup_service: failed to fetch %s — %s", column, exc)
        return []


def get_all_entity_lookups() -> Dict[str, List[str]]:
    """
    Return canonical entity names for GC, market, and region.

    >>> lookups = get_all_entity_lookups()
    >>> lookups["gc_names"]    # ["ACME INC.", "VERICORE LLC.", ...]
    >>> lookups["markets"]     # ["CHICAGO", "DALLAS", ...]
    >>> lookups["regions"]     # ["CENTRAL", "WEST", ...]
    """
    return {
        "gc_names": _fetch_distinct("construction_gc"),
        "markets": _fetch_distinct("m_market"),
        "regions": _fetch_distinct("rgn_region"),
    }
