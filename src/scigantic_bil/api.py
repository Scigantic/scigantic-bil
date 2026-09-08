"""Thin, typed wrappers over BIL's metadata API (api.brainimagelibrary.org).

Two endpoints, both open, no key (verified 2026-09-08):

- ``/query/<division>?<element>=<value>`` returns the BIL ids whose
  metadata matches. Divisions seen live: submission, contributors,
  funders, publication, dataset, specimen, instrument, image, fulltext.
  Matching is exact for structured fields (``instrument?microscopetype=
  Light-sheet`` matched 6 datasets while ``fulltext?text=light sheet``
  matched 777), so prefer fulltext() for discovery and the inventory
  (catalog.py) for structured filters.
- ``/retrieve?bildid=<id>`` returns the full record; a POST with
  ``{"bildids": [...]}`` returns many at once.

Every success envelope carries ``success: "true"`` (a string); a miss is an
HTTP 404 with ``success: "false"`` and a message, which is surfaced as
BilNotFoundError rather than an empty result.
"""

from __future__ import annotations

from typing import Any

from . import cache
from ._client import API_BASE, BilError, BilNotFoundError, send
from .models import DatasetDetail

_RETRIEVE_BATCH = 100


def query(division: str, **element: str) -> list[str]:
    """BIL ids matching one ``element=value`` pair in ``division``.
    Example: ``query("specimen", species="mouse")``."""
    if len(element) != 1:
        raise ValueError("query() takes exactly one element=value keyword argument")
    url = f"{API_BASE}/query/{division}"
    params = {k: v for k, v in element.items()}
    cached = cache.get("query", url, params)
    if cached is not None:
        return list(cached)
    try:
        resp = send("GET", url, params=params, timeout=180.0)
    except BilNotFoundError as exc:
        raise BilError(f"unknown query division or element: {division}?{params}") from exc
    body = resp.json()
    if str(body.get("success", "")).lower() != "true":
        raise BilError(f"BIL query failed: {body.get('message', body)}")
    ids = [str(b) for b in body.get("bildids", [])]
    cache.put("query", url, params, ids)
    return ids


def fulltext(text: str) -> list[str]:
    """BIL ids whose metadata mentions ``text`` anywhere (title, abstract,
    technique, instrument, contributor). Case-insensitive on BIL's side."""
    return query("fulltext", text=text)


def retrieve(bildid: str) -> DatasetDetail:
    """Full metadata record for one dataset. Raises BilNotFoundError for an
    unknown id."""
    url = f"{API_BASE}/retrieve"
    params = {"bildid": bildid}
    cached = cache.get("retrieve", url, params)
    if cached is None:
        resp = send("GET", url, params=params, timeout=120.0)
        body = resp.json()
        entries = body.get("retjson") or []
        if str(body.get("success", "")).lower() != "true" or not entries:
            raise BilNotFoundError(f"no BIL record for {bildid!r}: {body.get('message', '')}")
        cached = entries[0]
        cache.put("retrieve", url, params, cached)
    return DatasetDetail.from_json(cached)


def retrieve_many(bildids: list[str]) -> dict[str, DatasetDetail]:
    """Full records for many ids in batched POSTs. Ids BIL does not know
    are simply absent from the result (the endpoint reports partial success
    rather than failing the batch)."""
    out: dict[str, DatasetDetail] = {}
    pending: list[str] = []
    url = f"{API_BASE}/retrieve"
    for b in bildids:
        cached = cache.get("retrieve", url, {"bildid": b})
        if cached is not None:
            out[b] = DatasetDetail.from_json(cached)
        else:
            pending.append(b)
    session_post = _post_retrieve
    for i in range(0, len(pending), _RETRIEVE_BATCH):
        chunk = pending[i : i + _RETRIEVE_BATCH]
        for entry in session_post(chunk):
            detail = DatasetDetail.from_json(entry)
            if detail.bildid:
                cache.put("retrieve", url, {"bildid": detail.bildid}, entry)
                out[detail.bildid] = detail
    return out


def _post_retrieve(bildids: list[str]) -> list[dict[str, Any]]:
    from ._client import get_session

    resp = get_session().post(f"{API_BASE}/retrieve", json={"bildids": bildids}, timeout=300.0)
    if resp.status_code >= 400:
        raise BilError(f"HTTP {resp.status_code} from POST /retrieve: {resp.text[:300]}")
    body = resp.json()
    entries = body.get("retjson") or []
    return [e for e in entries if isinstance(e, dict)]
