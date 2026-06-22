"""
Safe JSON serialization shared by the DB persistence layer and the SSE stream.

Both paths emit the same heavy RCA payloads (analysis, charts, traces), which
can contain values that a vanilla ``json.dumps`` mishandles:

  • Exotic types — ``Decimal`` from SQL NUMERIC columns, ``datetime``,
    ``numpy``/``pandas`` scalars returned by the python sandbox — raise
    ``TypeError``.
  • ``NaN`` / ``Infinity`` floats — emitted as the literal tokens
    ``NaN``/``Infinity``, which PostgreSQL JSONB rejects and the browser's
    ``JSON.parse()`` throws on.

If the SSE stream and the DB write used different serializers, a payload that
serializes for one but not the other would be delivered live yet missing on a
``get_messages`` refresh (or vice-versa). Routing BOTH through ``safe_dumps``
guarantees the live stream and the refresh can never disagree.

  sanitize_json()  — strips NaN/Infinity floats to None (recursively).
  safe_dumps()     — sanitize_json() + ``default=str`` fallback for any
                     remaining non-JSON-native type.
"""
from __future__ import annotations

import json
import math
from typing import Any


def sanitize_json(value: Any) -> Any:
    """
    Recursively replace NaN/Infinity floats with None so the result is valid
    JSON for both PostgreSQL JSONB and the browser's ``JSON.parse()``.

    ``numpy.float64`` subclasses ``float``, so NaN values produced by the
    sandbox's pandas/numpy work are caught here too.
    """
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {k: sanitize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json(item) for item in value]
    return value


def safe_dumps(value: Any, **kwargs: Any) -> str:
    """
    ``json.dumps`` that tolerates the full range of values the RCA agent
    produces: NaN/Infinity are stripped to null, and any non-JSON-native type
    (Decimal, datetime, numpy/pandas scalars, …) falls back to ``str()``.
    """
    kwargs.setdefault("default", str)
    return json.dumps(sanitize_json(value), **kwargs)
