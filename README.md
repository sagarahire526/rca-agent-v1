# RCA Agent — LangGraph Multi-Agent Root Cause Analysis System

A LangGraph-based multi-agent system that performs Root Cause Analysis (RCA) for telecom tower deployment operations. It traverses a Neo4j Business Knowledge Graph (BKG) and PostgreSQL database to investigate delays, SLA breaches, compliance failures, vendor performance, and quality issues — delivering data-backed PM-readable reports with actionable recommendations.

---

## Architecture

```
User Query
    |
    v
+------------------------+
|   Query Refiner        |  <- Validates scope; may pause (HITL) for
|   [HITL Node]          |    clarification (geography, timeframe, entity)
+----------+-------------+
           |  (refined query)
           v
+------------------------+
|    Orchestrator        |  <- Classifies and routes
+----------+-------------+
      +----+-----------------------+
      |                            |
      v                            v
  "greeting"              "traversal" | "rca"
  (respond directly)               |
                                   v
                        +------------------------+
                        |   Schema Discovery     |  <- Fetches Neo4j KG schema
                        +----------+-------------+      + PostgreSQL table list
                              +----+----------------+
                              |                     |
                              v                     v
                     "traversal" path         "rca" path
                              |                     |
                              v                     v
                     +------------------+   +---------------------+
                     | Traversal Agent  |   |   Planner Agent     |
                     |  (single run)    |   |  (decomposes ->     |
                     +--------+---------+   |  N parallel steps)  |
                              |             +----------+----------+
                              |                        | N parallel
                              |             +----------v----------+
                              |             | Traversal Agent x N  |
                              |             | (ThreadPoolExecutor) |
                              |             +----------+----------+
                              +----------+-----------+
                                         v
                              +------------------------+
                              |   Response Agent       |  <- Synthesises findings,
                              +----------+-------------+    generates RCA report
                                         v
                                   RCA Report
```

---

## Agents

| Agent | Model | Role | Key Behaviour |
|---|---|---|---|
| **Query Refiner** | gpt-4o-mini | HITL scope checker | Validates geography, timeframe, specific entity. Pauses via `interrupt()` if missing. |
| **Orchestrator** | gpt-4o-mini | Router | Classifies as `greeting`, `traversal`, or `rca`. |
| **Schema Discovery** | — | KG context provider | Fetches Neo4j schema + PostgreSQL table list. No LLM call. |
| **Planner** | gpt-4o | Sub-query decomposer | Breaks complex RCA queries into 2-7 independent investigation sub-queries; runs them in parallel. |
| **Traversal** | gpt-4o | Data gatherer | Autonomous ReAct agent — uses tools to query Neo4j and PostgreSQL, gathers evidence. Built-in retry (max 3 attempts) for failed SQL/Python queries. |
| **Response** | gpt-4o | Report generator | Synthesises all findings into a structured PM-readable RCA report with root causes, impact assessment, and actionable recommendations. |

---

## RCA Investigation Areas

- **H&S / HSE Compliance** — PPE status, JSA compliance, check-in failures, vendor violations
- **SLA Breaches** — Civil (>21 days), RAN, Integration milestones vs targets
- **Quality / FTR** — First-time-right rates, rejection reasons, rework patterns
- **Vendor Performance** — Plan vs actual delivery, productivity, crew utilization
- **Delay Root Causes** — Material delays, site access issues, prerequisite blockers, crew shortage
- **Construction-to-On-Air Backlog** — Integration backlog, CMG delays, transmission issues
- **Process Compliance** — Check-in/check-out, ICOP readiness, RIOT completion

---

## Project Structure

