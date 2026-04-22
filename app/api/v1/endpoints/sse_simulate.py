"""
SSE RCA endpoints.

  GET  /api/v1/analyze/stream         — start a streaming RCA investigation
  POST /api/v1/analyze/stream/resume  — resume a paused (HITL) stream
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import services.db_service as db_svc
from graph import stream_rca
from services.sse_manager import sse_manager
from services.trace_builder import build_traces
from api.v1.schemas import ProjectType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analyze", tags=["SSE Stream"])

_RCA_EXECUTOR = ThreadPoolExecutor(max_workers=50)


class StreamResumeRequest(BaseModel):
    thread_id: str
    clarification: str

    model_config = {
        "json_schema_extra": {
            "example": {
                "thread_id": "session-abc-123",
                "clarification": "Analyze for last 90 days across all regions.",
            }
        }
    }


def _run_stream_thread(
    query: str,
    project_type: str,
    query_id: str,
    thread_id: str,
    user_id: str,
) -> None:
    t0 = time.perf_counter()

    db_svc.upsert_thread(thread_id, user_id)
    db_svc.auto_name_thread(thread_id, query)
    db_svc.create_query(query_id, thread_id, user_id, query)

    def _on_hitl(payload: dict) -> None:
        db_svc.update_query_paused(query_id)
        db_svc.create_hitl_clarification(
            query_id=query_id,
            thread_id=thread_id,
            questions_asked=payload.get("questions", []),
            assumptions_offered=payload.get("assumptions_if_skipped", []),
        )

    try:
        final_state = stream_rca(
            query=query,
            query_id=query_id,
            thread_id=thread_id,
            mgr=sse_manager,
            project_type=project_type,
            on_hitl=_on_hitl,
        )
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)

        nodes_executed = final_state.pop("_nodes_executed", [])
        traces = build_traces(final_state, nodes_executed, duration_ms)

        db_svc.update_query_complete(
            query_id=query_id,
            refined_query=final_state.get("refined_query", ""),
            routing_decision=final_state.get("routing_decision", ""),
            planner_steps=final_state.get("planner_steps", []),
            final_response=final_state.get("final_response", ""),
            duration_ms=duration_ms,
            traces=traces,
        )
        sse_manager.put_sync(query_id, "complete", {
            "final_response":    final_state.get("final_response", ""),
            "routing_decision":  final_state.get("routing_decision", ""),
            "planner_steps":     final_state.get("planner_steps", []),
            "planning_rationale": final_state.get("planning_rationale", ""),
            "traversal_steps":   final_state.get("traversal_steps_taken", 0),
            "errors":            final_state.get("errors", []),
            "traces":            traces,
        })

    except Exception as exc:
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.exception("stream_rca failed [query=%s thread=%s]", query_id, thread_id)
        db_svc.update_query_error(query_id, duration_ms)
        sse_manager.put_sync(query_id, "error", {"message": str(exc)})

    finally:
        sse_manager.put_sync(query_id, "__done__", {})


async def _event_generator(
    queue: asyncio.Queue,
    query_id: str,
    thread_id: str,
) -> AsyncGenerator[str, None]:
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=20)
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
                continue
            event_name = item["event"]
            if event_name == "__done__":
                break
            yield f"event: {event_name}\ndata: {json.dumps(item['data'])}\n\n"
            if event_name == "error":
                break
    finally:
        sse_manager.cleanup(query_id, thread_id)


@router.get("/stream")
async def stream_analyze(
    query:        str = Query(..., description="The RCA query"),
    user_id:      str = Query(..., description="User identifier"),
    project_type: ProjectType = Query(..., description="Project type"),
    thread_id:    str = Query(None, description="Conversation thread ID"),
):
    """Start a streaming RCA investigation. Returns text/event-stream."""
    if not query.strip():
        raise HTTPException(status_code=422, detail="query cannot be empty")
    if not user_id.strip():
        raise HTTPException(status_code=422, detail="user_id cannot be empty")

    if not thread_id:
        thread_id = str(uuid.uuid4())
    query_id = str(uuid.uuid4())

    loop  = asyncio.get_running_loop()
    queue = sse_manager.register(query_id, loop)

    loop.run_in_executor(
        _RCA_EXECUTOR,
        _run_stream_thread,
        query, project_type.value, query_id, thread_id, user_id,
    )

    async def _stream_with_preamble() -> AsyncGenerator[str, None]:
        yield (
            f"event: stream_started\n"
            f"data: {json.dumps({'query_id': query_id, 'thread_id': thread_id})}\n\n"
        )
        async for chunk in _event_generator(queue, query_id, thread_id):
            yield chunk

    return StreamingResponse(
        _stream_with_preamble(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "Connection":       "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/stream/resume", status_code=200)
def resume_stream(req: StreamResumeRequest):
    """Resume a paused SSE stream after HITL clarification."""
    if not req.thread_id.strip():
        raise HTTPException(status_code=422, detail="thread_id cannot be empty")

    clarification = req.clarification.strip() or "Accept stated assumptions"
    was_skipped   = (clarification == "Accept stated assumptions")

    query_id = db_svc.get_paused_query_id(req.thread_id)
    if query_id:
        db_svc.update_hitl_answered(query_id, clarification, was_skipped)
    db_svc.touch_thread(req.thread_id)

    signaled = sse_manager.signal_resume(req.thread_id, clarification)
    if not signaled:
        raise HTTPException(
            status_code=404,
            detail=f"No active stream found for thread '{req.thread_id}'.",
        )

    return {"status": "resumed", "thread_id": req.thread_id}
