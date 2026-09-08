"""Read image data from BIL without downloading a dataset.

Three shapes cover what BIL actually holds (measured across the 2026-07-31
inventory, 14,224 datasets):

- Single-slice TIFF stacks, one file per z (and per channel): the dominant
  light-sheet, fMOST and STPT layout (``.tif`` in 4,672 datasets, ``.tiff``
  in 608, ``.ome.tif`` in 226). read_tiff() fetches one slice; a ~16 MB
  slice arrived in 0.4 s single-stream (45 MB/s) on 2026-09-08. Eight
  parallel streams aggregated to 19 MB/s, i.e. slower, so this module
  reads sequentially on purpose.
- OME-Zarr stores (a directory, invisible to the inventory's extension
  histogram; find them with files.find_zarr()). open_zarr() opens one
  lazily through zarr's HTTP store; thumbnail() reads only the coarsest
  pyramid level.
- JPEG 2000 (``.jp2``, 5,787 datasets, mostly STPT/fMOST): not decoded by
  this package at 0.1.0. read_tiff() raises UnsupportedFormatError with a
  pointer to glymur rather than silently returning nothing.

Large multi-page TIFFs are read through HttpFile, a seekable file object
backed by HTTP range requests, so tifffile fetches only the IFD and the
pages asked for. Small files (under ``_WHOLE_FILE_LIMIT``) are fetched in
one GET, which is faster than several ranges for a single slice.
"""

from __future__ import annotations

import io
import math
from typing import IO, TYPE_CHECKING, Any, Sequence, cast

import numpy as np

from ._client import BilError, send
from .files import find, find_zarr, list_files, resolve_url
from .models import Dataset, DatasetDetail, FileEntry

if TYPE_CHECKING:
    import zarr

_WHOLE_FILE_LIMIT = 64 << 20
_BLOCK = 1 << 20

_TIFF_SUFFIXES = (".tif", ".tiff", ".ome.tif", ".ome.tiff")


class UnsupportedFormatError(BilError):
    """Raised for a file this package does not decode (JPEG 2000, Imaris,
    NIfTI). The message names the library that does."""


class HttpFile(io.RawIOBase):
    """A read-only, seekable file over HTTP range requests with a block
    cache, so tifffile (or anything expecting ``read``/``seek``/``tell``)
    can walk a remote TIFF's IFD chain without fetching the whole file."""

    def __init__(self, url: str, size: int | None = None, block_size: int = _BLOCK) -> None:
        super().__init__()
        self.url = url
        self._pos = 0
        self._block_size = block_size
        self._blocks: dict[int, bytes] = {}
        self.bytes_fetched = 0
        self.requests_made = 0
        if size is None:
            head = send("HEAD", url, timeout=60.0)
            length = head.headers.get("Content-Length")
            head.close()
            if length is None:
                raise BilError(f"server did not report a size for {url}")
            size = int(length)
        self._size = size

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        elif whence == io.SEEK_END:
            self._pos = self._size + offset
        else:
            raise ValueError(f"bad whence {whence}")
        self._pos = max(0, self._pos)
        return self._pos

    @property
    def size(self) -> int:
        return self._size

    def _block(self, index: int) -> bytes:
        block = self._blocks.get(index)
        if block is None:
            start = index * self._block_size
            end = min(start + self._block_size, self._size) - 1
            if start > end:
                return b""
            resp = send("GET", self.url, headers={"Range": f"bytes={start}-{end}"}, timeout=120.0)
            block = resp.content
            resp.close()
            self._blocks[index] = block
            self.bytes_fetched += len(block)
            self.requests_made += 1
        return block

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self._size - self._pos
        if self._pos >= self._size or n == 0:
            return b""
        n = min(n, self._size - self._pos)
        out = bytearray()
        pos = self._pos
        while len(out) < n:
            index, offset = divmod(pos, self._block_size)
            block = self._block(index)
            take = block[offset : offset + (n - len(out))]
            if not take:
                break
            out += take
            pos += len(take)
        self._pos = pos
        return bytes(out)

    def readinto(self, b: Any) -> int:
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)


def _entry_of(target: FileEntry | str) -> tuple[str, int | None]:
    if isinstance(target, FileEntry):
        return target.url, target.size
    return target, None