```
rca-agent-v2/
|
+-- app/
|   +-- main.py                        # FastAPI entrypoint (uvicorn app.main:app)
|   +-- graph.py                       # LangGraph StateGraph + run_rca / resume_rca / stream_rca
|   |
|   +-- agents/
|   |   +-- query_refiner.py           # HITL node -- interrupts for scope clarification
|   |   +-- orchestrator.py            # Routing node (greeting / traversal / rca)
|   |   +-- schema_discovery.py        # Fetches Neo4j KG schema + table list
|   |   +-- planner.py                 # Decomposes query; runs N parallel traversals
|   |   +-- traversal.py               # Autonomous ReAct agent (sync + async)
|   |   +-- response.py                # Synthesises data -> PM RCA report
|   |
|   +-- api/v1/
|   |   +-- router.py                  # Aggregates all v1 routers under /api/v1
|   |   +-- schemas.py                 # Pydantic request/response models
|   |   +-- endpoints/
|   |       +-- simulate.py            # POST /analyze, POST /analyze/resume
|   |       +-- sse_simulate.py        # GET /analyze/stream, POST /analyze/stream/resume
|   |       +-- threads.py             # Thread management (list, get, delete, messages)
|   |       +-- health.py              # GET /health
|   |       +-- bkg.py                 # POST /bkg (direct KG queries)
|   |       +-- sandbox.py             # POST /sandbox (Python execution)
|   |       +-- semantic.py            # POST /semantic/retrieve
|   |
|   +-- services/
|   |   +-- llm_provider.py            # Instance-based LLM factory (LLMProvider)
|   |   +-- rca_service.py             # Business logic: run_query, resume_query
|   |   +-- sse_manager.py             # asyncio.Queue + threading.Event for SSE
|   |   +-- db_service.py              # PostgreSQL persistence (threads, queries, HITL)
|   |   +-- semantic_service.py        # Semantic search API client
|   |   +-- bkg_service.py             # Neo4j BKG service
|   |
|   +-- prompts/
|   |   +-- query_refiner_prompt.py    # HITL scope validation prompt
|   |   +-- orchestrator_prompt.py     # Routing classification prompt
|   |   +-- planner_prompt.py          # Investigation sub-query decomposition prompt
|   |   +-- traversal_prompt.py        # ReAct data-gathering prompt (with retry rules)
|   |   +-- response_prompt.py         # RCA report synthesis prompt
|   |
|   +-- models/
|   |   +-- state.py                   # RCAState TypedDict
|   |
|   +-- tools/
|   |   +-- langchain_tools.py         # LangChain tool wrappers
|   |   +-- neo4j_tool.py              # Neo4j connection + Cypher execution
|   |   +-- bkg_tool.py                # BKG query tool
|   |   +-- python_sandbox.py          # Python sandbox executor (read-only psycopg2)
|   |
|   +-- config/
|       +-- __init__.py                # Flat config from env vars (dotenv)
|       +-- settings.py                # Dataclass-based AppConfig
|
+-- streamlit_app.py                   # Streamlit chat UI (dev testing)
+-- requirements.txt
+-- .env                               # Environment variables (not committed)
```

---

## Setup

### Prerequisites
- Python 3.11+
- Neo4j with your knowledge graph database loaded
- PostgreSQL with operational data
- OpenAI API key

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure environment
Create a `.env` file in the project root:

```env
# OpenAI
OPENAI_API_KEY=sk-your-key
LLM_MODEL=gpt-4o

# Neo4j
NEO4J_URI=neo4j://127.0.0.1:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your-password
NEO4J_DATABASE=nokia-v-one

# PostgreSQL
PG_HOST=localhost
PG_PORT=5433
PG_DATABASE=nokia_syn_v1
PG_USER=postgres
PG_PASSWORD=your-password
```

### 3. Run the backend
```bash
uvicorn app.main:app --port 8000 --timeout-keep-alive 300
```

Tables in `pwc_rca_agent_schema` are created automatically at startup.

- Swagger UI: http://localhost:8000/docs

### 4. Run the Streamlit UI
```bash
streamlit run streamlit_app.py
```

Chat input is enabled by default with user ID `dev-user`.

---

## API Overview

