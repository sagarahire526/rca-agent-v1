"""
BKG endpoints
  GET  /api/v1/schema
  POST /api/v1/bkg/query

Delegates to bkg_service; handles HTTP error mapping only.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException

import services.bkg_service as bkg_svc
from api.v1.schemas import BKGQueryRequest

router = APIRouter(tags=["BKG"])


@router.get("/schema")
def get_schema(table_name: Optional[str] = None):
    """
    Return the BKG schema overview.

    Pass `table_name` to get BKGNodes with map_* properties for a specific table,
    or omit it for a full overview of all mapped tables.
    """
    try:
        return bkg_svc.get_schema(table_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/bkg/query")
def bkg_query(req: BKGQueryRequest):
    """
    Query the Business Knowledge Graph directly.

    **Modes:**

    | mode | required fields | description |
    |------|----------------|-------------|
    | `get_node` | `node_id` | Fetch BKGNode by ID with relationships |
    | `find_relevant` | `question` | Keyword search across all BKGNodes |
    | `traverse` | `start`, `depth` | Walk RELATES_TO edges from a start node |
    | `get_kpi` | `node_id` | Get KPI definition, formula, business logic |
    | `schema` | *(none)* | List all tables with map_* properties |
    """
    try:
        return bkg_svc.query(req.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
