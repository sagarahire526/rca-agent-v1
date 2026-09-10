"""
v1 router — aggregates all v1 endpoint routers under the /v1 prefix.

The sandbox execute route is intentionally absent: it exposed arbitrary
Python execution (pentest finding 2.1) and had no product consumer. The
sandbox itself is still used in-process by planner.py and langchain_tools.py.

AUTHENTICATION IS CURRENTLY DISABLED at the product owner's request, to
unblock the frontend. The dependency is implemented and tested in
api/deps.py — re-enable by adding `dependencies=_authenticated` back to the
includes below (and the per-route dependencies in sse_simulate.py/chart.py).

    from api.deps import require_auth
    _authenticated = [Depends(require_auth)]

Note that /analyze and /analyze/stream execute LLM-generated SQL against
production data while unauthenticated.
"""
from fastapi import APIRouter

from api.v1.endpoints import (
    chart,
    feedback,
    health,
    internal_scenarios,
    semantic,
    simulate,
    sse_simulate,
    threads,
)

router = APIRouter(prefix="/v1")

router.include_router(health.public_router)
router.include_router(simulate.router)
router.include_router(threads.router)
router.include_router(feedback.router)
router.include_router(health.router)
router.include_router(semantic.router)
router.include_router(internal_scenarios.router)
router.include_router(sse_simulate.router)
router.include_router(chart.router)
