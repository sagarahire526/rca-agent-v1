"""
RCA Service — business logic layer for the LangGraph RCA agent pipeline.

Exposes three operations:
  - run_query(query, thread_id, user_id)    — start a new RCA investigation
  - resume_query(clarification, thread_id)  — resume after HITL clarification
  - get_interrupt_status(thread_id)         — check if a thread is paused
"""
from __future__ import annotations

import logging
import time
import uuid

from graph import run_rca, resume_rca, get_pending_interrupt
import services.db_service as db_svc

logger = logging.getLogger(__name__)


def _shape_response(state: dict) -> dict:
    """Convert a raw RCAState dict into the API response shape."""
    interrupts = state.get("__interrupt__", [])
    if interrupts:
        raw = interrupts[0]
        interrupt_payload = raw.value if hasattr(raw, "value") else raw
        return {
            "status": "clarification_needed",
            "clarification": interrupt_payload,
            "final_response": "",
            "errors": [],
            "traversal_steps": 0,
            "routing_decision": "",
            "planner_steps": [],
        }

    return {
        "status": "complete",
        "clarification": None,
        "final_response": state.get("final_response", ""),
        "errors": state.get("errors", []),
        "traversal_steps": state.get("traversal_steps_taken", 0),
        "routing_decision": state.get("routing_decision", ""),
        "planner_steps": state.get("planner_steps", []),
    }


def run_query(
    query: str,
    thread_id: str = "default",
    user_id: str = "anonymous",
) -> dict:
    """Start a new RCA investigation."""
    if not query.strip():
        raise ValueError("Query cannot be empty")

    query_id = str(uuid.uuid4())
    t0 = time.perf_counter()

    db_svc.upsert_thread(thread_id, user_id)
    db_svc.create_query(query_id, thread_id, user_id, query)

    logger.info("Starting RCA query [thread=%s query=%s]: %.80s", thread_id, query_id, query)

    try:
        state = run_rca(query, thread_id=thread_id)
    except Exception:
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        db_svc.update_query_error(query_id, duration_ms)
        raise

    duration_ms = round((time.perf_counter() - t0) * 1000, 1)
    response = _shape_response(state)

    if response["status"] == "clarification_needed":
        clarification = response.get("clarification", {})
        db_svc.update_query_paused(query_id)
        db_svc.create_hitl_clarification(
            query_id=query_id,
            thread_id=thread_id,
            questions_asked=clarification.get("questions", []),
            assumptions_offered=clarification.get("assumptions_if_skipped", []),
        )
    else:
        db_svc.update_query_complete(
            query_id=query_id,
            refined_query=state.get("refined_query", ""),
            routing_decision=state.get("routing_decision", ""),
            planner_steps=state.get("planner_steps", []),
            final_response=state.get("final_response", ""),
            duration_ms=duration_ms,
        )

    return response


def resume_query(clarification: str, thread_id: str) -> dict:
    """Resume a paused RCA with the user's clarification answer."""
    if not clarification.strip():
        raise ValueError("Clarification cannot be empty")
    if not thread_id.strip():
        raise ValueError("thread_id is required to resume")

    was_skipped = clarification.strip() == "Accept stated assumptions"

    query_id = db_svc.get_paused_query_id(thread_id)
    if query_id:
        db_svc.update_hitl_answered(query_id, clarification, was_skipped)

    db_svc.touch_thread(thread_id)

    logger.info("Resuming RCA query [thread=%s]", thread_id)

    t0 = time.perf_counter()
    state = resume_rca(clarification, thread_id)
    duration_ms = round((time.perf_counter() - t0) * 1000, 1)

    response = _shape_response(state)

    if query_id:
        if response["status"] == "complete":
            db_svc.update_query_complete(
                query_id=query_id,
                refined_query=state.get("refined_query", ""),
                routing_decision=state.get("routing_decision", ""),
                planner_steps=state.get("planner_steps", []),
                final_response=state.get("final_response", ""),
                duration_ms=duration_ms,
            )
        else:
            db_svc.update_query_error(query_id, duration_ms)

    return response


def get_interrupt_status(thread_id: str) -> dict | None:
    return get_pending_interrupt(thread_id)
