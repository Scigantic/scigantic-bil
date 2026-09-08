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
from .files import (
    MANIFEST_MAX_BYTES,
    ManifestTooLargeError,
    download,
    extensions_under,
    find,
    find_zarr,
    first_images,
    is_store_dir,
    list_files,
    manifest,
    manifest_size,
    resolve_url,
    walk,
)
from .images import (
    HttpFile,
    UnsupportedFormatError,
    downsample,
    open_zarr,
    preview_plane,
    read_image,
    read_jp2,
    read_region,
    read_stack,
    read_tiff,
    slices,
    terafly_levels,
    terafly_thumbnail,
    thumbnail,
    tiff_info,
    zarr_levels,
    zarr_thumbnail,
)
from .models import (
    Contributor,
    Dataset,
    DatasetDetail,
    FileEntry,
    Publication,
    dataset_url,
    encode_path,
    encode_url,
)

__all__ = [
    "__version__",
    "BilError",
    "BilNotFoundError",
    "UnsupportedFormatError",
    "ManifestTooLargeError",
    "MANIFEST_MAX_BYTES",
    "BilCatalog",
    "available_inventory_dates",
    "Dataset",
    "DatasetDetail",
    "Contributor",
    "Publication",
    "FileEntry",
    "dataset_url",
    "encode_path",
    "encode_url",
    "query",
    "fulltext",
    "retrieve",
    "retrieve_many",
    "list_files",
    "manifest",
    "manifest_size",
    "walk",
    "find",
    "find_zarr",
    "first_images",
    "is_store_dir",
    "download",
    "extensions_under",
    "resolve_url",
    "read_tiff",
    "read_jp2",
    "read_image",
    "read_region",
    "preview_plane",
    "tiff_info",
    "read_stack",
    "slices",
    "thumbnail",
    "terafly_levels",
    "terafly_thumbnail",
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
