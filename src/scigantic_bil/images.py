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
import re
import threading
from typing import IO, TYPE_CHECKING, Any, Sequence, cast

import numpy as np

from ._client import BilError, send
from .files import extensions_under, find, find_zarr, first_images, list_files, resolve_url, _natural_key
from .models import Dataset, DatasetDetail, FileEntry, encode_url

if TYPE_CHECKING:
    import zarr

_WHOLE_FILE_LIMIT = 64 << 20
_BLOCK = 256 << 10

_TIFF_SUFFIXES = (".tif", ".tiff", ".ome.tif", ".ome.tiff")
_JP2_SUFFIXES = (".jp2", ".j2k", ".jpx")
_IMAGE_SUFFIXES = _TIFF_SUFFIXES + _JP2_SUFFIXES


class UnsupportedFormatError(BilError):
    """Raised for a file this package does not decode (JPEG 2000, Imaris,
    NIfTI). The message names the library that does."""


class HttpFile(io.RawIOBase):
    """A read-only, seekable file over HTTP range requests, so tifffile (or
    anything expecting ``read``/``seek``/``tell``) can walk a remote TIFF's
    IFD chain and fetch only the pages or tiles asked for.

    Two regimes, measured on a 21.7 GB BigTIFF OME-TIFF (92 pages, tiled
    1024x1024, IFDs scattered through the file) on 2026-09-08:

    - small reads (IFD walking, tag values, the OME-XML block) go through a
      block cache. With 1 MB blocks opening that file cost 81 MB in 78
      requests; 256 KB blocks cut the waste on scattered IFDs.
    - large reads (a tile, a strip, a whole page) bypass the cache and are
      fetched as exactly one range request for the bytes asked for. With
      1 MB blocks a 2 MB tile took two or three requests and the page read
      ran at 10 MB/s; one request per tile is the same 38 MB/s a whole-file
      GET gets.
    """

    def __init__(self, url: str, size: int | None = None, block_size: int = _BLOCK) -> None:
        super().__init__()
        self.url = url
        self._pos = 0
        self._block_size = block_size
        self._blocks: dict[int, bytes] = {}
        self._lock = threading.Lock()
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

    def _fetch(self, start: int, end_inclusive: int) -> bytes:
        """One range request. Safe to call from several threads at once
        (requests.Session is; the counters are guarded)."""
        resp = send("GET", self.url, headers={"Range": f"bytes={start}-{end_inclusive}"}, timeout=300.0)
        data = resp.content
        resp.close()
        with self._lock:
            self.bytes_fetched += len(data)
            self.requests_made += 1
        return data

    def _block(self, index: int) -> bytes:
        block = self._blocks.get(index)
        if block is None:
            start = index * self._block_size
            end = min(start + self._block_size, self._size) - 1
            if start > end:
                return b""
            block = self._fetch(start, end)
            self._blocks[index] = block
        return block

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self._size - self._pos
        if self._pos >= self._size or n == 0:
            return b""
        n = min(n, self._size - self._pos)
        if n >= self._block_size:
            # One request for exactly the span asked for; don't pollute the
            # block cache with a tile that will never be read twice.
            start = self._pos
            first, last = divmod(start, self._block_size)[0], (start + n - 1) // self._block_size
            cached = all(i in self._blocks for i in range(first, last + 1))
            if not cached:
                data = self._fetch(start, start + n - 1)
                self._pos += len(data)
                return data
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
    return encode_url(target), None


def read_tiff(target: FileEntry | str, key: int | Sequence[int] | None = None) -> np.ndarray:
    """One TIFF (a light-sheet z-slice, an STPT section, an OME-TIFF) as a
    numpy array, straight from the download server. ``key`` selects pages
    of a multi-page file, as in ``tifffile.imread(key=...)``; for a
    multi-page file only the requested pages are fetched."""
    import tifffile

    url, size = _entry_of(target)
    lower = url.lower()
    if lower.endswith(_JP2_SUFFIXES):
        raise UnsupportedFormatError(f"{url} is JPEG 2000: use read_jp2() or read_image()")
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
        try:
            return np.asarray(tifffile.imread(io.BytesIO(data)))
        except _DECODE_ERRORS as exc:
            return _decode_with_pillow(io.BytesIO(data), key=None, url=url, cause=exc)
    fh = HttpFile(url, size=size)
    try:
        with tifffile.TiffFile(cast("IO[bytes]", fh)) as tif:
            if key is None:
                return np.asarray(tif.asarray())
            return np.asarray(tif.asarray(key=key))
    except _DECODE_ERRORS as exc:
        fh.seek(0)
        return _decode_with_pillow(cast("IO[bytes]", fh), key=key, url=url, cause=exc)


