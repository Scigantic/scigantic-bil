"""Local response cache for the metadata API and directory listings, ON by
default.

The family convention: a package that hits a live API per lookup caches by
default (scigantic-pubchem, scigantic-wwpdb); a package reading a public S3
mirror with no meaningful rate limit leaves caching opt-in (scigantic-chembl,
scigantic-bindingdb). BIL's metadata API and its nginx directory listings
are the former kind, so caching stays on unless turned off explicitly.

Image bytes (TIFF slices, zarr chunks) are never cached here: a single
light-sheet slice is ~16 MB and a notebook rarely re-reads the same one.
Only small JSON/HTML responses go through this module.

Entries expire after ttl_days (7 by default). BIL republishes its inventory
every few days, so a week keeps a session fast without letting a listing go
stale for months. Pass ttl_days=None to disable expiry.

Reads and writes are safe to call concurrently: each write goes to its own
uniquely-named temp file before an atomic os.replace(), so a reader never
sees a partial write and two writers of the same key never collide.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

_enabled = True
_cache_dir: Path | None = None
_ttl_seconds: float | None = 7 * 86400


def _default_cache_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "scigantic-bil"


def _resolve_dir() -> Path:
    global _cache_dir
    if _cache_dir is None:
        env = os.environ.get("SCIGANTIC_BIL_CACHE")
        _cache_dir = Path(env) if env else _default_cache_dir()
        _cache_dir.mkdir(parents=True, exist_ok=True)
    return _cache_dir


def enable_cache(cache_dir: str | None = None, ttl_days: float | None = 7) -> Path:
    """Turn caching on (it already is, by default), optionally pointing it at
    a directory and/or changing how long an entry stays valid. ttl_days=None
    disables expiry. Returns the resolved directory."""
    global _enabled, _cache_dir, _ttl_seconds
    if cache_dir is not None:
        _cache_dir = Path(cache_dir)
        _cache_dir.mkdir(parents=True, exist_ok=True)
    else:
        _resolve_dir()
    _ttl_seconds = ttl_days * 86400 if ttl_days is not None else None
    _enabled = True
    assert _cache_dir is not None
    return _cache_dir


def disable_cache() -> None:
    """Turn caching off. Every call hits BIL fresh until re-enabled."""
    global _enabled
    _enabled = False


def is_cache_enabled() -> bool:
    return _enabled


def cache_dir() -> Path:
    return _resolve_dir()


def _key(kind: str, url: str, params: dict[str, Any] | None) -> str:
    raw = json.dumps({"kind": kind, "url": url, "params": params or {}}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def get(kind: str, url: str, params: dict[str, Any] | None = None) -> Any | None:
    if not _enabled:
        return None
    file = _resolve_dir() / f"{_key(kind, url, params)}.json"
    if not file.exists():
        return None
    try:
        entry = json.loads(file.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if _ttl_seconds is not None and time.time() - entry.get("cached_at", 0) > _ttl_seconds:
        file.unlink(missing_ok=True)
        return None
    return entry["value"]


def put(kind: str, url: str, params: dict[str, Any] | None, value: Any) -> None:
    if not _enabled:
        return
    file = _resolve_dir() / f"{_key(kind, url, params)}.json"
    tmp = file.with_suffix(f".json.{uuid.uuid4().hex}.part")
    tmp.write_text(json.dumps({"cached_at": time.time(), "value": value}))
    os.replace(tmp, file)


def clear() -> int:
    """Delete every cached response and any downloaded inventory snapshot.
    Returns how many files were removed."""
    d = _resolve_dir()
    n = 0
    for pattern in ("*.json", "*.part", "inventory-*.tsv"):
        for f in d.glob(pattern):
            f.unlink()
            n += 1
    return n
