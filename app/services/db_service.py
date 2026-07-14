"""
DB Service — handles all persistence to pwc_agent_utility_schema.

Writes to three tables:
  • threads               — one row per conversation thread
  • queries               — one row per user query
  • hitl_clarifications   — one row per HITL pause/resume cycle

Design rules:
  - Every function opens and closes its own connection (consistent with
    the rest of the codebase which also uses per-operation connections).
  - DB errors are logged but NEVER raised — DB failures must not block
    the agent from returning a response to the user.
"""
from __future__ import annotations

import json
import logging
import uuid
from contextlib import contextmanager

import psycopg2
from psycopg2.pool import ThreadedConnectionPool

import config
from services.json_safe import safe_dumps as _dumps_safe

logger = logging.getLogger(__name__)

_SCHEMA = "pwc_agent_utility_schema"

_pool: ThreadedConnectionPool | None = None


# ─────────────────────────────────────────────
# Pool lifecycle (called from main.py lifespan)
# ─────────────────────────────────────────────

def init_pool() -> None:
    """Create the shared connection pool. Call once at startup."""
    global _pool
    _pool = ThreadedConnectionPool(
        minconn=5,
        maxconn=30,
        host=config.PG_HOST,
        port=config.PG_PORT,
        database=config.PG_DATABASE,
        user=config.PG_USER,
        password=config.PG_PASSWORD,
        connect_timeout=5,
    )
    logger.info("PostgreSQL connection pool created (min=5, max=30)")


def close_pool() -> None:
    """Close all pooled connections. Call once at shutdown."""
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None
        logger.info("PostgreSQL connection pool closed")


# ─────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────

def _unwrap_string_encoded_json(value):
    """
    Recursively walk a value and convert any string that is itself a JSON-encoded
    object/array into the real parsed structure, so JSONB columns store real
    JSON — never JSON enclosed in a string.
    """
    if isinstance(value, dict):
        return {k: _unwrap_string_encoded_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap_string_encoded_json(item) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")) and stripped.endswith(("}", "]")):
            try:
                parsed = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                return value
            if isinstance(parsed, (dict, list)):
                return _unwrap_string_encoded_json(parsed)
    return value


@contextmanager
def _conn():
    """Get a connection from the pool, auto-commit/rollback and return it."""
    if _pool is None:
        raise RuntimeError("Connection pool not initialized — call init_pool() first")
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


