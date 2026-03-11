"""
Streamlit chatbot UI for the RCA Agent.

Run backend first:  uvicorn app.main:app --port 8000 --timeout-keep-alive 300
Run UI:             streamlit run streamlit_app.py
"""
import uuid
import time

import streamlit as st
import requests

API_BASE = "http://localhost:8000/api/v1"

st.set_page_config(
    page_title="RCA Agent",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Session state init ──
if "messages" not in st.session_state:
    st.session_state.messages = []
if "health_checked" not in st.session_state:
    st.session_state.health_checked = False
if "health_data" not in st.session_state:
    st.session_state.health_data = None
if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid.uuid4())
if "pending_clarification" not in st.session_state:
    st.session_state.pending_clarification = None
if "user_id" not in st.session_state:
    st.session_state.user_id = "dev-user"


# ── Helpers ──

def _status_badge(status: str) -> str:
    return "🟢" if status == "connected" else "🔴"


def _fetch_health() -> dict | None:
    try:
        r = requests.get(f"{API_BASE}/health", timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def _run_analysis(query: str) -> dict:
    r = requests.post(
        f"{API_BASE}/analyze",
        json={
            "user_id": st.session_state.user_id,
            "query": query,
            "thread_id": st.session_state.thread_id,
        },
        timeout=300,
    )
    r.raise_for_status()
    return r.json()


def _resume_analysis(clarification: str) -> dict:
    r = requests.post(
        f"{API_BASE}/analyze/resume",
        json={
            "thread_id": st.session_state.thread_id,
            "clarification": clarification,
        },
        timeout=300,
    )
    r.raise_for_status()
    return r.json()


def _render_response_meta(meta: dict):
    if meta.get("errors"):
        with st.expander("Errors", expanded=True):
            for err in meta["errors"]:
                st.warning(err)

    if meta.get("planner_steps"):
        label = f"Investigation Plan — {len(meta['planner_steps'])} steps"
        with st.expander(label, expanded=False):
            for i, step in enumerate(meta["planner_steps"], 1):
                display = step.split(": ", 1)[1] if ": " in step else step
                st.markdown(f"**Step {i}:** {display}")

    parts = []
    if meta.get("traversal_steps"):
        parts.append(f"{meta['traversal_steps']} tool call(s)")
    if meta.get("routing_decision"):
        parts.append(f"route: {meta['routing_decision']}")
    if meta.get("elapsed_s") is not None:
        parts.append(f"answered in {meta['elapsed_s']}s")
    if parts:
        st.caption(" · ".join(parts))


def _handle_api_response(data: dict, elapsed_s: float):
    status = data.get("status", "complete")

    if status == "clarification_needed":
        clarification = data.get("clarification", {})
        st.session_state.pending_clarification = clarification

        questions = clarification.get("questions", [])
        assumptions = clarification.get("assumptions_if_skipped", [])
        message_txt = clarification.get("message", "Please clarify your query.")

        lines = [f"**{message_txt}**\n"]
        for i, q in enumerate(questions, 1):
            lines.append(f"{i}. {q}")
        if assumptions:
            lines.append("\n*If you skip, I'll assume:*")
            for a in assumptions:
                lines.append(f"- {a}")
        content = "\n".join(lines)
        st.markdown(content)

        st.session_state.messages.append({
            "role": "assistant",
            "content": content,
            "meta": {"is_clarification": True},
        })
        return

    st.session_state.pending_clarification = None

    final_response = data.get("final_response", "").strip()
    if not final_response:
        final_response = "_The agent did not produce a response. Check the backend logs._"

    st.markdown(final_response)

    meta = {
        "errors": data.get("errors", []),
        "traversal_steps": data.get("traversal_steps", 0),
        "elapsed_s": elapsed_s,
        "routing_decision": data.get("routing_decision", ""),
        "planner_steps": data.get("planner_steps", []),
        "is_clarification": False,
    }
    _render_response_meta(meta)

    st.session_state.messages.append({
        "role": "assistant",
        "content": final_response,
        "meta": meta,
    })

    st.session_state.thread_id = str(uuid.uuid4())


# ── Sidebar ──
with st.sidebar:
    st.markdown("## 🔍 RCA Agent")
    st.caption("Root Cause Analysis · Neo4j + PostgreSQL + LLM")
    st.divider()

    user_id_input = st.text_input(
        "User ID",
        value=st.session_state.user_id,
        placeholder="e.g. user-001",
    )
    if user_id_input != st.session_state.user_id:
        st.session_state.user_id = user_id_input
    st.divider()

    # ── Health check ──
    if not st.session_state.health_checked:
        st.session_state.health_data = _fetch_health()
        st.session_state.health_checked = True

    h = st.session_state.health_data

    if h and "error" not in h:
        services = h.get("services", {})
        neo4j_s = services.get("neo4j", {})
        pg_s = services.get("postgres", {})
        openai_s = services.get("openai", {})

        col1, col2, col3 = st.columns(3)
        col1.markdown(f"{_status_badge(neo4j_s.get('status', ''))} Neo4j")
        col2.markdown(f"{_status_badge(pg_s.get('status', ''))} PG")
        col3.markdown(f"{_status_badge(openai_s.get('status', ''))} LLM")

        overall = h.get("status", "degraded")
        if overall == "ok":
            st.success("All services connected")
        else:
            st.warning("One or more services unavailable")
    elif h and "error" in h:
        st.error("Backend unreachable — start the API server first")
    else:
        st.info("Checking services...")

    if st.button("Refresh", use_container_width=True):
        st.session_state.health_data = _fetch_health()
        st.rerun()

    st.divider()

    if st.button("Clear chat", use_container_width=True):
        st.session_state.messages = []
        st.session_state.pending_clarification = None
        st.session_state.thread_id = str(uuid.uuid4())
        st.rerun()

    st.divider()
    st.caption(f"Thread: `{st.session_state.thread_id[:8]}...`")


# ── Main chat area ──
st.markdown("## RCA Agent — Root Cause Analysis")
st.caption("Investigate delays, SLA breaches, compliance issues, vendor performance, and more.")

if not st.session_state.user_id:
    st.info("Enter a User ID in the sidebar to start.")
    st.stop()

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg["role"] == "assistant" and msg.get("meta") and not msg["meta"].get("is_clarification"):
            _render_response_meta(msg["meta"])

# ── HITL clarification input ──
if st.session_state.pending_clarification:
    st.info("The agent needs more detail before running the investigation.")

    with st.form("clarification_form", clear_on_submit=True):
        clarification_text = st.text_area(
            "Your answer (or leave blank to accept stated assumptions):",
            placeholder="e.g. Last 90 days, all regions, focus on Civil SLA",
            height=80,
        )
        submitted = st.form_submit_button("Submit & Continue")

    if submitted:
        answer = clarification_text.strip() or "Accept stated assumptions"
        st.session_state.messages.append({"role": "user", "content": answer})

        with st.chat_message("assistant"):
            placeholder = st.empty()
            placeholder.markdown("_Resuming investigation..._")
            try:
                with st.spinner("Running RCA..."):
                    t0 = time.perf_counter()
                    data = _resume_analysis(answer)
                    elapsed_s = round(time.perf_counter() - t0, 1)
                placeholder.empty()
                _handle_api_response(data, elapsed_s)
            except requests.HTTPError as e:
                placeholder.error(f"API error ({e.response.status_code}): {e.response.text}")
            except Exception as e:
                placeholder.error(f"Could not reach the API: {e}")

# ── Normal chat input ──
elif prompt := st.chat_input(
    "Ask about delays, SLA breaches, compliance, vendor performance...",
):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        placeholder = st.empty()
        placeholder.markdown("_Investigating..._")

        try:
            with st.spinner("Running RCA..."):
                t0 = time.perf_counter()
                data = _run_analysis(prompt)
                elapsed_s = round(time.perf_counter() - t0, 1)
            placeholder.empty()
            _handle_api_response(data, elapsed_s)

        except requests.HTTPError as e:
            error_text = f"API error ({e.response.status_code}): {e.response.text}"
            placeholder.error(error_text)
            st.session_state.messages.append({"role": "assistant", "content": error_text, "meta": {}})

        except Exception as e:
            error_text = f"Could not reach the API: {e}"
            placeholder.error(error_text)
            st.session_state.messages.append({"role": "assistant", "content": error_text, "meta": {}})
