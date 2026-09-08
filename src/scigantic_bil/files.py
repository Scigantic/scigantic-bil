"""Lazy file listing over BIL's download server, which is plain nginx with
autoindex on (verified 2026-09-08): every dataset directory returns an HTML
index of ``name / modified / size`` rows, range requests are honoured, and
nothing needs a login. Listings are small and cached; nothing is downloaded
unless download() is called.

A light-sheet dataset here is typically one directory of a few thousand
single-slice TIFFs (``Z00001_ch02.tif`` ...), ~16 MB each, so listing a
dataset is one request and reading one slice is one more.
"""

from __future__ import annotations

import gzip
import json
import re
import shutil
from pathlib import Path
from typing import Any, Iterator

from . import cache
from ._client import DOWNLOAD_BASE, BilNotFoundError, send
from .models import Dataset, DatasetDetail, FileEntry, dataset_url

_MANIFEST_URL = f"{DOWNLOAD_BASE}/inventory/datasets/JSON/{{bildid}}.json.gz"
_BILDID_RE = re.compile(r"[a-z]{3}-[a-z]{3}-[a-z]{3}")

# nginx autoindex row: <a href="NAME">NAME</a>   15-Dec-2021 01:58   50912
# Directories end with "/" and report "-" for size.
_ROW_RE = re.compile(
    r'<a href="(?P<href>[^"]+)">[^<]*</a>\s+(?P<date>\d{2}-\w{3}-\d{4} \d{2}:\d{2})\s+(?P<size>[\d-]+)'
)

Target = "str | Dataset | DatasetDetail"


def resolve_url(target: str | Dataset | DatasetDetail) -> str:
    """Normalise anything that identifies a location on the download
    server to its directory URL (trailing slash): a Dataset or
    DatasetDetail, a ``/bil/data/...`` path, a full https URL, or a BIL id
    (which costs one metadata lookup)."""
    if isinstance(target, (Dataset, DatasetDetail)):
        return target.url
    if target.startswith("http://") or target.startswith("https://"):
        return target if target.endswith("/") else target + "/"
    if target.startswith("/bil/"):
        return dataset_url(target)
    if _BILDID_RE.fullmatch(target):
        from .api import retrieve

        return retrieve(target).url
    return dataset_url(target)


def _bildid_of(target: str | Dataset | DatasetDetail) -> str | None:
    if isinstance(target, (Dataset, DatasetDetail)):
        return target.bildid or None
    return target if _BILDID_RE.fullmatch(target) else None


def manifest(bildid: str | Dataset | DatasetDetail) -> list[FileEntry]:
    """Every file of a dataset from BIL's own per-dataset manifest
    (``inventory/datasets/JSON/<bildid>.json.gz``): relative path, size,
    modification time, MD5 and download URL for each file, in one gzipped
    GET (352 KB for a 1,923-slice stack, 2.6 MB for a 15,323-chunk zarr
    store, measured 2026-09-08). This is the complete truth for a dataset,
    zarr chunks included; walk() is the store-aware view over it. Raises
    BilNotFoundError when BIL has not built a manifest for the id."""
    b = _bildid_of(bildid)
    if b is None:
        raise ValueError(f"manifest() needs a BIL id or Dataset, got {bildid!r}")
    url = _MANIFEST_URL.format(bildid=b)
    cached = cache.get("manifest", url)
    if cached is None:
        resp = send("GET", url, timeout=300.0)
        body = json.loads(gzip.decompress(resp.content).decode("utf-8"))
        resp.close()
        root = str(body.get("download_url") or "").rstrip("/") + "/"
        rows: list[dict[str, Any]] = []
        for item in body.get("manifest") or []:
            if not isinstance(item, dict):
                continue
            rel = str(item.get("relativepath") or item.get("filename") or "")
            file_url = str(item.get("download_url") or (root + rel))
            size = item.get("size")
            rows.append(
                {
                    "name": str(item.get("filename") or rel.rsplit("/", 1)[-1]),
                    "url": file_url,
                    "size": int(size) if isinstance(size, (int, float)) else None,
                    "modified": str(item.get("modification_time") or ""),
                    "is_dir": False,
                    "path": rel,
                    "md5": str(item.get("md5") or ""),
                }
            )
        cached = rows
        cache.put("manifest", url, None, cached)
    return [FileEntry(**row) for row in cached]


def list_files(target: str | Dataset | DatasetDetail) -> list[FileEntry]:
    """Entries of one directory (not recursive). Cached."""
    url = resolve_url(target)
    cached = cache.get("listing", url)
    if cached is None:
        resp = send("GET", url, timeout=120.0)
        cached = _parse_listing(resp.text, url)
        cache.put("listing", url, None, cached)
    return [FileEntry(**row) for row in cached]