def ensure_tables() -> None:
    """
    Create all required tables in pwc_agent_utility_schema if they do not exist.
    Called once at application startup.
    """
    ddl = f"""
        CREATE TABLE IF NOT EXISTS {_SCHEMA}.rca_agent_threads (
            thread_id       VARCHAR(100)    PRIMARY KEY,
            user_id         VARCHAR(100)    NOT NULL,
            thread_name     VARCHAR(255),
            created_at      TIMESTAMP       NOT NULL DEFAULT NOW(),
            last_active_at  TIMESTAMP       NOT NULL DEFAULT NOW(),
            status          VARCHAR(20)     NOT NULL DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS {_SCHEMA}.rca_agent_queries (
            query_id            VARCHAR(100)    PRIMARY KEY,
            thread_id           VARCHAR(100)    NOT NULL
                                    REFERENCES {_SCHEMA}.rca_agent_threads(thread_id),
            user_id             VARCHAR(100)    NOT NULL,
            original_query      TEXT            NOT NULL,
            refined_query       TEXT,
            routing_decision    VARCHAR(50),
            planning_rationale  JSONB,
            final_response      TEXT,
            started_at          TIMESTAMP       NOT NULL DEFAULT NOW(),
            completed_at        TIMESTAMP,
            duration_ms         NUMERIC(12, 2),
            status              VARCHAR(20)     NOT NULL DEFAULT 'running'
        );

        CREATE TABLE IF NOT EXISTS {_SCHEMA}.rca_agent_hitl_clarifications (
            clarification_id    VARCHAR(100)    PRIMARY KEY,
            query_id            VARCHAR(100)    NOT NULL
                                    REFERENCES {_SCHEMA}.rca_agent_queries(query_id),
            thread_id           VARCHAR(100)    NOT NULL
                                    REFERENCES {_SCHEMA}.rca_agent_threads(thread_id),
            questions_asked     JSONB           NOT NULL,
            assumptions_offered JSONB           NOT NULL,
            user_answer         TEXT,
            asked_at            TIMESTAMP       NOT NULL DEFAULT NOW(),
            answered_at         TIMESTAMP,
            was_skipped         BOOLEAN         DEFAULT FALSE
        );

        CREATE TABLE IF NOT EXISTS {_SCHEMA}.rca_agent_feedback (
            feedback_id     VARCHAR(100)    PRIMARY KEY,
            thread_id       VARCHAR(100)    NOT NULL
                                REFERENCES {_SCHEMA}.rca_agent_threads(thread_id),
            query_id        VARCHAR(100)    NOT NULL
                                REFERENCES {_SCHEMA}.rca_agent_queries(query_id),
            user_id         VARCHAR(100)    NOT NULL,
            username        VARCHAR(255)    NOT NULL,
            rating          SMALLINT,
            is_positive     BOOLEAN,
            comment         TEXT,
            created_at      TIMESTAMP       NOT NULL DEFAULT NOW()
        );
    """
    migrate_ddl = f"""
        ALTER TABLE {_SCHEMA}.rca_agent_queries
            ADD COLUMN IF NOT EXISTS traces JSONB;
        ALTER TABLE {_SCHEMA}.rca_agent_queries
            ADD COLUMN IF NOT EXISTS analysis JSONB;
        ALTER TABLE {_SCHEMA}.rca_agent_queries
            ADD COLUMN IF NOT EXISTS algorithm TEXT;
        ALTER TABLE {_SCHEMA}.rca_agent_queries
            ADD COLUMN IF NOT EXISTS charts JSONB;
        ALTER TABLE {_SCHEMA}.rca_agent_queries
            ADD COLUMN IF NOT EXISTS scenario_match_found BOOLEAN;

        DO $$ BEGIN
            CREATE TYPE {_SCHEMA}.rca_agent_type AS ENUM ('rca', 'recommendation');
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;

        -- Added NULLABLE with NO default on purpose: pre-existing threads were a
        -- mix of both agents, and we cannot know which is which, so they stay
        -- NULL rather than being wrongly stamped 'rca'. NULL == "legacy /
        -- unknown agent" and, per the read filters below, surfaces in BOTH
        -- agents' lists so no old thread is ever lost. Every thread created from
        -- here on is stamped explicitly on insert (see upsert_thread).
        ALTER TABLE {_SCHEMA}.rca_agent_threads
            ADD COLUMN IF NOT EXISTS agent_type {_SCHEMA}.rca_agent_type;

        -- Corrective for any DB that already ran an earlier NOT NULL DEFAULT 'rca'
        -- version of this migration: make the column nullable again and drop the
        -- default so future inserts must supply the value.
        ALTER TABLE {_SCHEMA}.rca_agent_threads
            ALTER COLUMN agent_type DROP NOT NULL;
        ALTER TABLE {_SCHEMA}.rca_agent_threads
            ALTER COLUMN agent_type DROP DEFAULT;
    """
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
                cur.execute(migrate_ddl)
        logger.info("pwc_rca_agent_schema tables verified / created.")
    except Exception as exc:
        logger.error("ensure_tables failed: %s", exc)


def _exec(sql: str, params: tuple) -> None:
    """Execute a single DML statement. Logs and swallows all DB errors."""
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
    except Exception as exc:
        logger.error("DB write failed — %.80s | error=%s", sql, exc)


def _fetch_one(sql: str, params: tuple):
    """Fetch a single scalar value. Returns None on error or no result."""
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
        return row[0] if row else None
    except Exception as exc:
        logger.error("DB read failed — %.80s | error=%s", sql, exc)
        return None


