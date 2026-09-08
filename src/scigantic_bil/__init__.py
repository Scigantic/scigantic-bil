"""scigantic-bil: search the Brain Image Library and read its volumes over
HTTP without downloading them.

    import scigantic_bil as bil

    cat = bil.BilCatalog.load()          # every dataset, from BIL's daily inventory
    ls = cat.light_sheet()               # light-sheet datasets, technique + fulltext
    d = bil.retrieve(ls[0].bildid)       # full record: abstract, instrument, rights
    files = bil.slices(d)                # z-slice TIFFs, in order, nothing fetched
    img = bil.thumbnail(d)               # one middle slice, downsampled
"""

from ._client import BilError, BilNotFoundError
from ._version import __version__
from .api import fulltext, query, retrieve, retrieve_many
from .cache import cache_dir, clear as clear_cache, disable_cache, enable_cache, is_cache_enabled
from .catalog import BilCatalog, available_inventory_dates
from .files import download, find, find_zarr, is_store_dir, list_files, manifest, resolve_url, walk
from .images import (
    HttpFile,
    UnsupportedFormatError,
    downsample,
    open_zarr,
    read_stack,
    read_tiff,
    slices,
    thumbnail,
    zarr_levels,
    zarr_thumbnail,
)
from .models import Contributor, Dataset, DatasetDetail, FileEntry, Publication, dataset_url

__all__ = [
    "__version__",
    "BilError",
    "BilNotFoundError",
    "UnsupportedFormatError",
    "BilCatalog",
    "available_inventory_dates",
    "Dataset",
    "DatasetDetail",
    "Contributor",
    "Publication",
    "FileEntry",
    "dataset_url",
    "query",
    "fulltext",
    "retrieve",
    "retrieve_many",
    "list_files",
    "manifest",
    "walk",
    "find",
    "find_zarr",
    "is_store_dir",
    "download",
    "resolve_url",
    "read_tiff",
    "read_stack",
    "slices",
    "thumbnail",
    "downsample",
    "open_zarr",
    "zarr_levels",
    "zarr_thumbnail",
    "HttpFile",
    "enable_cache",
    "disable_cache",
    "is_cache_enabled",
    "cache_dir",
    "clear_cache",
]
