"""
Python Sandbox tool for executing computation code safely.
Used by the Traversal Agent and Response Agent for calculations.
"""
from __future__ import annotations

import ast
import time
import logging
import traceback
from typing import Any
from io import StringIO
import contextlib
import math
import json
import statistics

import psycopg2
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
import concurrent.futures
import config

logger = logging.getLogger(__name__)

# Allowed built-in modules for the sandbox
SAFE_MODULES = {
    "math": math,
    "json": json,
    "statistics": statistics,
    "numpy": np,
    "pandas": pd,
}

# Blocked built-in functions
BLOCKED_BUILTINS = {
    "exec", "eval", "compile", "open",
    "breakpoint", "exit", "quit",
}

# Modules allowed at runtime via import statements
ALLOWED_IMPORT_MODULES = {
    *SAFE_MODULES.keys(),
    "collections", "datetime", "itertools", "functools",
}


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    """
    Restricted __import__ that only allows whitelisted modules.
    This replaces the real __import__ in the sandbox so that
    `import json`, `from datetime import date`, etc. work at runtime
    while blocking arbitrary module imports.
    """
    top_level = name.split(".")[0]
    if top_level not in ALLOWED_IMPORT_MODULES:
        raise ImportError(f"Import of '{name}' is not allowed in sandbox.")
    return __builtins__["__import__"](name, globals, locals, fromlist, level) \
        if isinstance(__builtins__, dict) \
        else __builtins__.__dict__["__import__"](name, globals, locals, fromlist, level)


