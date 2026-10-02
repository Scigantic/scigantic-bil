"""BrAinPI: BIL's on-the-fly OME-Zarr and Neuroglancer views.

BIL runs BrAinPI (github.com/CBI-PITT/BrAinPI) at
``https://brainapi.brainimagelibrary.org``. Given the disk path of a
multiscale file it holds (Imaris ``.ims``, ``.omehans``, TeraFly, an
existing OME-Zarr store, JPEG 2000) it serves that file as an OME-Zarr
pyramid, as Neuroglancer precomputed, and as OpenSeadragon tiles, without
converting anything on disk. Measured 2026-10-02: a three-channel
12000 x 16000 x 140 ``.omehans`` opens as 13 pyramid levels in 20 s and a
64 x 64 read takes 0.3 s; an ``.ims`` opens in 2.6 s.

Which files are eligible comes from the metadata API: each ``/retrieve``
record carries ``Assets.brainpiroot`` (a directory) and
``Assets.brainpidata`` (files under it). 417 of 1,500 sampled datasets
(28 percent) have entries; the files are overwhelmingly ``.ims``, then
``.jp2``, ``.omehans`` and ``.terafly``. That list is BIL's curation, not
the service's limit: BrAinPI also serves a single TIFF (as a 2-D pyramid
of that one slice) and an existing ``.ome.zarr`` store whether or not
they are listed, while a directory of slices and a missing file get
nothing, so brainpi_links() asks the service about the exact path.

The view URLs are not a simple prefix of the disk path (``/bil/data/...``
becomes ``bil_data/...`` while ``/bil/assets/...`` stays), so they are
always taken from the service's own answer, never assembled here.

JPEG 2000 views were not usable when checked: one ``.jp2`` view answered
neither in 120 s nor in 410 s. The link is returned; opening it is the
caller's risk.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from . import cache
from ._client import DOWNLOAD_BASE, send
from .models import Dataset, DatasetDetail, FileEntry

BRAINPI_BASE = "https://brainapi.brainimagelibrary.org"
_LINKS_URL = f"{BRAINPI_BASE}/path_to_html_options/"

# brainpi_views() asks the service about this many files of a dataset by
# default. One MORF dataset lists 2,098 Imaris tiles; nobody previewing it
# wants 2,098 requests. Pass limit=None for every file.
VIEWS_DEFAULT_LIMIT = 32


@dataclass(frozen=True)
class BrainpiViews:
    """Every view BrAinPI offers for one file, or None where it offers
    none. ``path`` is the disk path asked about. ``neuroglancer`` is a
    precomputed source (``precomputed://`` + this URL in Neuroglancer);
    ``omezarr`` opens with zarr, fsspec or napari as it stands;
    ``omezarr_8bit`` and ``omezarr_ng`` are the 8-bit and the
    channel-chunked variants; ``openseadragon`` is a tile source."""

    path: str
    neuroglancer: str | None
    neuroglancer_info: str | None
    omezarr: str | None
    omezarr_8bit: str | None
    omezarr_ng: str | None
    openseadragon: str | None
    raw: dict[str, Any]

    @property
    def available(self) -> bool:
        """True when BrAinPI serves this path in at least one form."""
        return any((self.neuroglancer, self.omezarr, self.openseadragon))

    @classmethod
    def from_json(cls, body: dict[str, Any]) -> BrainpiViews:
        def opt(key: str) -> str | None:
            v = body.get(key)
            return str(v) if v else None

        return cls(
            path=str(body.get("path") or ""),
            neuroglancer=opt("neuroglancer"),
            neuroglancer_info=opt("neuroglancer_metadata"),
            omezarr=opt("omezarr"),
            omezarr_8bit=opt("omezarr_8bit"),
            omezarr_ng=opt("omezarr_neuroglancer_optimized"),
            openseadragon=opt("openseadragon"),
            raw=body,
        )


def disk_path(target: str | FileEntry) -> str:
    """The BIL filesystem path BrAinPI wants, from a ``/bil/...`` path, a
    download-server URL or a FileEntry. The download server publishes
    ``/bil/data/`` at its root, so its URLs map back to ``/bil/data/<path>``
    (percent-decoding undone: BrAinPI takes the raw name)."""
    url = target.url if isinstance(target, FileEntry) else target
    if url.startswith(DOWNLOAD_BASE):
        rest = urlsplit(url).path
        return "/bil/data/" + unquote(rest).strip("/")
    if url.startswith("/bil/"):
        return url.rstrip("/") or url
    raise ValueError(
        f"BrAinPI needs a /bil/... path or a download-server URL, got {url!r}"
    )


def brainpi_links(target: str | FileEntry) -> BrainpiViews:
    """Ask BrAinPI which views it offers for one file or store. One
    request, cached; every field is None when it serves nothing there.
    Accepts a ``/bil/...`` disk path, a download-server URL or a
    FileEntry."""
    path = disk_path(target)
    params = {"path": path}
    cached = cache.get("brainpi", _LINKS_URL, params)
    if cached is None:
        resp = send("GET", _LINKS_URL, params=params, timeout=120.0)
        cached = resp.json()
        resp.close()
        if not isinstance(cached, dict):
            cached = {"path": path}
        cache.put("brainpi", _LINKS_URL, params, cached)
    return BrainpiViews.from_json(dict(cached))


def brainpi_paths(detail: DatasetDetail) -> list[str]:
    """Disk paths of every file BIL lists for BrAinPI on this dataset,
    from the metadata record alone (no request). Empty when BIL lists
    none."""
    root = detail.brainpiroot.rstrip("/")
    if not root:
        return []
    return [f"{root}/{f.lstrip('/')}" for f in detail.brainpidata if f]


def brainpi_views(
    target: str | Dataset | DatasetDetail, limit: int | None = VIEWS_DEFAULT_LIMIT
) -> list[BrainpiViews]:
    """The BrAinPI views for a dataset's listed files, in BIL's order,
    one request per file (the first ``limit``; None for all). Files the
    service declines are kept with their fields None, so the list lines
    up with ``brainpi_paths()``. A dataset BIL lists nothing for returns
    an empty list without asking the service."""
    detail = _detail_of(target)
    paths = brainpi_paths(detail)
    if limit is not None:
        paths = paths[:limit]
    return [brainpi_links(p) for p in paths]


def first_omezarr(target: str | Dataset | DatasetDetail) -> str | None:
    """URL of the first OME-Zarr view BrAinPI offers for a dataset, or
    None. Stops at the first file the service serves."""
    detail = _detail_of(target)
    for path in brainpi_paths(detail):
        views = brainpi_links(path)
        if views.omezarr:
            return views.omezarr
    return None


def _detail_of(target: str | Dataset | DatasetDetail) -> DatasetDetail:
    if isinstance(target, DatasetDetail):
        return target
    from .api import retrieve

    bildid = target.bildid if isinstance(target, Dataset) else target
    return retrieve(bildid)
