"""
Semantic search service — calls the internal PM Copilot semantic search API
to retrieve relevant context from KPI, question_bank, rca, and keywords tables.

API endpoints:
    POST /api/v1/semantic/search           — kpi, question_bank, rca
    POST /api/v1/semantic/keywords/search  — keywords

Note: Only accessible within the company network.

Request body:
    { "query": str, "table": "kpi"|"question_bank"|"rca", "top_k": int }
    { "query": str, "top_k": int }  (keywords)

Response (200):
    {
        "query": str,
        "results": [
            { "table": str, "id": int, "content": {...}, "similarity_score": float }
        ]
    }
"""
from __future__ import annotations

import logging
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

_TABLES = ("kpi", "question_bank", "rca")
_DEFAULT_TOP_K = 1
_TABLE_TOP_K: dict[str, int] = {
    "kpi":           10,
    "question_bank": 10,
    "rca":           1,
    "keywords":      10,
}
_REQUEST_TIMEOUT = 15  # seconds

# Known structured keys inside the rca table's content dict
_RCA_CONTENT_KEYS: dict[str, str] = {
    "question_id": "Question ID",
    "question":    "Question",
    "sql":         "SQL",
}


class SemanticService:
    """
    Client for the internal PM Copilot semantic search API.

    Queries kpi, question_bank, and rca tables and formats
    the results as structured context strings for the traversal and
    response agents. Gracefully degrades when the API is unreachable
    (e.g., outside the company network).
    """

    def __init__(self):
        self._base_url = config.SEMANTIC_SEARCH_URL.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({
            "accept": "application/json",
            "Content-Type": "application/json",
        })

    # ── Low-level API call ─────────────────────────────────────────────────

    def _search(self, query: str, table: str, top_k: int = _DEFAULT_TOP_K) -> list[dict]:
        """
        Call the semantic search API for a single table.
        Returns an empty list on any error so the agent can proceed without context.
        """
        url = f"{self._base_url}/api/v1/semantic/search"
        payload = {"query": query, "table": table, "top_k": top_k}

        try:
            resp = self._session.post(url, json=payload, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            results: list[dict] = resp.json().get("results", [])
            logger.info(
                "Semantic search [%s]: %d result(s) for query: %.80s",
                table, len(results), query,
            )
            return results

        except requests.exceptions.ConnectionError:
            logger.warning(
                "Semantic search [%s]: Cannot reach %s — are you on the company network?",
                table, self._base_url,
            )
        except requests.exceptions.Timeout:
            logger.warning(
                "Semantic search [%s]: Request timed out after %ds", table, _REQUEST_TIMEOUT
            )
        except requests.exceptions.HTTPError as exc:
            logger.warning("Semantic search [%s]: HTTP error — %s", table, exc)
        except Exception as exc:
            logger.warning("Semantic search [%s]: Unexpected error — %s", table, exc)

        return []

    def _search_keywords(self, query: str, top_k: int = _DEFAULT_TOP_K) -> list[dict]:
        """
        Call the keywords semantic search API (separate endpoint).
        Returns an empty list on any error.
        """
        url = f"{self._base_url}/api/v1/semantic/keywords/search"
        payload = {"query": query, "top_k": top_k}

        try:
            resp = self._session.post(url, json=payload, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            results: list[dict] = resp.json().get("results", [])
            logger.info(
                "Semantic search [keywords]: %d result(s) for query: %.80s",
                len(results), query,
            )
            return results

        except requests.exceptions.ConnectionError:
            logger.warning(
                "Semantic search [keywords]: Cannot reach %s — are you on the company network?",
                self._base_url,
            )
        except requests.exceptions.Timeout:
            logger.warning(
                "Semantic search [keywords]: Request timed out after %ds", _REQUEST_TIMEOUT
            )
        except requests.exceptions.HTTPError as exc:
            logger.warning("Semantic search [keywords]: HTTP error — %s", exc)
        except Exception as exc:
            logger.warning("Semantic search [keywords]: Unexpected error — %s", exc)

        return []

    # ── High-level: query all tables ───────────────────────────────────────

    def get_all_context(
        self,
        query: str,
        top_k: int = _DEFAULT_TOP_K,
    ) -> dict[str, list[dict]]:
        """
        Query kpi, question_bank, rca, and keywords tables concurrently.

        Returns:
            {
                "kpi":           [...],
                "question_bank": [...],
                "rca":           [...],
                "keywords":      [...],
            }
        Each list contains result dicts from the API (may be empty on error).
        """
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(_TABLES) + 1) as executor:
            futures = {
                table: executor.submit(
                    self._search, query, table, _TABLE_TOP_K.get(table, top_k),
                )
                for table in _TABLES
            }
            futures["keywords"] = executor.submit(
                self._search_keywords, query, _TABLE_TOP_K.get("keywords", top_k),
            )
        return {table: fut.result() for table, fut in futures.items()}

    # ── Context formatting ─────────────────────────────────────────────────

    def format_traversal_context(self, context: dict[str, list[dict]]) -> str:
        """
        Format all semantic search results into a structured context block
        to be injected into the Traversal Agent's system prompt.

        Sections: KPI context → Question Bank examples → RCA scenarios.
        Returns an empty string when no results are available.
        """
        kpi_results = context.get("kpi", [])
        qb_results  = context.get("question_bank", [])
        sim_results = context.get("rca", [])
        kw_results  = context.get("keywords", [])

        if not any([kpi_results, qb_results, sim_results, kw_results]):
            return ""

        lines: list[str] = [
            "## Semantic Context (from Internal Knowledge Base)",
            "The following was retrieved via semantic similarity search against "
            "the user's query. Use it to guide your data retrieval strategy.",
            "",
        ]

        # ── KPI section ──
        if kpi_results:
            lines.append("### Relevant KPIs")
            for r in kpi_results:
                score = f"{r.get('similarity_score', 0) * 100:.1f}%"
                lines.append(f"**KPI #{r.get('id', '?')}** (similarity: {score})")
                for k, v in (r.get("content") or {}).items():
                    if v:
                        lines.append(f"  - **{k}**: {v}")
                lines.append("")

        # ── Question Bank section ──
        if qb_results:
            lines.append("### Relevant Questions from Knowledge Base")
            lines.append(
                "These pre-answered questions are semantically similar to the user's query. "
                "Use them to understand expected data shape and calculations."
            )
            lines.append("")
            for r in qb_results:
                score = f"{r.get('similarity_score', 0) * 100:.1f}%"
                lines.append(f"**Q&A #{r.get('id', '?')}** (similarity: {score})")
                for k, v in (r.get("content") or {}).items():
                    if v:
                        lines.append(f"  - **{k}**: {v}")
                lines.append("")

        # ── RCA section ──
        if sim_results:
            lines.append("### Matched RCA Scenarios")
            lines.append(
                "These pre-defined RCA questions closely match the query. "
                "Use the associated SQL and question context to guide your "
                "data retrieval strategy."
            )
            lines.append("")
            for i, r in enumerate(sim_results, 1):
                score = f"{r.get('similarity_score', 0) * 100:.1f}%"
                content: dict[str, Any] = r.get("content") or {}
                q_id = content.get("question_id", r.get("id", "?"))
                lines.append(
                    f"**RCA {i} — Question ID {q_id}** (similarity: {score})"
                )
                # Only render question_id, question, and sql
                for key, label in _RCA_CONTENT_KEYS.items():
                    val = content.get(key)
                    if not val:
                        continue
                    if isinstance(val, list):
                        lines.append(f"  **{label}**:")
                        for item in val:
                            if str(item).strip():
                                lines.append(f"    - {item}")
                    else:
                        lines.append(f"  **{label}**: {val}")

                lines.append("")

        # ── Keywords section ──
        if kw_results:
            lines.append("### Relevant Keywords")
            lines.append(
                "These domain keywords match the user's query. "
                "Use their mapped table columns and logic to guide data retrieval."
            )
            lines.append("")
            for r in kw_results:
                score = f"{r.get('similarity_score', 0) * 100:.1f}%"
                content: dict[str, Any] = r.get("content") or {}
                kw_name = content.get("keyword_name", f"#{r.get('id', '?')}")
                lines.append(f"**{kw_name}** (similarity: {score})")
                if content.get("keyword_description"):
                    lines.append(f"  - **Description**: {content['keyword_description']}")
                if content.get("mapped_table_columns"):
                    lines.append(f"  - **Mapped Tables/Columns**: {content['mapped_table_columns']}")
                if content.get("logic"):
                    lines.append(f"  - **Logic**: {content['logic']}")
                if content.get("synonyms"):
                    lines.append(f"  - **Synonyms**: {content['synonyms']}")
                lines.append("")

        lines.append("─" * 60)
        return "\n".join(lines)

    def format_rca_guidance(self, context: dict[str, list[dict]]) -> str:
        """
        Extract RCA guidance (question and SQL) from the best-matched
        RCA result.

        This is passed to the Response Agent so it knows how to structure
        calculations and the final output. Returns empty string if no match.
        """
        rca_results = context.get("rca", [])
        if not rca_results:
            return ""

        best    = rca_results[0]  # highest similarity
        content = best.get("content") or {}
        score   = f"{best.get('similarity_score', 0) * 100:.1f}%"
        q_id    = content.get("question_id", best.get("id", "?"))

        lines: list[str] = [
            "## Matched RCA — Guidance (Reference Only)",
            f"*Question ID {q_id} · Similarity {score}*",
            "",
        ]

        question: str = content.get("question", "")
        if question:
            lines.append(f"### Question")
            lines.append(question)
            lines.append("")

        sql: str = content.get("sql", "")
        if sql:
            lines.append("### SQL")
            lines.append("*(Adapt to what was actually retrieved)*")
            lines.append(f"```sql\n{sql}\n```")
            lines.append("")

        return "\n".join(lines)
