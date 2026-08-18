"""
Internal Scenarios endpoints — curated planner step templates.

Endpoints:
    GET    /v1/internal-scenarios            — list all (without embeddings)
    POST   /v1/internal-scenarios            — create (auto-embeds the question)
    POST   /v1/internal-scenarios/search     — semantic search (default threshold 0.90)
    DELETE /v1/internal-scenarios/{id}       — delete by id

The planner consults this store before its own LLM decomposition. When a
stored scenario matches the user's query above threshold, the planner uses
the curated steps as the spine of its plan.
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException

from api.v1.schemas import (
    InternalScenarioCreate,
    InternalScenarioDeleteResponse,
    InternalScenarioMatch,
    InternalScenarioOut,
    InternalScenarioSearchRequest,
    InternalScenarioSearchResponse,
)
from services.internal_scenarios import get_internal_scenarios_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal-scenarios", tags=["Internal Scenarios"])


@router.get(
    "",
    response_model=list[InternalScenarioOut],
    summary="List all curated scenarios (without embeddings)",
)
def list_internal_scenarios():
    store = get_internal_scenarios_store()
    return store.list_all()


@router.post(
    "",
    response_model=InternalScenarioOut,
    status_code=201,
    summary="Create a new curated scenario",
)
def create_internal_scenario(body: InternalScenarioCreate):
    if not body.question.strip():
        raise HTTPException(status_code=422, detail="question must not be empty")
    if not body.steps:
        raise HTTPException(status_code=422, detail="steps must not be empty")

    store = get_internal_scenarios_store()
    try:
        return store.create(tag=body.tag, question=body.question, steps=body.steps)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        logger.exception("Internal scenario create failed")
        raise


@router.post(
    "/search",
    response_model=InternalScenarioSearchResponse,
    summary="Semantic-search curated scenarios",
)
def search_internal_scenarios(body: InternalScenarioSearchRequest):
    if not (0.0 <= body.threshold <= 1.0):
        raise HTTPException(
            status_code=422, detail="threshold must be between 0.0 and 1.0"
        )

    t0 = time.perf_counter()
    store = get_internal_scenarios_store()
    try:
        raw_matches = store.search(body.query, threshold=body.threshold)
    except Exception:
        logger.exception("Internal scenario search failed")
        raise

    elapsed = round((time.perf_counter() - t0) * 1000, 1)
    logger.info(
        "Internal-scenario search: %d match(es) for %.80s in %.0fms",
        len(raw_matches), body.query, elapsed,
    )

    matches = [
        InternalScenarioMatch(
            id=m["id"],
            tag=m["tag"],
            question=m["question"],
            steps=m["steps"],
            embedding_model=m["embedding_model"],
            created_at=m["created_at"],
            similarity_score=m["similarity_score"],
            similarity_pct=f"{m['similarity_score'] * 100:.1f}%",
        )
        for m in raw_matches
    ]

    return InternalScenarioSearchResponse(
        query=body.query,
        threshold=body.threshold,
        total_indexed=store.count(),
        matches_found=len(matches),
        matches=matches,
    )


@router.delete(
    "/{scenario_id}",
    response_model=InternalScenarioDeleteResponse,
    summary="Delete a curated scenario by id",
)
def delete_internal_scenario(scenario_id: str):
    store = get_internal_scenarios_store()
    if not store.delete(scenario_id):
        raise HTTPException(status_code=404, detail=f"Scenario '{scenario_id}' not found")
    return InternalScenarioDeleteResponse(deleted=True, id=scenario_id)
