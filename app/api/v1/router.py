"""
v1 router — aggregates all v1 endpoint routers under the /v1 prefix.

Authentication is applied here, at include time, so a new route cannot be
added to an existing router and accidentally ship unauthenticated.

`sse_simulate` and `chart` declare their own per-route dependencies because
two of their routes are loaded directly by the browser and need the
`?token=` fallback — see api/deps.py.

The sandbox execute route is intentionally absent: it exposed arbitrary
Python execution (pentest finding #1) and had no product consumer. The
sandbox itself is still used in-process by planner.py and langchain_tools.py.
"""
from fastapi import APIRouter, Depends

from api.deps import require_auth
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

_authenticated = [Depends(require_auth)]

# Public liveness probe only — no dependencies, no service detail.
router.include_router(health.public_router)

router.include_router(simulate.router, dependencies=_authenticated)
router.include_router(threads.router, dependencies=_authenticated)
router.include_router(feedback.router, dependencies=_authenticated)
router.include_router(health.router, dependencies=_authenticated)
router.include_router(semantic.router, dependencies=_authenticated)
router.include_router(internal_scenarios.router, dependencies=_authenticated)

# Auth declared per-route inside these two modules.
router.include_router(sse_simulate.router)
router.include_router(chart.router)
