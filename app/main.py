"""
FastAPI application entry point for the RCA Agent system.

Run:
    uvicorn app.main:app --host 0.0.0.0 --port 8000

Swagger UI:  http://localhost:8000/docs
"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Ensure app/ is on sys.path so internal imports (api, services, agents, …)
# resolve correctly whether we run  `uvicorn app.main:app`  from the project
# root or  `uvicorn main:app`  from inside app/.
_APP_DIR = str(Path(__file__).resolve().parent)
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.v1.router import router as v1_router
import services.db_service as db_svc

# ── Logging setup ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-30s | %(levelname)-5s | %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_svc.ensure_tables()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="RCA Agent API",
    description="LangGraph multi-agent Root Cause Analysis system backed by Neo4j and PostgreSQL.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", tags=["Root"])
async def root():
    return {
        "service": "RCA Agent",
        "version": "1.0.0",
        "docs": "/docs",
    }


app.include_router(v1_router, prefix="/api")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
        timeout_keep_alive=300,
    )