def _validate_code(code: str) -> tuple[bool, str]:
    """
    Static analysis to reject dangerous code patterns.
    Returns (is_safe, reason).
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"Syntax error: {e}"

    for node in ast.walk(tree):
        # Block imports except whitelisted
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = ""
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module.split(".")[0]
            elif isinstance(node, ast.Import):
                module = node.names[0].name.split(".")[0]

            if module not in ALLOWED_IMPORT_MODULES:
                return False, f"Import of '{module}' is not allowed in sandbox."

        # Block attribute access to dunder methods (except __init__, __str__, __repr__)
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr not in ("__init__", "__str__", "__repr__", "__len__"):
                return False, f"Access to '{node.attr}' is not allowed."

    return True, "OK"


def execute_python(code: str, context: dict[str, Any] | None = None) -> dict:
    """
    Execute Python code in a restricted sandbox.

    Args:
        code: Python code string
        context: Variables to inject into the execution namespace

    Returns:
        dict with status, output (stdout), result (last expression), error
    """
    is_safe, reason = _validate_code(code)
    if not is_safe:
        return {
            "status": "error",
            "error": f"Code validation failed: {reason}",
            "output": "",
            "result": None,
        }

    # Build restricted globals — keep __import__ but swap it for the safe version
    builtins_dict = __builtins__.__dict__ if hasattr(__builtins__, "__dict__") else __builtins__
    safe_builtins = {
        k: v for k, v in builtins_dict.items()
        if k not in BLOCKED_BUILTINS
    }
    safe_builtins["__import__"] = _safe_import

    namespace = {
        "__builtins__": safe_builtins,
        **SAFE_MODULES,
        # Common aliases — pre-injected so LLM doesn't need import statements
        "np": np,
        "pd": pd,
    }

    # Inject context variables (e.g., data from previous steps)
    if context:
        namespace.update(context)

    # Capture stdout
    stdout_capture = StringIO()
    start = time.perf_counter()

    try:
        # If last line is a bare expression (not assignment), auto-capture it as result
        lines = code.strip().splitlines()
        last_line = lines[-1].strip() if lines else ""
        auto_capture = False
        if last_line and not any(last_line.startswith(k) for k in ("result", "#", "print", "import", "from", "if ", "for ", "while ", "def ", "class ", "return", "try", "except", "with ")):
            try:
                ast.parse(last_line, mode="eval")
                auto_capture = True
            except SyntaxError:
                pass

        with contextlib.redirect_stdout(stdout_capture):
            exec(code, namespace)

        elapsed_ms = (time.perf_counter() - start) * 1000

        # Try to extract a 'result' variable if set by the code
        result = namespace.get("result", None)

        # Auto-capture: if result was never set, evaluate the last expression
        if result is None and auto_capture:
            try:
                result = eval(last_line, namespace)  # noqa: S307
            except Exception:
                pass

        # Last resort: use stdout if nothing else captured
        if result is None and stdout_capture.getvalue().strip():
            result = stdout_capture.getvalue().strip()

        return {
            "status": "success",
            "output": stdout_capture.getvalue(),
            "result": result,
            "elapsed_ms": round(elapsed_ms, 2),
        }
    except Exception as e:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
            "output": stdout_capture.getvalue(),
            "result": None,
            "elapsed_ms": round(elapsed_ms, 2),
        }


# ── PostgreSQL-backed sandbox ────────────────────────────────────────────────

class PythonSandbox:
    """
    PostgreSQL-backed execution sandbox.

    Provides `conn` (psycopg2, read-only), `pd`, `np`, `go`, `px`, `json`
    in the execution namespace. User code sets a `result` dict to return data.
    """

    def __init__(self):
        self.conn = None
        self.session_vars = {}
        self._connect()

    def _connect(self):
        """Lazily connect to Postgres. Gracefully handles missing DB."""
        if self.conn is not None:
            return
        try:
            self.conn = psycopg2.connect(
                host=config.PG_HOST,
                port=config.PG_PORT,
                database=config.PG_DATABASE,
                user=config.PG_USER,
                password=config.PG_PASSWORD,
                options="-c default_transaction_read_only=on",
            )
            self.conn.autocommit = True
        except Exception as e:
            print(f"⚠ Postgres not available: {e}")
            self.conn = None

    def _is_raw_sql(self, code: str) -> bool:
        """Detect if code is raw SQL rather than Python."""
        first_line = code.strip().split("\n")[0].strip().rstrip(";").upper()
        sql_starts = ("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "WITH ", "EXPLAIN ")
        return first_line.startswith(sql_starts)

    def execute(self, code: str, timeout_seconds: int = 30) -> dict:
        if self.conn is None:
            self._connect()

        # Auto-wrap raw SQL in pd.read_sql() so exec() doesn't choke on it
        if self._is_raw_sql(code):
            escaped = code.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
            code = f'result = pd.read_sql("""{escaped}""", conn).to_dict(orient="records")'

        def _execute_query(sql, params=None, db=None, max_rows=None):
            """Helper: run SQL and return list[dict] (not a DataFrame)."""
            df = pd.read_sql(sql, self.conn, params=params)
            if max_rows is not None:
                df = df.head(max_rows)
            return df.to_dict(orient="records")

        # ── Deterministic scenario-execution helpers ────────────────────────
        # These let a scenario's `scn_python_function` orchestrator chain its
        # contributing KPI/transform nodes with NO LLM in the loop — the
        # consistency path. Fetched fresh from Neo4j on each call so the graph
        # stays the single source of truth.

        def _fetch_node_source(node_id, field):
            """Fetch a stored function string for a node from Neo4j (or '' if absent)."""
            from tools.neo4j_tool import Neo4jTool
            out = Neo4jTool().run_cypher_safe(
                f"MATCH (n:BKGNode {{node_id: $nid}}) RETURN coalesce(n.{field}, '') AS fn",
                {"nid": node_id},
            )
            records = (out.get("records") or out.get("results") or []) if isinstance(out, dict) else []
            return (records[0].get("fn") if records else "") or ""

        def _pick_callable(ns, prefixes):
            """Return the first top-level user function in `ns` whose name starts with
            one of `prefixes`; fall back to the first non-dunder user function."""
            import types
            funcs = {k: v for k, v in ns.items()
                     if isinstance(v, types.FunctionType) and not k.startswith("__")}
            for p in prefixes:
                for k, v in funcs.items():
                    if k.startswith(p):
                        return v
            return next(iter(funcs.values())) if funcs else None

        def run_node(node_id, filter=None, group_by=None):
            """Deterministically execute a KPI/core node's stored function with the
            SAME execute_query this sandbox exposes — NO LLM, NO GROUP BY stripping.

            Fetches kpi_python_function (or map_python_function for core nodes),
            exec's it, locates the get_* callable, merges `group_by` into the filter
            dict, and calls fn(execute_query, filters=<merged>). Returns list[dict].
            """
            fn_src = _fetch_node_source(node_id, "kpi_python_function")
            if not fn_src:
                fn_src = _fetch_node_source(node_id, "map_python_function")
            if not fn_src:
                raise RuntimeError(f"Node '{node_id}' has no kpi_python_function/map_python_function.")
            local_ns = {}
            exec(fn_src, local_ns)  # noqa: S102
            fn = _pick_callable(local_ns, ("get_",))
            if fn is None:
                raise RuntimeError(f"No get_* callable found in node '{node_id}' function body.")
            merged = dict(filter or {})
            if group_by is not None:
                merged["group_by"] = group_by
            return fn(_execute_query, merged)

        def run_transform(node_id, *args, **kwargs):
            """Execute a pure-transform node (e.g. a predictor) — calls the node's
            stored function with the given args/kwargs, WITHOUT execute_query."""
            fn_src = _fetch_node_source(node_id, "kpi_python_function")
            if not fn_src:
                raise RuntimeError(f"Transform node '{node_id}' has no kpi_python_function.")
            local_ns = {}
            exec(fn_src, local_ns)  # noqa: S102
            fn = _pick_callable(local_ns, ("predict_", "transform_", "compute_"))
            if fn is None:
                raise RuntimeError(f"No transform callable found in node '{node_id}' function body.")
            return fn(*args, **kwargs)

        def run_scenario(scenario_id, filter=None, group_by=None):
            """Run a scenario node's deterministic orchestrator (scn_python_function),
            passing the run_node + run_transform helpers so it can chain the
            contributing nodes with NO LLM in the loop."""
            fn_src = _fetch_node_source(scenario_id, "scn_python_function")
            if not fn_src:
                raise RuntimeError(f"Scenario '{scenario_id}' has no scn_python_function.")
            local_ns = {}
            exec(fn_src, local_ns)  # noqa: S102
            fn = _pick_callable(local_ns, ("run_", "scenario_"))
            if fn is None:
                raise RuntimeError(f"No run_* orchestrator found in scenario '{scenario_id}'.")
            # Expose the node runners AND execute_query as globals so an orchestrator can
            # call them directly. execute_query lets a scenario that needs granularity a
            # single node can't express (e.g. month-wise + delay-code RCA over a join) run
            # one deterministic SQL against the same read-only connection — still no LLM.
            local_ns["run_node"] = run_node
            local_ns["run_transform"] = run_transform
            local_ns["execute_query"] = _execute_query
            return fn(run_node, run_transform, filter=filter, group_by=group_by)

        namespace = {
            "conn": self.conn,
            "pd": pd,
            "np": np,
            "go": go,
            "px": px,
            "json": json,
            "execute_query": _execute_query,
            "run_node": run_node,
            "run_transform": run_transform,
            "run_scenario": run_scenario,
            "session": self.session_vars,
            "result": None,
        }

        # Detect if last line is a bare expression (auto-capture as result)
        lines = code.strip().splitlines()
        last_line = lines[-1].strip() if lines else ""
        auto_capture = False
        if last_line and not any(last_line.startswith(k) for k in ("result", "#", "print", "import", "from", "if ", "for ", "while ", "def ", "class ", "return", "try", "except", "with ")):
            try:
                ast.parse(last_line, mode="eval")
                auto_capture = True
            except SyntaxError:
                pass

        def _run():
            exec(code, namespace)  # noqa: S102
            return namespace

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_run)
                try:
                    result_ns = future.result(timeout=timeout_seconds)
                except concurrent.futures.TimeoutError:
                    raise TimeoutError(
                        f"Execution timed out after {timeout_seconds}s"
                    )

            if "session" in result_ns:
                self.session_vars = result_ns["session"]

            result = result_ns.get("result", None)

            # Auto-capture: if result was never set, evaluate the last expression
            if result is None and auto_capture:
                try:
                    result = eval(last_line, result_ns)  # noqa: S307
                except Exception:
                    pass

            # Handle result being a DataFrame, list, or other non-dict type
            if isinstance(result, pd.DataFrame):
                result = result.to_dict(orient="records")
            elif isinstance(result, dict):
                for key, val in list(result.items()):
                    if isinstance(val, pd.DataFrame):
                        result[key] = val.to_dict(orient="records")
            elif result is None:
                result = {}
            return {"status": "success", "result": result}

        except Exception as e:
            return {
                "status": "error",
                "error": str(e)
            }

    def close(self):
        if self.conn:
            self.conn.close()
