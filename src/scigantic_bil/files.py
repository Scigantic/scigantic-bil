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
from urllib.parse import unquote

from . import cache
from ._client import DOWNLOAD_BASE, BilError, BilNotFoundError, send
from .models import Dataset, DatasetDetail, FileEntry, dataset_url, encode_url

_MANIFEST_URL = f"{DOWNLOAD_BASE}/inventory/datasets/JSON/{{bildid}}.json.gz"
# BIL ids are one to three (older) or three (current) lowercase words:
# 13,363 three-word, 748 two-word, 113 one-word in the 2026-07-31 inventory.
_BILDID_RE = re.compile(r"[a-z]{3}(?:-[a-z]{3}){0,3}")
_TERAFLY_RE = re.compile(r"^RES_\d+x\d+x\d+_?$", re.IGNORECASE)

# A manifest is one gzipped JSON document listing every file. Most are KB to
# a few MB, but a MERFISH or fMOST dataset with millions of files has one of
# 200-710 MB gzipped (measured 2026-09-08: ace-owl-cot, 4.7 M files, 710 MB).
# Downloading and json-decoding that in a notebook is not what anyone asked
# for by calling walk(), so manifest() checks Content-Length first and
# refuses above MANIFEST_MAX_BYTES (override per call); walk() and
# find_zarr() fall back to crawling the directory listings instead. Only
# small manifests are written to the disk cache.
MANIFEST_MAX_BYTES = 64 << 20
_MANIFEST_DISK_CACHE_MAX = 8 << 20


class ManifestTooLargeError(BilError):
    """The dataset's manifest exceeds the size limit passed to manifest()."""

# nginx autoindex row: <a href="NAME">NAME</a>   15-Dec-2021 01:58   50912
# Directories end with "/" and report "-" for size.
_ROW_RE = re.compile(
    r'<a href="(?P<href>[^"]+)">[^<]*</a>\s+(?P<date>\d{2}-\w{3}-\d{4} \d{2}:\d{2})\s+(?P<size>[\d-]+)'
)

Target = "str | Dataset | DatasetDetail"


def path_is_landing_zone(path: str) -> bool:
    return path.startswith("/bil/lz/")


def resolve_url(target: str | Dataset | DatasetDetail) -> str:
    """Normalise anything that identifies a location on the download
    server to its directory URL (trailing slash): a Dataset or
    DatasetDetail, a ``/bil/data/...`` path, a full https URL, or a BIL id
    (which costs one metadata lookup)."""
    if isinstance(target, (Dataset, DatasetDetail)):
        if path_is_landing_zone(target.bildirectory):
            raise BilNotFoundError(
                f"{target.bildid} lives under BIL's landing zone ({target.bildirectory}), which is not "
                "served publicly; the dataset has not been published to /bil/data/ yet"
            )
        return target.url
    if target.startswith("http://") or target.startswith("https://"):
        url = encode_url(target)
        return url if url.endswith("/") else url + "/"
    if path_is_landing_zone(target):
        raise BilNotFoundError(
            f"{target} is under BIL's landing zone (/bil/lz/), which is not served publicly; "
            "the dataset has not been published to /bil/data/ yet"
        )
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


def manifest_size(bildid: str | Dataset | DatasetDetail) -> int | None:
    """Gzipped byte size of a dataset's manifest from a HEAD request, or
    None if BIL has not built one. Cheap; call it before manifest() on an
    unfamiliar dataset."""
    b = _bildid_of(bildid)
    if b is None:
        raise ValueError(f"manifest_size() needs a BIL id or Dataset, got {bildid!r}")
    try:
        head = send("HEAD", _MANIFEST_URL.format(bildid=b), timeout=60.0)
    except BilNotFoundError:
        return None
    length = head.headers.get("Content-Length")
    head.close()
    return int(length) if length else None


def manifest(
    bildid: str | Dataset | DatasetDetail, max_bytes: int | None = MANIFEST_MAX_BYTES
) -> list[FileEntry]:
    """Every file of a dataset from BIL's own per-dataset manifest
    (``inventory/datasets/JSON/<bildid>.json.gz``): relative path, size,
    modification time, MD5 and download URL for each file, in one gzipped
    GET (352 KB for a 1,923-slice stack, 2.6 MB for a 15,323-chunk zarr
    store, measured 2026-09-08). This is the complete truth for a dataset,
    zarr chunks included; walk() is the store-aware view over it.

    Raises BilNotFoundError when BIL has not built a manifest for the id
    and ManifestTooLargeError when the gzipped manifest exceeds
    ``max_bytes`` (default MANIFEST_MAX_BYTES; pass None to accept any
    size, knowing a 4.7 M-file dataset's manifest is 710 MB gzipped)."""
    b = _bildid_of(bildid)
    if b is None:
        raise ValueError(f"manifest() needs a BIL id or Dataset, got {bildid!r}")
    url = _MANIFEST_URL.format(bildid=b)
    cached = cache.get("manifest", url)
    if cached is None:
        size = manifest_size(b)
        if size is None:
            raise BilNotFoundError(f"BIL has no manifest for {b!r}")
        if max_bytes is not None and size > max_bytes:
            raise ManifestTooLargeError(
                f"manifest for {b!r} is {size / 1e6:.0f} MB gzipped, over the {max_bytes / 1e6:.0f} MB "
                "limit; pass max_bytes=None to load it anyway, or use walk()/list_files() to crawl"
            )
        resp = send("GET", url, timeout=600.0)
        body = json.loads(gzip.decompress(resp.content).decode("utf-8"))
        resp.close()
        root = encode_url(str(body.get("download_url") or "").rstrip("/") + "/")
        rows: list[dict[str, Any]] = []
        for item in body.get("manifest") or []:
            if not isinstance(item, dict):
                continue
            rel = str(item.get("relativepath") or item.get("filename") or "")
            raw_url = item.get("download_url")
            file_url = encode_url(str(raw_url)) if raw_url else root + encode_url(rel)
            fsize = item.get("size")
            rows.append(
                {
                    "name": str(item.get("filename") or rel.rsplit("/", 1)[-1]),
                    "url": file_url,
                    "size": int(fsize) if isinstance(fsize, (int, float)) else None,
                    "modified": str(item.get("modification_time") or ""),
                    "is_dir": False,
                    "path": rel,
                    "md5": str(item.get("md5") or ""),
                }
            )
        cached = rows
        if size <= _MANIFEST_DISK_CACHE_MAX:
            cache.put("manifest", url, None, cached)
    return [FileEntry(**row) for row in cached]


