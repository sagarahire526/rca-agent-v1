"""
Pydantic request / response schemas for the v1 API — RCA Agent.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel


# ── Enums ─────────────────────────────────────────────────────────────────────

class ProjectType(str, Enum):
    NTM = "NTM"
    AHLOB_MODERNIZATION = "AHLOB Modernization"
    BOTH = "Both"


# ── Analyze (RCA) ────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    user_id: str
    query: str
    project_type: ProjectType
    thread_id: Optional[str] = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "user_id": "user-001",
                "query": "Which regions have the highest H&S non-compliance in the last 60 days?",
                "project_type": "NTM",
                "thread_id": "session-abc-123",
            }
        }
    }


class ClarificationPayload(BaseModel):
    type: str
    original_query: str
    questions: list[str]
    assumptions_if_skipped: list[str]
    message: str


class AnalyzeResponse(BaseModel):
    status: str                        # "complete" | "clarification_needed"
    thread_id: str
    final_response: str
    errors: list[str]
    traversal_steps: int
    routing_decision: str              # "greeting" | "traversal" | "rca"
    planner_steps: list[str]
    clarification: Optional[ClarificationPayload] = None


# ── Resume (HITL) ────────────────────────────────────────────────────────────

class ResumeRequest(BaseModel):
    thread_id: str
    clarification: str

    model_config = {
        "json_schema_extra": {
            "example": {
                "thread_id": "session-abc-123",
                "clarification": "Analyze for the last 90 days across all regions.",
            }
        }
    }


# ── BKG ──────────────────────────────────────────────────────────────────────

class BKGQueryRequest(BaseModel):
    mode: str
    node_id: Optional[str] = None
    question: Optional[str] = None
    start: Optional[str] = None
    depth: Optional[int] = 2
    rel_type: Optional[str] = None
    table_name: Optional[str] = None

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"mode": "get_node",      "node_id": "general_contractor"},
                {"mode": "find_relevant", "question": "H&S compliance"},
                {"mode": "traverse",      "start": "general_contractor", "depth": 2},
                {"mode": "get_kpi",       "node_id": "on_air_cycle_time"},
                {"mode": "schema"},
            ]
        }
    }


# ── Semantic Retrieval ───────────────────────────────────────────────────────

class SemanticRetrieveRequest(BaseModel):
    question: str
    threshold: float = 0.70

    model_config = {
        "json_schema_extra": {
            "example": {
                "question": "Which vendors have the highest Civil SLA breaches?",
                "threshold": 0.70,
            }
        }
    }


class ScenarioMatch(BaseModel):
    scenario_id: int
    scenario: str
    data_phase_questions: list[str]
    data_phase_steps: list[str]
    calculation_phase_steps: list[str]
    simulator_phase_steps: list[str]
    simulation_methodology: str
    similarity_score: float
    similarity_pct: str


class SemanticRetrieveResponse(BaseModel):
    question: str
    threshold: float
    total_scenarios_searched: int
    matches_found: int
    matches: list[ScenarioMatch]


# ── Threads ──────────────────────────────────────────────────────────────────

class CreateThreadRequest(BaseModel):
    user_id: str
    thread_name: str

    model_config = {
        "json_schema_extra": {
            "example": {
                "user_id": "user-001",
                "thread_name": "H&S non-compliance investigation",
            }
        }
    }


class ThreadSummary(BaseModel):
    thread_id: str
    user_id: str
    thread_name: Optional[str] = None
    created_at: Any
    last_active_at: Any
    status: str
    total_queries: int


class MessageRecord(BaseModel):
    query_id: str
    thread_id: str
    user_id: str
    original_query: str
    refined_query: Optional[str] = None
    routing_decision: Optional[str] = None
    planning_rationale: Optional[Any] = None
    final_response: Optional[str] = None
    started_at: Any
    completed_at: Optional[Any] = None
    duration_ms: Optional[float] = None
    traces: Optional[Any] = None
    status: str


class ClarificationStatus(BaseModel):
    is_paused: bool
    clarification_id: Optional[str] = None
    query_id: Optional[str] = None
    questions_asked: Optional[list[str]] = None
    assumptions_offered: Optional[list[str]] = None
    asked_at: Optional[Any] = None


# ── Feedback ──────────────────────────────────────────────────────────────────

class FeedbackRequest(BaseModel):
    thread_id: str
    query_id: str
    user_id: str
    username: str
    rating: Optional[int] = None
    is_positive: Optional[bool] = None
    comment: Optional[str] = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "thread_id": "session-abc-123",
                "query_id": "query-xyz-456",
                "user_id": "user-001",
                "username": "sagar.ahire",
                "rating": 4,
                "is_positive": True,
                "comment": "Very accurate RCA results!",
            }
        }
    }


class FeedbackOut(BaseModel):
    feedback_id: str
    thread_id: str
    query_id: str
    user_id: str
    username: str
    rating: Optional[int] = None
    is_positive: Optional[bool] = None
    comment: Optional[str] = None
    created_at: Any


class FeedbackSubmitResponse(BaseModel):
    feedback_id: str
    status: str = "submitted"


class FeedbackStats(BaseModel):
    total: int
    avg_rating: Optional[float] = None
    thumbs_up: int
    thumbs_down: int


# ── Sandbox ──────────────────────────────────────────────────────────────────

class SandboxRequest(BaseModel):
    code: str
    timeout_seconds: int = 30

    model_config = {
        "json_schema_extra": {
            "example": {
                "code": (
                    "df = pd.read_sql('SELECT 1 AS test', conn)\n"
                    "result = {'data': df.to_dict(orient='records')}"
                ),
                "timeout_seconds": 30,
            }
        }
    }