def _fetch_rows(sql: str, params: tuple) -> list[dict]:
    """Fetch all rows as a list of dicts. Returns [] on error or no results."""
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [desc[0] for desc in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        logger.error("DB read failed — %.80s | error=%s", sql, exc)
        return []


def _fetch_row(sql: str, params: tuple) -> dict | None:
    """Fetch a single row as a dict. Returns None on error or no result."""
    rows = _fetch_rows(sql, params)
    return rows[0] if rows else None


# ─────────────────────────────────────────────
# threads
# ─────────────────────────────────────────────

def upsert_thread(
    thread_id: str,
    user_id: str,
    thread_name: str | None = None,
    agent_type: str = "rca",
) -> None:
    """
    Create thread if new; on conflict refresh last_active_at and set status=active.

    agent_type ('rca' | 'recommendation') is set only on first insert — it is
    intentionally NOT touched on conflict, so a thread never changes ownership
    once created (a later stream upsert can't relabel it).
    """
    _exec(
        f"""
        INSERT INTO {_SCHEMA}.rca_agent_threads
            (thread_id, user_id, thread_name, agent_type, created_at, last_active_at, status)
        VALUES (%s, %s, %s, %s, NOW(), NOW(), 'active')
        ON CONFLICT (thread_id)
        DO UPDATE SET last_active_at = NOW(), status = 'active'
        """,
        (thread_id, user_id, thread_name, agent_type),
    )


def auto_name_thread(thread_id: str, query: str) -> None:
    """Set thread_name to the first user query (truncated) if it's currently NULL or blank."""
    name = query.strip()[:250]
    if not name:
        return
    _exec(
        f"UPDATE {_SCHEMA}.rca_agent_threads SET thread_name = %s "
        f"WHERE thread_id = %s AND (thread_name IS NULL OR btrim(thread_name) = '')",
        (name, thread_id),
    )


def touch_thread(thread_id: str) -> None:
    """Refresh last_active_at on resume (user_id already stored from initial call)."""
    _exec(
        f"UPDATE {_SCHEMA}.rca_agent_threads SET last_active_at = NOW() WHERE thread_id = %s",
        (thread_id,),
    )


# ─────────────────────────────────────────────
# queries
# ─────────────────────────────────────────────

def create_query(
    query_id: str,
    thread_id: str,
    user_id: str,
    original_query: str,
) -> None:
    """Insert a new query row with status=running at the moment of receipt."""
    _exec(
        f"""
        INSERT INTO {_SCHEMA}.rca_agent_queries
            (query_id, thread_id, user_id, original_query, started_at, status)
        VALUES (%s, %s, %s, %s, NOW(), 'running')
        """,
        (query_id, thread_id, user_id, original_query),
    )


def update_query_paused(query_id: str) -> None:
    """Mark query as paused while waiting for HITL clarification."""
    _exec(
        f"UPDATE {_SCHEMA}.rca_agent_queries SET status = 'paused' WHERE query_id = %s",
        (query_id,),
    )


def update_query_complete(
    query_id: str,
    refined_query: str,
    routing_decision: str,
    planner_steps: list[str],
    final_response: str,
    duration_ms: float,
    traces: dict | None = None,
    analysis: dict | None = None,
    algorithm: str | None = None,
    charts: dict | None = None,
    scenario_match_found: bool | None = None,
) -> None:
    """
    Finalize a completed query.
    planning_rationale is stored as a JSON array of the planner steps.
    traces is stored as a JSONB object with execution trace details.
    analysis is stored as a JSONB object with the pre-fetched semantic context
    (KPIs, question bank hits, RCA scenarios, keywords) used for this query.
    algorithm is the numbered narrative of how the agent reached its answer,
    produced by the parallel fast-LLM in agents/response.py.
    charts is the full Highcharts-compatible payload {"charts": [...],
    "rationale": "..."} produced by the parallel chart-generation LLM in
    agents/response.py — stored so the /chart/{query_id} endpoint can
    re-serve it without re-running the agent.
    """
    planning_rationale = _dumps_safe(planner_steps) if planner_steps else None
    traces_json = _dumps_safe(traces, default=str) if traces else None
    analysis_json = None
    if analysis:
        cleaned_analysis = _unwrap_string_encoded_json(analysis)
        analysis_json = _dumps_safe(cleaned_analysis, ensure_ascii=False, default=str)
    charts_json = _dumps_safe(charts, ensure_ascii=False, default=str) if charts else None
    _exec(
        f"""
        UPDATE {_SCHEMA}.rca_agent_queries SET
            refined_query        = %s,
            routing_decision     = %s,
            planning_rationale   = %s,
            final_response       = %s,
            completed_at         = NOW(),
            duration_ms          = %s,
            traces               = %s,
            analysis             = %s,
            algorithm            = %s,
            charts               = %s,
            scenario_match_found = %s,
            status               = 'complete'
        WHERE query_id = %s
        """,
        (
            refined_query,
            routing_decision,
            planning_rationale,
            final_response,
            duration_ms,
            traces_json,
            analysis_json,
            algorithm or None,
            charts_json,
            scenario_match_found,
            query_id,
        ),
    )


def update_query_error(query_id: str, duration_ms: float) -> None:
    """Mark query as errored with the elapsed duration."""
    _exec(
        f"""
        UPDATE {_SCHEMA}.rca_agent_queries SET
            completed_at = NOW(),
            duration_ms  = %s,
            status       = 'error'
        WHERE query_id = %s
        """,
        (duration_ms, query_id),
    )


def get_paused_query_id(thread_id: str) -> str | None:
    """Return the query_id of the most recent paused query for this thread."""
    return _fetch_one(
        f"""
        SELECT query_id FROM {_SCHEMA}.rca_agent_queries
        WHERE thread_id = %s AND status = 'paused'
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (thread_id,),
    )


# ─────────────────────────────────────────────
# hitl_clarifications
# ─────────────────────────────────────────────

def create_hitl_clarification(
    query_id: str,
    thread_id: str,
    questions_asked: list[str],
    assumptions_offered: list[str],
) -> None:
    """Record the clarification questions shown to the user."""
    _exec(
        f"""
        INSERT INTO {_SCHEMA}.rca_agent_hitl_clarifications
            (clarification_id, query_id, thread_id,
             questions_asked, assumptions_offered, asked_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        """,
        (
            str(uuid.uuid4()),
            query_id,
            thread_id,
            _dumps_safe(questions_asked),
            _dumps_safe(assumptions_offered),
        ),
    )


def update_hitl_answered(
    query_id: str,
    user_answer: str,
    was_skipped: bool,
) -> None:
    """Record the user's response to the clarification questions."""
    _exec(
        f"""
        UPDATE {_SCHEMA}.rca_agent_hitl_clarifications SET
            user_answer = %s,
            answered_at = NOW(),
            was_skipped = %s
        WHERE query_id = %s
        """,
        (user_answer, was_skipped, query_id),
    )


# ─────────────────────────────────────────────
# Read queries (used by threads endpoints)
# ─────────────────────────────────────────────

def get_threads_by_user(user_id: str, agent_type: str = "rca") -> list[dict]:
    """
    Return all threads for a user under a single agent, most recent first,
    with query count. Filtering on agent_type is what keeps RCA threads out of
    the Recommendation agent's list and vice versa.

    Legacy threads with agent_type IS NULL (created before agent tagging, when
    the two agents shared threads) are returned to BOTH agents so no old thread
    is ever lost to the user.
    """
    return _fetch_rows(
        f"""
        SELECT
            t.thread_id,
            t.user_id,
            t.thread_name,
            t.agent_type,
            t.created_at,
            t.last_active_at,
            t.status,
            COUNT(q.query_id) AS total_queries
        FROM {_SCHEMA}.rca_agent_threads t
        LEFT JOIN {_SCHEMA}.rca_agent_queries q ON q.thread_id = t.thread_id
        WHERE t.user_id = %s AND (t.agent_type = %s OR t.agent_type IS NULL)
        GROUP BY t.thread_id
        ORDER BY t.last_active_at DESC
        """,
        (user_id, agent_type),
    )


def get_thread(thread_id: str, agent_type: str | None = None) -> dict | None:
    """
    Return a single thread's metadata plus query count.

    When agent_type is given, the thread is only returned if it belongs to that
    agent — so an RCA request can never read a Recommendation thread by ID (and
    vice versa). A mismatch returns None, indistinguishable from "not found".
    Legacy threads (agent_type IS NULL) are accessible under either agent.
    """
    where = "WHERE t.thread_id = %s"
    params: tuple = (thread_id,)
    if agent_type is not None:
        where += " AND (t.agent_type = %s OR t.agent_type IS NULL)"
        params = (thread_id, agent_type)
    return _fetch_row(
        f"""
        SELECT
            t.thread_id,
            t.user_id,
            t.thread_name,
            t.agent_type,
            t.created_at,
            t.last_active_at,
            t.status,
            COUNT(q.query_id) AS total_queries
        FROM {_SCHEMA}.rca_agent_threads t
        LEFT JOIN {_SCHEMA}.rca_agent_queries q ON q.thread_id = t.thread_id
        {where}
        GROUP BY t.thread_id
        """,
        params,
    )


def delete_thread(thread_id: str) -> bool:
    """
    Delete a thread and all its child rows.
    Deletes in FK order: feedback → hitl_clarifications → queries → threads.
    Returns True if the thread existed and was deleted.
    """
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {_SCHEMA}.rca_agent_feedback WHERE thread_id = %s",
                    (thread_id,),
                )
                cur.execute(
                    f"DELETE FROM {_SCHEMA}.rca_agent_hitl_clarifications WHERE thread_id = %s",
                    (thread_id,),
                )
                cur.execute(
                    f"DELETE FROM {_SCHEMA}.rca_agent_queries WHERE thread_id = %s",
                    (thread_id,),
                )
                cur.execute(
                    f"DELETE FROM {_SCHEMA}.rca_agent_threads WHERE thread_id = %s",
                    (thread_id,),
                )
                return cur.rowcount > 0
    except Exception as exc:
        logger.error("delete_thread failed — thread=%s | error=%s", thread_id, exc)
        return False


def get_messages_by_thread(thread_id: str) -> list[dict]:
    """Return all queries for a thread ordered by started_at ascending."""
    return _fetch_rows(
        f"""
        SELECT
            query_id,
            thread_id,
            user_id,
            original_query,
            refined_query,
            routing_decision,
            planning_rationale,
            final_response,
            started_at,
            completed_at,
            duration_ms,
            traces,
            analysis,
            algorithm,
            charts,
            scenario_match_found,
            status
        FROM {_SCHEMA}.rca_agent_queries
        WHERE thread_id = %s
        ORDER BY started_at ASC
        """,
        (thread_id,),
    )


def get_charts_by_query_id(query_id: str) -> dict | None:
    """
    Return the chart payload + the original query text for a single query.
    Used by the /chart/{query_id} endpoints. Returns None if the query row
    does not exist.
    """
    return _fetch_row(
        f"""
        SELECT charts, original_query
        FROM {_SCHEMA}.rca_agent_queries
        WHERE query_id = %s
        """,
        (query_id,),
    )


def get_pending_clarification(thread_id: str) -> dict | None:
    """
    Return the pending clarification for a paused thread, or None if not paused.
    Joins queries + hitl_clarifications to get full context in one call.
    """
    return _fetch_row(
        f"""
        SELECT
            hc.clarification_id,
            hc.query_id,
            hc.questions_asked,
            hc.assumptions_offered,
            hc.asked_at
        FROM {_SCHEMA}.rca_agent_hitl_clarifications hc
        JOIN {_SCHEMA}.rca_agent_queries q ON q.query_id = hc.query_id
        WHERE q.thread_id = %s
          AND q.status    = 'paused'
          AND hc.answered_at IS NULL
        ORDER BY hc.asked_at DESC
        LIMIT 1
        """,
        (thread_id,),
    )


# ─────────────────────────────────────────────
# feedback
# ─────────────────────────────────────────────

def create_feedback(
    feedback_id: str,
    thread_id: str,
    query_id: str,
    user_id: str,
    username: str,
    rating: int | None = None,
    is_positive: bool | None = None,
    comment: str | None = None,
) -> None:
    """Insert a feedback entry linked to a specific query turn."""
    _exec(
        f"""
        INSERT INTO {_SCHEMA}.rca_agent_feedback
            (feedback_id, thread_id, query_id, user_id, username,
             rating, is_positive, comment, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
        """,
        (feedback_id, thread_id, query_id, user_id, username, rating, is_positive, comment),
    )


def get_feedback_for_query(query_id: str) -> list[dict]:
    """Return all feedback entries for a specific query turn."""
    return _fetch_rows(
        f"""
        SELECT feedback_id, thread_id, query_id, user_id, username,
               rating, is_positive, comment, created_at
        FROM {_SCHEMA}.rca_agent_feedback
        WHERE query_id = %s
        ORDER BY created_at ASC
        """,
        (query_id,),
    )


def get_feedback_for_thread(thread_id: str) -> list[dict]:
    """Return all feedback entries for a given thread."""
    return _fetch_rows(
        f"""
        SELECT feedback_id, thread_id, query_id, user_id, username,
               rating, is_positive, comment, created_at
        FROM {_SCHEMA}.rca_agent_feedback
        WHERE thread_id = %s
        ORDER BY created_at ASC
        """,
        (thread_id,),
    )


def get_feedback_stats(thread_id: str | None = None) -> dict:
    """Return aggregate feedback stats (count, avg rating, thumbs up/down)."""
    where = "WHERE thread_id = %s" if thread_id else ""
    params = (thread_id,) if thread_id else ()
    sql = f"""
        SELECT
            COUNT(*) AS total,
            ROUND(AVG(rating)::numeric, 2) AS avg_rating,
            COUNT(*) FILTER (WHERE is_positive = true) AS thumbs_up,
            COUNT(*) FILTER (WHERE is_positive = false) AS thumbs_down
        FROM {_SCHEMA}.rca_agent_feedback
        {where}
    """
    row = _fetch_row(sql, params)
    return row if row else {"total": 0, "avg_rating": None, "thumbs_up": 0, "thumbs_down": 0}