def _parse_listing(html: str, base_url: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for m in _ROW_RE.finditer(html):
        href = m.group("href")
        if href in ("../", "./"):
            continue
        is_dir = href.endswith("/")
        size = m.group("size")
        rows.append(
            {
                "name": href.rstrip("/"),
                "url": base_url + href,
                "size": None if size == "-" else int(size),
                "modified": m.group("date"),
                "is_dir": is_dir,
            }
        )
    return rows


_OPAQUE_DIR_SUFFIXES = (".zarr", ".n5", ".ome.zarr")


def is_store_dir(entry: FileEntry) -> bool:
    """A directory that is a chunked array store (zarr, N5) rather than a
    folder of files. walk() never descends into one: a zarr pyramid is
    thousands of chunk directories, and listing them one request at a time
    took 218 s on a real BIL store before this guard existed. Use
    find_zarr()/open_zarr() for those."""
    return entry.is_dir and entry.name.lower().endswith(_OPAQUE_DIR_SUFFIXES)


def walk(target: str | Dataset | DatasetDetail, max_depth: int = 8) -> Iterator[FileEntry]:
    """Recursive listing, files only. For a dataset (id, Dataset or
    DatasetDetail) this reads BIL's manifest in one request and collapses
    each zarr/N5 store to a single directory entry; for an arbitrary URL,
    or when no manifest exists, it crawls the nginx autoindex one request
    per directory, never entering a store (see is_store_dir)."""
    b = _bildid_of(target)
    if b is not None:
        try:
            entries = manifest(b)
        except BilNotFoundError:
            entries = []
        if entries:
            yield from _collapse_stores(entries)
            return
    root = resolve_url(target)
    stack: list[tuple[str, int]] = [(root, 0)]
    while stack:
        url, depth = stack.pop()
        for entry in list_files(url):
            if is_store_dir(entry):
                yield entry
            elif entry.is_dir:
                if depth < max_depth:
                    stack.append((entry.url, depth + 1))
            else:
                yield entry


def _collapse_stores(entries: list[FileEntry]) -> Iterator[FileEntry]:
    """Replace every file inside a ``*.zarr``/``*.n5`` directory with one
    directory entry for the store, preserving first-seen order."""
    seen: set[str] = set()
    for e in entries:
        parts = e.path.split("/") if e.path else [e.name]
        store_index = next(
            (i for i, part in enumerate(parts[:-1]) if part.lower().endswith(_OPAQUE_DIR_SUFFIXES)),
            None,
        )
        if store_index is None:
            yield e
            continue
        store_rel = "/".join(parts[: store_index + 1])
        if store_rel in seen:
            continue
        seen.add(store_rel)
        root = e.url[: e.url.rfind(e.path)] if e.path and e.path in e.url else e.url.rsplit("/", 1)[0] + "/"
        yield FileEntry(
            name=parts[store_index],
            url=root + store_rel + "/",
            size=None,
            modified="",
            is_dir=True,
            path=store_rel,
        )


def find(
    target: str | Dataset | DatasetDetail,
    suffix: str | tuple[str, ...] = (".tif", ".tiff", ".ome.tif", ".ome.tiff"),
    recursive: bool = True,
) -> list[FileEntry]:
    """Files under ``target`` whose name ends with ``suffix``
    (case-insensitive), sorted by name so z-slices come out in order."""
    suffixes = (suffix,) if isinstance(suffix, str) else suffix
    suffixes = tuple(s.lower() for s in suffixes)
    entries = walk(target) if recursive else (e for e in list_files(target) if not e.is_dir)
    hits = [e for e in entries if not e.is_dir and e.name.lower().endswith(suffixes)]
    hits.sort(key=lambda e: _natural_key(e.name))
    return hits


def find_zarr(target: str | Dataset | DatasetDetail, max_depth: int = 4) -> list[str]:
    """URLs of every ``*.zarr`` directory under ``target``. A zarr store is
    a directory, so it never appears in the inventory's extension
    histogram; this is how to tell whether a dataset ships one."""
    b = _bildid_of(target)
    if b is not None:
        try:
            entries = manifest(b)
        except BilNotFoundError:
            entries = []
        if entries:
            return sorted(e.url for e in _collapse_stores(entries) if is_store_dir(e))
    root = resolve_url(target)
    out: list[str] = []
    stack: list[tuple[str, int]] = [(root, 0)]
    while stack:
        url, depth = stack.pop()
        for entry in list_files(url):
            if not entry.is_dir:
                continue
            if entry.name.lower().endswith(".zarr"):
                out.append(entry.url)
            elif depth < max_depth:
                stack.append((entry.url, depth + 1))
    return sorted(out)


def download(entry: FileEntry | str, dest: str | Path) -> Path:
    """Stream one file to ``dest`` (a path or a directory). Returns the
    written path. This is the only function in the package that writes
    image bytes to disk."""
    url = entry.url if isinstance(entry, FileEntry) else entry
    name = entry.name if isinstance(entry, FileEntry) else url.rstrip("/").rsplit("/", 1)[-1]
    dest = Path(dest)
    path = dest / name if dest.is_dir() else dest
    path.parent.mkdir(parents=True, exist_ok=True)
    resp = send("GET", url, stream=True, timeout=600.0)
    tmp = path.with_suffix(path.suffix + ".part")
    with open(tmp, "wb") as fh:
        shutil.copyfileobj(resp.raw, fh, length=1 << 20)
    resp.close()
    tmp.replace(path)
    return path


def _natural_key(name: str) -> list[object]:
    return [int(tok) if tok.isdigit() else tok.lower() for tok in re.split(r"(\d+)", name)]
