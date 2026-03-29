"""
RCA endpoints.

  POST /api/v1/analyze        — Start a new RCA investigation
  POST /api/v1/analyze/resume — Resume a paused investigation with user clarification
"""
import uuid

from fastapi import APIRouter, HTTPException

import services.rca_service as rca_svc
from api.v1.schemas import AnalyzeRequest, AnalyzeResponse, ResumeRequest

router = APIRouter(tags=["RCA Agent"])


@router.post("/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest):
    """
    Run a natural-language query through the RCA agent pipeline.

    Pipeline:
      1. Query Refiner — validates completeness; may pause for clarification.
      2. Orchestrator  — routes to the right downstream pipeline.
      3a. (greeting)   → response directly.
      3b. (traversal)  → schema discovery → traversal → response.
      3c. (rca)        → schema discovery → planner (parallel steps) → response.
    """
    thread_id = req.thread_id or str(uuid.uuid4())
    try:
        result = rca_svc.run_query(req.query, project_type=req.project_type.value, thread_id=thread_id, user_id=req.user_id)
        return AnalyzeResponse(thread_id=thread_id, **result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/analyze/resume", response_model=AnalyzeResponse)
def analyze_resume(req: ResumeRequest):
    """Resume an RCA investigation that paused for user clarification."""
    try:
        result = rca_svc.resume_query(req.clarification, thread_id=req.thread_id)
        return AnalyzeResponse(thread_id=req.thread_id, **result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
