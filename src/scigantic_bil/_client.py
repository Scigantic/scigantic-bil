"""Shared HTTP plumbing: one lazily-built requests.Session, retry with
backoff on transient failures, and the two BIL hosts this package talks to.

BIL has no documented rate limit and no throttling header (checked live
2026-09-08: responses carry only nginx defaults), so unlike
scigantic-pubchem there is no token bucket here. Retries cover 429/5xx and
connection errors only; a 404 is a real answer (a path that does not exist)
and is raised as BilNotFoundError immediately.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests

from ._version import __version__

API_BASE = "https://api.brainimagelibrary.org"
DOWNLOAD_BASE = "https://download.brainimagelibrary.org"

_USER_AGENT = f"scigantic-bil/{__version__} (+https://scigantic.com; mailto:support@scigantic.com)"

_MAX_RETRIES = 4
_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}

_session: requests.Session | None = None
_session_lock = threading.Lock()


class BilError(Exception):
    """Raised for an HTTP error after retries are exhausted, or for an API
    response whose envelope reports failure."""


class BilNotFoundError(BilError):
    """Raised for a 404: a dataset id, directory or file that does not
    exist. A real outcome, not a transient failure, so never retried."""


def get_session() -> requests.Session:
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                _session = requests.Session()
                _session.headers["User-Agent"] = _USER_AGENT
    return _session


def send(
    method: str,
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    stream: bool = False,
    timeout: float = 60.0,
) -> requests.Response:
    """Issue one request with retry on 429/5xx and connection errors.
    Raises BilNotFoundError on 404 and BilError on any other failure."""
    session = get_session()
    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = session.request(
                method, url, params=params, headers=headers, stream=stream, timeout=timeout
            )
        except requests.RequestException as exc:
            last_error = exc
            if attempt == _MAX_RETRIES:
                break
            time.sleep(1.5 * (2**attempt))
            continue
        if resp.status_code == 404:
            resp.close()
            raise BilNotFoundError(f"404 for {resp.url}")
        if resp.status_code in _RETRY_STATUS_CODES and attempt < _MAX_RETRIES:
            resp.close()
            time.sleep(1.5 * (2**attempt))
            continue
        if resp.status_code >= 400:
            body = resp.text[:300]
            resp.close()
            raise BilError(f"HTTP {resp.status_code} for {resp.url}: {body}")
        return resp
    raise BilError(f"request to {url} failed after {_MAX_RETRIES + 1} attempts: {last_error}")
