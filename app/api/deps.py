"""
Authentication dependencies for the v1 API.

Every route is gated by HTTP Basic credentials supplied in the standard
``Authorization: Basic <base64(username:password)>`` header.

Two routes are loaded directly by the browser rather than by an HTTP client —
the SSE stream (``EventSource``) and the chart preview page (``<iframe>`` /
new tab). Neither API lets the caller set request headers, so those two
additionally accept the same base64 blob as a ``?token=`` query parameter.
Every other route is header-only.

Credentials come from ``API_USERNAME`` / ``API_PASSWORD``. If either is unset
the dependency fails **closed** — an unconfigured deployment rejects all
requests rather than silently serving the API without authentication.
"""
from __future__ import annotations

import base64
import binascii
import logging
import secrets
from typing import Optional

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import config

logger = logging.getLogger(__name__)

# A single shared instance — the body is identical for every rejection so the
# response never reveals *why* it failed (missing vs malformed vs wrong).
_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Unauthorized",
    headers={"WWW-Authenticate": 'Basic realm="RCA Agent API"'},
)

# auto_error=False so a missing header reaches our own handler and produces the
# same generic 401 as a wrong password, instead of FastAPI's default 403 body.
_basic = HTTPBasic(auto_error=False)


def _credentials_valid(username: str, password: str) -> bool:
    """
    Compare supplied credentials against the configured pair.

    Both halves are compared with `secrets.compare_digest` and neither
    comparison is short-circuited, so response timing does not leak whether
    the username alone was correct.
    """
    expected_user = config.API_USERNAME
    expected_pass = config.API_PASSWORD

    if not expected_user or not expected_pass:
        logger.error(
            "API_USERNAME / API_PASSWORD are not configured — denying all requests"
        )
        return False

    user_ok = secrets.compare_digest(username.encode("utf-8"), expected_user.encode("utf-8"))
    pass_ok = secrets.compare_digest(password.encode("utf-8"), expected_pass.encode("utf-8"))
    return user_ok and pass_ok


def require_auth(
    credentials: Optional[HTTPBasicCredentials] = Depends(_basic),
) -> str:
    """Header-only gate. Returns the authenticated username."""
    if credentials is None or not _credentials_valid(
        credentials.username, credentials.password
    ):
        raise _UNAUTHORIZED
    return credentials.username


def require_auth_or_token(
    credentials: Optional[HTTPBasicCredentials] = Depends(_basic),
    token: Optional[str] = Query(
        None,
        description=(
            "base64(username:password) — only for browser-loaded routes "
            "(EventSource / iframe) that cannot set an Authorization header."
        ),
    ),
) -> str:
    """
    Gate for browser-loaded routes: accepts the Authorization header, and
    falls back to an equivalent `?token=` query parameter.

    The token value is scrubbed from access logs by the log filter installed
    in `main.py`, so it does not land in log files.
    """
    if credentials is not None and _credentials_valid(
        credentials.username, credentials.password
    ):
        return credentials.username

    if token:
        try:
            decoded = base64.b64decode(token, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            raise _UNAUTHORIZED
        username, sep, password = decoded.partition(":")
        if sep and _credentials_valid(username, password):
            return username

    raise _UNAUTHORIZED