def read_jp2(target: FileEntry | str, reduce: int = 0) -> np.ndarray:
    """One JPEG 2000 file (``.jp2``/``.j2k``) as a numpy array, decoded with
    imagecodecs' bundled OpenJPEG: exact dtype and every channel, so a
    16-bit STPT section comes back uint16 (11377 x 8557 in 1.5 s from a
    12 MB file) and a Dong-lab tracing section as (12000, 16000, 3)
    uint16. ``reduce=k`` decodes at 1/2**k resolution through Pillow
    instead, which is 4 to 15x faster on the 75 to 550 MB RGB sections
    but returns 8-bit and does not support 16-bit single-channel files;
    on those it falls back to a full decode plus stride. The whole file
    is always fetched: BIL's codestreams do not decode truncated (checked
    2026-09-08 at 3, 10 and 30 percent of the bytes)."""
    import imagecodecs

    url, size = _entry_of(target)
    if not url.lower().endswith(_JP2_SUFFIXES):
        raise UnsupportedFormatError(f"{url} is not JPEG 2000")
    resp = send("GET", url, timeout=900.0)
    data = resp.content
    resp.close()
    if reduce > 0:
        try:
            from PIL import Image

            Image.MAX_IMAGE_PIXELS = None
            im: Any = Image.open(io.BytesIO(data))
            # Jpeg2KImageFile.reduce is the decode-time resolution reduction
            # (an attribute set before load), not Image.reduce() the method.
            setattr(im, "reduce", reduce)
            im.load()
            return np.asarray(im)
        except Exception:
            arr = np.asarray(imagecodecs.jpeg2k_decode(data))
            step = 2**reduce
            return arr[::step, ::step]
    return np.asarray(imagecodecs.jpeg2k_decode(data))


def read_image(target: FileEntry | str, **kwargs: Any) -> np.ndarray:
    """read_tiff() or read_jp2() by file extension."""
    url, _ = _entry_of(target)
    if url.lower().endswith(_JP2_SUFFIXES):
        return read_jp2(target, **kwargs)
    return read_tiff(target, **kwargs)


# tifffile raises its codec's own error class on a bad strip; imagecodecs'
# DeflateError/LzwError are not importable without imagecodecs, so match
# broadly here and let _decode_with_pillow re-raise anything Pillow cannot
# read either.
_DECODE_ERRORS: tuple[type[BaseException], ...] = (ValueError, RuntimeError, OSError)


def _open_tiff(url: str, size: int | None) -> tuple[Any, HttpFile | None]:
    """A TiffFile over the whole file (small) or an HttpFile (large)."""
    import tifffile

    if size is None:
        head = send("HEAD", url, timeout=60.0)
        length = head.headers.get("Content-Length")
        head.close()
        size = int(length) if length else None
    if size is not None and size <= _WHOLE_FILE_LIMIT:
        resp = send("GET", url, timeout=300.0)
        data = resp.content
        resp.close()
        return tifffile.TiffFile(io.BytesIO(data)), None
    fh = HttpFile(url, size=size)
    return tifffile.TiffFile(cast("IO[bytes]", fh)), fh