def read_tiff(target: FileEntry | str, key: int | Sequence[int] | None = None) -> np.ndarray:
    """One TIFF (a light-sheet z-slice, an STPT section, an OME-TIFF) as a
    numpy array, straight from the download server. ``key`` selects pages
    of a multi-page file, as in ``tifffile.imread(key=...)``; for a
    multi-page file only the requested pages are fetched."""
    import tifffile

    url, size = _entry_of(target)
    lower = url.lower()
    if lower.endswith(".jp2"):
        raise UnsupportedFormatError(
            f"{url} is JPEG 2000; scigantic-bil does not decode .jp2 yet. "
            "Use files.download() then glymur (pip install glymur, needs OpenJPEG)."
        )
    if lower.endswith(".ims"):
        raise UnsupportedFormatError(f"{url} is an Imaris HDF5 file; download it and open with h5py.")
    if not lower.endswith(_TIFF_SUFFIXES):
        raise UnsupportedFormatError(f"{url} is not a TIFF")
    if size is None:
        head = send("HEAD", url, timeout=60.0)
        length = head.headers.get("Content-Length")
        head.close()
        size = int(length) if length else None
    if size is not None and size <= _WHOLE_FILE_LIMIT and key is None:
        resp = send("GET", url, timeout=300.0)
        data = resp.content
        resp.close()
        return np.asarray(tifffile.imread(io.BytesIO(data)))
    fh = HttpFile(url, size=size)
    with tifffile.TiffFile(cast("IO[bytes]", fh)) as tif:
        if key is None:
            return np.asarray(tif.asarray())
        return np.asarray(tif.asarray(key=key))


def slices(target: str | Dataset | DatasetDetail, channel: str | None = None) -> list[FileEntry]:
    """The TIFF files of a dataset in natural z order; the raw material of
    a stack. ``channel`` keeps only names containing that substring
    (``"ch02"``) when a directory interleaves channels."""
    entries = find(target, suffix=_TIFF_SUFFIXES, recursive=True)
    if channel:
        entries = [e for e in entries if channel.lower() in e.name.lower()]
    return entries


def read_stack(
    target: str | Dataset | DatasetDetail,
    start: int = 0,
    stop: int | None = None,
    step: int = 1,
    channel: str | None = None,
) -> np.ndarray:
    """Stack a range of z-slices into one ``(z, y, x)`` array. Reads one
    file per slice, sequentially; ``step`` subsamples along z. Keep the
    range small: a full 2,000-slice stack is ~30 GB."""
    entries = slices(target, channel=channel)[start:stop:step]
    if not entries:
        raise BilError(f"no TIFF slices found under {resolve_url(target)}")
    planes = [read_tiff(e) for e in entries]
    return np.stack(planes, axis=0)


def downsample(array: np.ndarray, max_size: int = 512) -> np.ndarray:
    """Stride-subsample a 2-D (or 2-D plus channel) array so its longest
    edge is at most ``max_size``. Integer stride, no filtering: cheap,
    and adequate for a preview."""
    if array.ndim < 2:
        return array
    h, w = array.shape[:2]
    stride = max(1, math.ceil(max(h, w) / max_size))
    return array[::stride, ::stride]


def thumbnail(
    target: str | Dataset | DatasetDetail | FileEntry,
    max_size: int = 512,
    index: int | None = None,
    channel: str | None = None,
) -> np.ndarray:
    """A small 2-D preview of a dataset, reading as little as possible:
    the middle z-slice of a TIFF stack (one file), or the coarsest pyramid
    level of an OME-Zarr store (a few chunks). Pass a FileEntry to preview
    one specific file, or ``index`` to pick a slice."""
    if isinstance(target, FileEntry):
        return downsample(_squeeze_2d(read_tiff(target)), max_size)
    # A zarr store first: it is a shallow check and its coarsest level is
    # the cheapest preview there is. Only then look for TIFF slices.
    stores = find_zarr(target)
    if stores:
        return zarr_thumbnail(stores[0], max_size=max_size, index=index)
    stack = slices(target, channel=channel)
    if stack:
        i = len(stack) // 2 if index is None else index
        return downsample(_squeeze_2d(read_tiff(stack[i])), max_size)
    other = [e for e in list_files(target) if not e.is_dir]
    exts = sorted({e.extension for e in other})
    raise UnsupportedFormatError(
        f"no TIFF slices or zarr store under {resolve_url(target)}; found extensions {exts}"
    )


