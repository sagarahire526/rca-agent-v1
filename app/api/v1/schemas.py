"""
Pydantic request / response schemas for the v1 API — RCA Agent.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


# ── Analyze (RCA) ────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    user_id: str
    query: str
    thread_id: Optional[str] = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "user_id": "user-001",
                "query": "Which regions have the highest H&S non-compliance in the last 60 days?",
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
    metric_id: Optional[str] = None
    question: Optional[str] = None
    start: Optional[str] = None
    depth: Optional[int] = 2
    rel_type: Optional[str] = None
    table_name: Optional[str] = None

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"mode": "get_node",      "node_id": "GeneralContractor"},
                {"mode": "find_relevant", "question": "H&S compliance"},
                {"mode": "traverse",      "start": "GeneralContractor", "depth": 2},
                {"mode": "diagnostic",    "metric_id": "completion_rate"},
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
    status: str


class ClarificationStatus(BaseModel):
    is_paused: bool
    clarification_id: Optional[str] = None
    query_id: Optional[str] = None
    questions_asked: Optional[list[str]] = None
    assumptions_offered: Optional[list[str]] = None
    asked_at: Optional[Any] = None


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