def preview_plane(target: FileEntry | str, max_size: int = 512, page: int | None = None) -> np.ndarray:
    """A downsampled 2-D preview of one TIFF, reading a bounded amount
    however large the file is. Which page: ``page``, else the middle one.
    How it reads, by what the page is:

    - pyramid (OME-TIFF sub-resolutions): the coarsest level, whole
    - uncompressed and not tiled: every k-th row fetched by offset, so a
      10 GB single-strip plane (84289 x 61974, BIL MERFISH mosaic) costs
      ~512 rows, 63 MB, not 10 GB (which is what asarray() read)
    - compressed strips: every k-th strip decoded, first row kept
    - tiled: a centre region of 2 x max_size pixels a side, exact pixels,
      because a uniform decimation of a tiled page touches every tile
    - anything under 64 MB: read whole, then stride-downsampled
    - JPEG 2000: full decode for small sections, Pillow reduced-resolution
      decode (1/4 or 1/8) for large ones; see read_jp2()

    read_tiff()/read_jp2() remain the exact readers; this is for looking."""
    import tifffile

    url, size = _entry_of(target)
    if url.lower().endswith(_JP2_SUFFIXES):
        if size is None:
            head = send("HEAD", url, timeout=60.0)
            length = head.headers.get("Content-Length")
            head.close()
            size = int(length) if length else None
        # Small sections (STPT, ~12 MB) decode fully in a second or two.
        # Big RGB sections (75 to 550 MB) decode at reduced resolution;
        # a guess of reduce from the byte size keeps the decode under a
        # few seconds, the fetch itself is the cost there.
        if size is not None and size > _WHOLE_FILE_LIMIT:
            reduce = 2 if size < 200 << 20 else 3
            return downsample(_squeeze_2d(read_jp2(target, reduce=reduce)), max_size)
        return downsample(_squeeze_2d(read_jp2(target)), max_size)
    tif, fh = _open_tiff(url, size)
    with tif:
        n_pages = len(tif.pages)
        idx = (n_pages // 2) if page is None else min(page, n_pages - 1)
        pg: Any = tif.pages[idx]
        series: Any = None
        try:
            series = tif.series[0] if tif.series else None
        except Exception:
            series = None
        levels = getattr(series, "levels", None) if series is not None else None
        if levels and len(levels) > 1 and fh is not None:
            return downsample(_squeeze_2d(np.asarray(levels[-1].asarray())), max_size)
        if fh is None:
            try:
                return downsample(_squeeze_2d(np.asarray(pg.asarray())), max_size)
            except _DECODE_ERRORS as exc:
                tif.filehandle.seek(0)
                data = tif.filehandle.read()
                return downsample(_squeeze_2d(_decode_with_pillow(io.BytesIO(data), key=idx, url=url, cause=exc)), max_size)
        height, width = int(pg.imagelength), int(pg.imagewidth)
        spp = int(pg.samplesperpixel)
        stride = max(1, math.ceil(max(height, width) / max_size))
        if not pg.is_tiled and int(pg.compression) == 1 and pg.is_contiguous:
            offset = int(pg.dataoffsets[0])
            row_bytes = width * spp * int(pg.bitspersample) // 8
            dtype = np.dtype(pg.dtype)
            spans = [(offset + r * row_bytes, row_bytes) for r in range(0, height, stride)]
            rows = []
            for buf in _fetch_spans(fh, spans):
                row = np.frombuffer(buf, dtype=dtype, count=width * spp)
                rows.append(row.reshape(width, spp)[::stride, 0] if spp > 1 else row[::stride])
            return np.stack(rows, axis=0)
        if not pg.is_tiled:
            n_strips = len(pg.dataoffsets)
            step = max(1, n_strips // max(1, height // stride))
            picks = list(range(0, n_strips, step))
            spans = [(int(pg.dataoffsets[i]), int(pg.databytecounts[i])) for i in picks]
            rows = []
            for i, data in zip(picks, _fetch_spans(fh, spans)):
                a = np.asarray(pg.decode(data, i)[0])
                # decode() yields (depth, rows, width, samples); keep one row
                # of one sample plane, decimated along x.
                a = a.reshape(-1, a.shape[-2], a.shape[-1]) if a.ndim >= 3 else a[None]
                rows.append(a[0, ::stride] if a.ndim == 2 else a[0, :, 0][::stride] if a.shape[-1] > 1 else a[0, :, 0][::stride])
            return np.stack(rows, axis=0)
        half = max_size
        y0, x0 = max(0, height // 2 - half), max(0, width // 2 - half)
        region = read_region(target, rows=(y0, min(height, y0 + 2 * half)), cols=(x0, min(width, x0 + 2 * half)), page=idx)
        return downsample(_squeeze_2d(region), max_size)


def _fetch_spans(fh: HttpFile, spans: list[tuple[int, int]], workers: int = 8) -> list[bytes]:
    """Fetch many small byte ranges of one file, in parallel. Bulk transfers
    on BIL are sequential on purpose (8 streams measured slower than 1 for
    16 MB slices), but a preview that samples 512 rows 20 MB apart is
    latency-bound, not bandwidth-bound: 512 sequential range requests took
    68 s for 63 MB. Eight workers bring that to a few seconds. Order of the
    result matches ``spans``."""
    import concurrent.futures as cf

    def one(span: tuple[int, int]) -> bytes:
        start, length = span
        if length <= 0:
            return b""
        return fh._fetch(start, start + length - 1)

    if len(spans) <= 2:
        return [one(sp) for sp in spans]
    with cf.ThreadPoolExecutor(max_workers=min(workers, len(spans))) as ex:
        return list(ex.map(one, spans))


def _decode_with_pillow(
    fp: "IO[bytes]", key: int | Sequence[int] | None, url: str, cause: BaseException
) -> np.ndarray:
    """Second decoder for TIFFs tifffile rejects. Seen on BIL 2026-09-08:
    ImageJ-style Deflate stacks (``ace-owl-cot``, 253 pages, rowsperstrip
    11) where libdeflate reports INSUFFICIENT_SPACE because a strip holds
    more decompressed bytes than its declared rows; Pillow's zlib path
    tolerates the surplus and decodes the same pixels."""
    try:
        from PIL import Image, ImageSequence
    except ImportError as exc:
        raise BilError(f"tifffile could not decode {url} ({cause}) and Pillow is not installed") from exc
    # Scientific planes routinely exceed Pillow's 89-megapixel "decompression
    # bomb" threshold (a 13107 x 11265 Patch-seq slide is 148 Mpx); this is
    # trusted archive data, so lift the guard for our own decode only.
    Image.MAX_IMAGE_PIXELS = None
    try:
        im = Image.open(fp)
        if key is None:
            frames = [np.asarray(f) for f in ImageSequence.Iterator(im)]
            return frames[0] if len(frames) == 1 else np.stack(frames, axis=0)
        keys = [key] if isinstance(key, int) else list(key)
        out = []
        for k in keys:
            im.seek(k)
            out.append(np.asarray(im))
        return out[0] if isinstance(key, int) else np.stack(out, axis=0)
    except Exception as exc:
        raise BilError(f"neither tifffile ({cause}) nor Pillow ({exc}) could decode {url}") from exc


def tiff_info(target: FileEntry | str) -> dict[str, Any]:
    """Shape, dtype, page count, tiling and pyramid levels of a remote TIFF
    from its metadata alone (IFD walk over range requests, no pixel data).
    Use it before read_tiff() on anything that is not a single-slice file:
    a BIL OME-TIFF can be a 21 GB, 92-page BigTIFF whose one page is a
    1 GB 33210 x 14904 plane."""
    import tifffile

    url, size = _entry_of(target)
    fh = HttpFile(url, size=size)
    with tifffile.TiffFile(cast("IO[bytes]", fh)) as tif:
        page0: Any = tif.pages[0]
        series: Any = None
        try:
            # tifffile raises "incompatible keyframe" building series for
            # some multi-page files whose pages differ in shape; pages still
            # read fine one at a time, so report what we can.
            series = tif.series[0] if tif.series else None
        except Exception:
            series = None
        levels = len(series.levels) if series is not None and hasattr(series, "levels") else 1
        return {
            "url": url,
            "bytes": size,
            "bigtiff": bool(tif.is_bigtiff),
            "ome": bool(tif.is_ome),
            "pages": len(tif.pages),
            "series": len(tif.series),
            "shape": tuple(int(x) for x in (series.shape if series is not None else page0.shape)),
            "axes": str(series.axes) if series is not None else "",
            "dtype": str(page0.dtype),
            "tiled": bool(page0.is_tiled),
            "chunks": tuple(int(x) for x in getattr(page0, "chunks", ()) or ()),
            "pyramid_levels": levels,
            "metadata_bytes_read": fh.bytes_fetched,
            "metadata_requests": fh.requests_made,
        }


def read_region(
    target: FileEntry | str,
    rows: slice | tuple[int | None, int | None] = (None, None),
    cols: slice | tuple[int | None, int | None] = (None, None),
    page: int = 0,
    level: int = 0,
) -> np.ndarray:
    """A rectangular region of one page of a remote TIFF, fetching only the
    tiles or strips that cover it. ``rows``/``cols`` are ``(start, stop)``
    pairs or slices in pixels of the chosen pyramid ``level`` (0 = full
    resolution). Requires ``pip install scigantic-bil[zarr]``: the tile
    map is exposed through tifffile's zarr store."""
    import tifffile

    try:
        import zarr
    except ImportError as exc:
        raise ImportError("zarr is not installed; pip install 'scigantic-bil[zarr]'") from exc
    url, size = _entry_of(target)
    fh = HttpFile(url, size=size)
    r = rows if isinstance(rows, slice) else slice(rows[0], rows[1])
    c = cols if isinstance(cols, slice) else slice(cols[0], cols[1])
    with tifffile.TiffFile(cast("IO[bytes]", fh)) as tif:
        store = tif.aszarr(key=page, level=level) if level else tif.aszarr(key=page)
        try:
            arr: Any = zarr.open(store, mode="r")
            plane = arr[r, c] if arr.ndim == 2 else arr[..., r, c]
        finally:
            store.close()
    return np.asarray(plane)


def slices(target: str | Dataset | DatasetDetail, channel: str | None = None) -> list[FileEntry]:
    """The TIFF and JPEG 2000 files of a dataset in natural z order; the
    raw material of a stack. ``channel`` keeps only names containing that
    substring (``"ch02"``) when a directory interleaves channels."""
    entries = find(target, suffix=_IMAGE_SUFFIXES, recursive=True)
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
        raise BilError(f"no TIFF or JPEG 2000 slices found under {resolve_url(target)}")
    planes = [_squeeze_2d(read_image(e)) for e in entries]
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
    """A small 2-D preview of a dataset, reading as little as possible and
    never crawling a whole tree. In order: a FileEntry is read directly;
    a TeraFly (fMOST) tree's coarsest resolution folder, stitched; an
    OME-Zarr store's coarsest level; else the first folder of TIFF or
    JPEG 2000 slices found by a bounded descent, middle slice. ``index`` picks a slice; ``channel``
    keeps only file names containing it (``"ch02"``)."""
    if isinstance(target, FileEntry):
        return preview_plane(target, max_size=max_size, page=index)
    # TeraFly first: it is two listings and rules out the crawl below on
    # the trees where that crawl is most expensive.
    levels = terafly_levels(target)
    if levels:
        return terafly_thumbnail(target, max_size=max_size, levels=levels)
    stores = find_zarr(target)
    if stores:
        return zarr_thumbnail(stores[0], max_size=max_size, index=index)
    stack = first_images(target, suffix=_IMAGE_SUFFIXES)
    if channel:
        stack = [e for e in stack if channel.lower() in e.name.lower()]
    if stack:
        i = len(stack) // 2 if index is None else index
        # One file per z-plane: preview the chosen slice. One multi-page or
        # giant file: preview_plane picks a middle page and bounds the read.
        return preview_plane(stack[i], max_size=max_size)
    exts = extensions_under(target)
    raise UnsupportedFormatError(
        f"no TIFF or JPEG 2000 slices, zarr store or TeraFly tree under {resolve_url(target)}; "
        f"found extensions {exts}. " + _format_hint(exts)
    )


def _format_hint(exts: list[str]) -> str:
    hints = {
        ".swc": "SWC neuron reconstructions: navis.read_swc(url) reads them directly.",
        ".nii": "NIfTI volumes: nibabel.load() after bil.download().",
        ".nii.gz": "NIfTI volumes: nibabel.load() after bil.download().",
        ".h5": "HDF5: h5py after bil.download(), or h5py with fsspec's HTTPFileSystem.",
        ".ims": "Imaris .ims is HDF5: h5py after bil.download().",
        ".dax": "MERFISH raw .dax frames: uint16 binary, shape in the sibling .inf file; np.memmap after bil.download().",
        ".h5ad": "AnnData tables: anndata.read_h5ad() after bil.download().",
        ".csv": "Tables, not images: pandas.read_csv(url) works directly.",
    }
    for ext in exts:
        if ext in hints:
            return hints[ext]
    return ""


_RES_RE = re.compile(r"^RES_(\d+)x(\d+)x(\d+)_?$", re.IGNORECASE)


def terafly_levels(target: str | Dataset | DatasetDetail) -> list[tuple[tuple[int, int, int], str]]:
    """Resolution levels of a TeraFly / TeraStitcher tree, coarsest first,
    as ``((x, y, z), url)``. fMOST brains on BIL (985 datasets) ship this
    layout: ``RES_<x>x<y>x<z>_/<Y>/<Y>_<X>/<Y>_<X>_<Z>.tif`` with the full
    pyramid down to a few hundred voxels a side, so a preview never has to
    touch the full-resolution folder (1.6 M files on ace-cap-cop). Looks at
    the dataset root and one level down. Empty list if not TeraFly."""
    root = resolve_url(target)
    for url in [root] + [e.url for e in list_files(root) if e.is_dir and not e.name.lower().endswith((".zarr", ".n5"))][:8]:
        found: list[tuple[tuple[int, int, int], str]] = []
        for e in list_files(url):
            m = _RES_RE.match(e.name) if e.is_dir else None
            if m:
                found.append(((int(m.group(1)), int(m.group(2)), int(m.group(3))), e.url))
        if found:
            return sorted(found, key=lambda t: t[0][0] * t[0][1] * t[0][2])
    return []


def terafly_thumbnail(
    target: str | Dataset | DatasetDetail,
    max_size: int = 512,
    levels: list[tuple[tuple[int, int, int], str]] | None = None,
    level: int = 0,
) -> np.ndarray:
    """One xy plane through the middle of a TeraFly tree at resolution
    ``level`` (0 = coarsest), stitched from that level's blocks: one page
    read per block column, ~25 small TIFFs at the coarsest level."""
    levels = levels if levels is not None else terafly_levels(target)
    if not levels:
        raise UnsupportedFormatError(f"no TeraFly RES_ folders under {resolve_url(target)}")
    _, level_url = levels[min(level, len(levels) - 1)]
    import concurrent.futures as cf

    import tifffile

    y_dirs = sorted((e for e in list_files(level_url) if e.is_dir), key=lambda e: _natural_key(e.name))

    def x_dirs_of(y: FileEntry) -> list[FileEntry]:
        return sorted((e for e in list_files(y.url) if e.is_dir), key=lambda e: _natural_key(e.name))

    def middle_block(x: FileEntry) -> FileEntry | None:
        blocks = sorted(
            (e for e in list_files(x.url) if not e.is_dir and e.name.lower().endswith(_TIFF_SUFFIXES)),
            key=lambda e: _natural_key(e.name),
        )
        return blocks[len(blocks) // 2] if blocks else None

    def middle_page(block: FileEntry) -> np.ndarray:
        url, _ = _entry_of(block)
        resp = send("GET", url, timeout=300.0)
        data = resp.content
        resp.close()
        with tifffile.TiffFile(io.BytesIO(data)) as tif:
            n = len(tif.pages)
            return np.asarray(tif.pages[n // 2].asarray())

    # Listings and block reads are many small requests: latency-bound, so
    # each stage runs through a small pool (see _fetch_spans). Three flat
    # stages rather than nested maps: a pool waiting on its own tasks
    # starves itself.
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        grid: list[list[FileEntry]] = list(ex.map(x_dirs_of, y_dirs))
        flat_x = [x for xs in grid for x in xs]
        blocks_flat: list[FileEntry | None] = list(ex.map(middle_block, flat_x))
        present = [b for b in blocks_flat if b is not None]
        pages = dict(zip((b.url for b in present), ex.map(middle_page, present)))
    rows: list[np.ndarray] = []
    i = 0
    for xs in grid:
        tiles = []
        for _ in xs:
            b = blocks_flat[i]
            i += 1
            if b is not None:
                tiles.append(pages[b.url])
        if tiles:
            h = min(t.shape[0] for t in tiles)
            rows.append(np.concatenate([t[:h] for t in tiles], axis=1))
    if not rows:
        raise UnsupportedFormatError(f"TeraFly level at {level_url} has no readable blocks")
    w = min(r.shape[1] for r in rows)
    plane = np.concatenate([r[:, :w] for r in rows], axis=0)
    return downsample(plane, max_size)


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