def _squeeze_2d(array: np.ndarray) -> np.ndarray:
    """Reduce a slice read as (1, y, x) or (c, y, x) to 2-D for preview by
    taking the first plane; leaves (y, x) and (y, x, rgb) alone."""
    a = np.asarray(array)
    while a.ndim > 2 and a.shape[0] <= 4 and not (a.ndim == 3 and a.shape[-1] in (3, 4)):
        a = a[0]
    while a.ndim > 3:
        a = a[0]
    if a.ndim == 3 and a.shape[-1] not in (3, 4):
        a = a[a.shape[0] // 2]
    return a


def open_zarr(target: str | Dataset | DatasetDetail) -> "zarr.Group | zarr.Array[Any]":
    """Open an OME-Zarr store on the download server lazily. ``target`` may
    be the store URL itself or a dataset, in which case the first
    ``*.zarr`` directory found is opened. Nothing is read until you slice
    an array. Requires ``pip install scigantic-bil[zarr]``."""
    try:
        import zarr
    except ImportError as exc:
        raise ImportError("zarr is not installed; pip install 'scigantic-bil[zarr]'") from exc
    url = target if isinstance(target, str) and target.lower().rstrip("/").endswith(".zarr") else None
    if url is None:
        stores = find_zarr(target)
        if not stores:
            raise BilError(f"no *.zarr store found under {resolve_url(target)}")
        url = stores[0]
    return zarr.open(url.rstrip("/"), mode="r")


def zarr_levels(group: Any, store_url: str | None = None) -> list[str]:
    """Multiscale level paths of an OME-Zarr group, finest first.

    Read from the ``multiscales`` metadata, then checked against what the
    server actually has: verified 2026-09-08 that at least one BIL store
    (``ace-cup-eel``) declares eight levels in ``.zattrs`` while only seven
    directories exist, so trusting the metadata alone raises KeyError on
    the coarsest level. When ``store_url`` is known the directory listing
    is used; otherwise each declared path is probed."""
    attrs = dict(group.attrs)
    ms = attrs.get("multiscales")
    if isinstance(ms, dict):
        ms = [ms]
    declared: list[str] = []
    if isinstance(ms, list) and ms:
        datasets = ms[0].get("datasets") or []
        declared = [
            str(d.get("path")) for d in datasets if isinstance(d, dict) and d.get("path") is not None
        ]
    url = store_url or _store_url_of(group)
    if url:
        present = {e.name for e in list_files(url) if e.is_dir}
        if declared:
            kept = [p for p in declared if p.split("/")[0] in present]
            if kept:
                return kept
        numeric = sorted((n for n in present if n.isdigit()), key=int)
        if numeric:
            return numeric
    if declared:
        kept = []
        for path in declared:
            try:
                group[path]
            except KeyError:
                continue
            kept.append(path)
        if kept:
            return kept
    keys = sorted((k for k in group.keys() if str(k).isdigit()), key=int)
    return [str(k) for k in keys]


def _store_url_of(group: Any) -> str | None:
    """Best effort: the https URL a zarr 3 FsspecStore-backed group was
    opened from, or None."""
    store = getattr(group, "store", None)
    for attr in ("path", "root"):
        val = getattr(store, attr, None)
        if isinstance(val, str) and val.startswith("http"):
            return val if val.endswith("/") else val + "/"
    fs = getattr(store, "fs", None)
    path = getattr(store, "path", None)
    if fs is not None and isinstance(path, str):
        proto = getattr(fs, "protocol", None)
        proto = proto[0] if isinstance(proto, (tuple, list)) else proto
        if proto in ("http", "https") and not path.startswith("http"):
            return f"https://{path}/" if not path.endswith("/") else f"https://{path}"
        if path.startswith("http"):
            return path if path.endswith("/") else path + "/"
    return None


def zarr_thumbnail(target: str | Any, max_size: int = 512, index: int | None = None) -> np.ndarray:
    """Middle plane of the coarsest pyramid level of an OME-Zarr store,
    then stride-downsampled to ``max_size``."""
    group: Any = open_zarr(target) if isinstance(target, str) else target
    levels = zarr_levels(group)
    arr: Any = group[levels[-1]] if levels else group
    shape = tuple(int(s) for s in arr.shape)
    sel: list[Any] = []
    for axis, extent in enumerate(shape):
        remaining = len(shape) - axis
        if remaining > 2:
            sel.append(extent // 2 if (index is None or remaining != 3) else min(index, extent - 1))
        else:
            sel.append(slice(None))
    plane = np.asarray(arr[tuple(sel)])
    return downsample(plane, max_size)