Base URL: `http://localhost:8000/api/v1`

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Neo4j, PostgreSQL, OpenAI service status |
| `POST` | `/analyze` | Run an RCA query (blocking, full response) |
| `POST` | `/analyze/resume` | Resume a HITL-paused investigation |
| `GET` | `/analyze/stream` | **SSE** — stream RCA progress events |
| `POST` | `/analyze/stream/resume` | Resume a paused SSE stream |
| `GET` | `/threads?user_id=` | List all threads for a user |
| `GET` | `/threads/{thread_id}` | Thread metadata |
| `DELETE` | `/threads/{thread_id}` | Delete thread and all its data |
| `GET` | `/threads/{thread_id}/messages` | All queries within a thread |
| `GET` | `/threads/{thread_id}/clarification` | HITL pause status |

### Response Shape (POST /analyze)

```json
{
  "status": "complete",
  "thread_id": "uuid",
  "final_response": "### RCA Report: ...",
  "errors": [],
  "traversal_steps": 12,
  "routing_decision": "rca",
  "planner_steps": ["Sub-query 1: ...", "Sub-query 2: ..."]
}
```

---

## LLM Provider

Instance-based `LLMProvider` wrapping `langchain_openai.ChatOpenAI`:

```python
from services.llm_provider import LLMProvider

provider = LLMProvider(model="gpt-4o-mini")
llm = provider.get_llm()
```

Reads `OPENAI_API_KEY` from environment. For client deployment with a custom gateway (e.g., Nokia LLM Gateway), update the provider with `base_url`, `default_headers`, and `workspacename`.

---

## HITL Flow

The **Query Refiner** checks whether the user's query has enough scope (geography, timeframe, specific entity) to run a precise RCA investigation. If not:

**Standard HTTP:**
```
POST /analyze -> { status: "clarification_needed", clarification: { questions, assumptions } }
(user answers)
POST /analyze/resume { thread_id, clarification: "user answer" } -> { status: "complete", final_response }
```

**SSE (stream stays open):**
```
GET /analyze/stream -> event: hitl_start { questions, assumptions }
(SSE connection stays open)
POST /analyze/stream/resume { thread_id, clarification }
-> event: hitl_complete -> ... -> event: complete
```

---

## Database Schema

Three tables in `pwc_rca_agent_schema` (auto-created at startup):

```
threads              -- one row per conversation thread
  thread_id, user_id, created_at, last_active_at, status

queries              -- one row per user query
  query_id, thread_id, user_id, original_query, refined_query,
  routing_decision, planning_rationale (JSONB), final_response,
  started_at, completed_at, duration_ms, status

hitl_clarifications  -- one row per HITL pause/resume cycle
  clarification_id, query_id, thread_id,
  questions_asked (JSONB), assumptions_offered (JSONB),
  user_answer, asked_at, answered_at, was_skipped
```

---

## Example Queries

```
"Which regions have the highest H&S non-compliance in the last 60 days?"
"Which vendors have the most Civil SLA breaches (>21 days) in the last 90 days?"
"What is driving low first-time-right rates and which vendors are worst?"
"Which regions have the largest Construction-to-On-Air backlog?"
"Why is the Chicago construction pipeline delayed?"
"What corrective actions should we take for recurring quality issues?"
```

---

## Key Design Decisions

| Decision | Rationale |
|---|---|
| LangGraph `MemorySaver` checkpointer | Required for HITL `interrupt()` / `Command(resume=...)` across HTTP requests |
| Module-level `_graph` singleton | All requests share one compiled graph and one MemorySaver |
| `threading.Event` for SSE HITL | Blocks executor thread (not the event loop) during HITL pause |
| Per-operation DB connections | DB errors are logged but never raised — DB failures never block the agent |
| `ThreadPoolExecutor` for parallel traversal | Planner's N sub-queries run concurrently — reduces total latency |
| Traversal retry via prompting | Max 3 retries for failed SQL/Python — error + query passed back to agent for self-correction |
| Semantic context injection | KPI definitions, question bank, and RCA scenarios injected into Planner and Traversal prompts |
