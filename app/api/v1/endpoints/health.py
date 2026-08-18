"""
Health endpoint — GET /api/v1/health

Checks connectivity for all three external services:
  • Neo4j      (Knowledge Graph)
  • PostgreSQL  (operational data + semantic store)
  • OpenAI API  (validates key by retrieving the configured LLM model)

Response shape:
  {
    "status": "ok" | "degraded",
    "services": {
      "neo4j":    { "status": "connected"|"unavailable", "detail": "...", "latency_ms": 12.5 },
      "postgres": { "status": "connected"|"unavailable", "detail": "...", "latency_ms": 5.2  },
      "openai":   { "status": "connected"|"unavailable", "detail": "...", "latency_ms": 230  }
    },
    "checked_at": "2026-03-02T10:30:00Z"
  }
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import psycopg2
from fastapi import APIRouter
from openai import OpenAI

import config
import services.bkg_service as bkg_svc

logger = logging.getLogger(__name__)

# Authenticated, detailed health — mounted with an auth dependency in router.py.
router = APIRouter(tags=["System"])

# Unauthenticated liveness probe for the platform. Deliberately says nothing
# about backing services: the detailed view leaks hostnames, schema names and
# driver error text, which is what pentest finding #5 was about.
public_router = APIRouter(tags=["System"])


def _sanitize(exc: Exception) -> str:
    """
    Exception text from psycopg2 / neo4j carries hostnames, ports, database
    names and schema names. Surface it only outside deployed environments;
    the full detail always goes to the server log.
    """
    if config.IS_PRODUCTION:
        return "unavailable"
    return str(exc)


# ── Individual connectivity checks ─────────────────────────────────────────

def _check_neo4j() -> dict:
    t0 = time.perf_counter()
    try:
        result = bkg_svc.health()
        latency = round((time.perf_counter() - t0) * 1000, 1)
        if result["status"] == "connected":
            return {
                "status": "connected",
                "detail": f"{result.get('node_count', '?')} nodes loaded",
                "latency_ms": latency,
            }
        error = result.get("error", "unknown error")
        logger.warning("Neo4j health check reported an error: %s", error)
        return {
            "status": "unavailable",
            "detail": "unavailable" if config.IS_PRODUCTION else error,
            "latency_ms": latency,
        }
    except Exception as e:
        latency = round((time.perf_counter() - t0) * 1000, 1)
        logger.warning("Neo4j health check failed: %s", e)
        return {"status": "unavailable", "detail": _sanitize(e), "latency_ms": latency}


def _check_postgres() -> dict:
    t0 = time.perf_counter()
    try:
        conn = psycopg2.connect(
            host=config.PG_HOST,
            port=config.PG_PORT,
            database=config.PG_DATABASE,
            user=config.PG_USER,
            password=config.PG_PASSWORD,
            connect_timeout=5,
        )
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM pwc_semantic_information_schema.semantics_simulation"
        )
        row_count = cur.fetchone()[0]
        cur.close()
        conn.close()
        latency = round((time.perf_counter() - t0) * 1000, 1)
        return {
            "status": "connected",
            "detail": f"{row_count} scenario(s) indexed",
            "latency_ms": latency,
        }
    except Exception as e:
        latency = round((time.perf_counter() - t0) * 1000, 1)
        logger.warning("PostgreSQL health check failed: %s", e)
        return {"status": "unavailable", "detail": _sanitize(e), "latency_ms": latency}


def _check_openai() -> dict:
    """Validate OpenAI API key by retrieving the configured LLM model."""
    t0 = time.perf_counter()
    if not config.OPENAI_API_KEY:
        return {
            "status": "unavailable",
            "detail": "OPENAI_API_KEY is not set in environment",
            "latency_ms": 0.0,
        }
    try:
        client = OpenAI(api_key=config.OPENAI_API_KEY)
        # Retrieve model metadata — zero-cost, validates key + model access
        model_info = client.models.retrieve(config.LLM_MODEL)
        latency = round((time.perf_counter() - t0) * 1000, 1)
        return {
            "status": "connected",
            "detail": f"model '{model_info.id}' is accessible",
            "latency_ms": latency,
        }
    except Exception as e:
        latency = round((time.perf_counter() - t0) * 1000, 1)
        logger.warning("OpenAI health check failed: %s", e)
        return {"status": "unavailable", "detail": _sanitize(e), "latency_ms": latency}


# ── Endpoints ──────────────────────────────────────────────────────────────

@public_router.get("/healthz", summary="Liveness probe (public)")
def liveness():
    """
    Unauthenticated liveness probe for platform health checks.

    Reports only that the process is serving requests. Use `/health` (which
    requires credentials) for backing-service status.
    """
    return {"status": "ok"}


@router.get("/health", summary="System health check")
def health_check():
    """
    Verify connectivity to all external services.

    - **neo4j** — checks Neo4j Knowledge Graph (node count)
    - **postgres** — checks PostgreSQL and counts indexed scenarios in semantics_simulation
    - **openai** — validates API key by retrieving the configured LLM model (`LLM_MODEL` from env)

    Returns `status: ok` only when **all three** services are reachable.
    Returns `status: degraded` if any service is unavailable.
    """
    neo4j = _check_neo4j()
    postgres = _check_postgres()
    openai = _check_openai()

    all_statuses = [neo4j["status"], postgres["status"], openai["status"]]
    overall = "ok" if all(s == "connected" for s in all_statuses) else "degraded"

    return {
        "status": overall,
        "services": {
            "neo4j": neo4j,
            "postgres": postgres,
            "openai": openai,
        },
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