def list_files(target: str | Dataset | DatasetDetail) -> list[FileEntry]:
    """Entries of one directory (not recursive). Cached.

    A 404 on a dataset's own directory usually means BIL's inventory path
    is stale (the directory was renamed on the server; seen for
    ``IV68_..._ventral midbrain_...`` listed with a space where the server
    has an underscore). The error says so and names the parent to list."""
    url = resolve_url(target)
    cached = cache.get("listing", url)
    if cached is None:
        try:
            resp = send("GET", url, timeout=120.0)
        except BilNotFoundError as exc:
            parent = url.rstrip("/").rsplit("/", 1)[0] + "/"
            raise BilNotFoundError(
                f"{url} does not exist on the download server. If this came from a dataset's "
                f"bildirectory, BIL's inventory path may be stale; list the parent {parent} "
                "to find the current name."
            ) from exc
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
                # nginx serves hrefs percent-encoded (Virus_tracing-B1-%236/);
                # the name is the decoded filename, the url keeps the encoding.
                "name": unquote(href.rstrip("/")),
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
        except (BilNotFoundError, ManifestTooLargeError):
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


def first_images(
    target: str | Dataset | DatasetDetail,
    suffix: str | tuple[str, ...] = (".tif", ".tiff", ".ome.tif", ".ome.tiff"),
    max_depth: int = 8,
) -> list[FileEntry]:
    """The first folder of image files under ``target``, found by a bounded
    descent: list a directory, return its matching files if it has any,
    otherwise step into its first subdirectory (natural order) and repeat.
    One request per level, never a full crawl. This is what a preview
    needs on a dataset with millions of files (an fMOST TeraFly tree, a
    MERFISH run), where find() would list every folder."""
    suffixes = (suffix,) if isinstance(suffix, str) else suffix
    suffixes = tuple(x.lower() for x in suffixes)
    url = resolve_url(target)
    for _ in range(max_depth + 1):
        entries = list_files(url)
        hits = [e for e in entries if not e.is_dir and e.name.lower().endswith(suffixes)]
        if hits:
            hits.sort(key=lambda e: _natural_key(e.name))
            return hits
        dirs = sorted((e for e in entries if e.is_dir and not is_store_dir(e)), key=lambda e: _natural_key(e.name))
        if not dirs:
            return []
        url = dirs[0].url
    return []


def extensions_under(target: str | Dataset | DatasetDetail, max_depth: int = 8) -> list[str]:
    """File extensions at the first level that holds files, following the
    first-subdirectory chain like first_images(). For error messages and
    quick orientation on a dataset whose root is all folders."""
    url = resolve_url(target)
    for _ in range(max_depth + 1):
        entries = list_files(url)
        files = [e for e in entries if not e.is_dir]
        if files:
            return sorted({e.extension for e in files})
        dirs = sorted((e for e in entries if e.is_dir and not is_store_dir(e)), key=lambda e: _natural_key(e.name))
        if not dirs:
            return []
        url = dirs[0].url
    return []


def find_zarr(
    target: str | Dataset | DatasetDetail, max_depth: int = 2, max_dirs: int = 64
) -> list[str]:
    """URLs of every ``*.zarr`` directory under ``target``. A zarr store is
    a directory, so it never appears in the inventory's extension
    histogram; this is how to tell whether a dataset ships one.

    Uses the manifest when BIL has one of manageable size; otherwise
    crawls listings with a hard budget: at most ``max_dirs`` directories
    listed, ``max_depth`` levels deep, never entering TeraFly ``RES_``
    folders. The budget exists because the unbounded fallback listed a
    1.6 M-file fMOST tree one folder at a time and thumbnail() sat on it
    for over twenty minutes (2026-09-08). BIL zarr stores sit at the
    dataset root or one level down."""
    b = _bildid_of(target)
    if b is not None:
        try:
            entries = manifest(b)
        except (BilNotFoundError, ManifestTooLargeError):
            entries = []
        if entries:
            return sorted(e.url for e in _collapse_stores(entries) if is_store_dir(e))
    root = resolve_url(target)
    out: list[str] = []
    stack: list[tuple[str, int]] = [(root, 0)]
    listed = 0
    while stack and listed < max_dirs:
        url, depth = stack.pop()
        listed += 1
        for entry in list_files(url):
            if not entry.is_dir:
                continue
            if entry.name.lower().endswith(".zarr"):
                out.append(entry.url)
            elif depth < max_depth and not _TERAFLY_RE.match(entry.name):
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
