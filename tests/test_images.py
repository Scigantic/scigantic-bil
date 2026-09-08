from __future__ import annotations

import io

import numpy as np
import pytest

import scigantic_bil as bil
from tests.conftest import TIFF_STACK, ZARR_STORE


def test_read_one_light_sheet_slice() -> None:
    stack = bil.slices(TIFF_STACK)
    assert len(stack) == 1923
    mid = stack[len(stack) // 2]
    img = bil.read_tiff(mid)
    assert img.shape == (4501, 3828) and img.dtype == np.uint16
    assert img.max() > 0


def test_httpfile_reads_only_what_tifffile_asks_for() -> None:
    import tifffile

    entry = bil.slices(TIFF_STACK)[10]
    fh = bil.HttpFile(entry.url, size=entry.size)
    with tifffile.TiffFile(fh) as tif:
        arr = np.asarray(tif.asarray())
    assert arr.shape == (4501, 3828)
    assert 0 < fh.bytes_fetched <= (entry.size or 0)
    assert fh.requests_made >= 1
    # seek/tell semantics a file object must honour
    fh.seek(0)
    assert fh.read(4) in (b"II*\x00", b"MM\x00*")
    fh.seek(-2, io.SEEK_END)
    assert len(fh.read()) == 2 and fh.tell() == fh.size


def test_httpfile_head_fallback_when_size_unknown() -> None:
    entry = bil.slices(TIFF_STACK)[0]
    fh = bil.HttpFile(entry.url)
    assert fh.size == entry.size


def test_thumbnail_tiff_stack_is_small_and_fast() -> None:
    th = bil.thumbnail(TIFF_STACK, max_size=256)
    assert th.ndim == 2 and max(th.shape) <= 256 and th.dtype == np.uint16
    th2 = bil.thumbnail(TIFF_STACK, max_size=128, index=5)
    assert max(th2.shape) <= 128


def test_read_stack_subsamples_z() -> None:
    vol = bil.read_stack(TIFF_STACK, start=900, stop=906, step=3)
    assert vol.shape == (2, 4501, 3828)


def test_downsample_stride() -> None:
    a = np.arange(1000 * 700, dtype=np.uint16).reshape(1000, 700)
    d = bil.downsample(a, 100)
    assert max(d.shape) <= 100 and d[0, 0] == a[0, 0]
    assert bil.downsample(np.zeros(5), 2).shape == (5,)


def test_unsupported_formats_name_the_alternative() -> None:
    with pytest.raises(bil.UnsupportedFormatError, match="glymur"):
        bil.read_tiff("https://download.brainimagelibrary.org/x/y/z/section.jp2")
    with pytest.raises(bil.UnsupportedFormatError, match="h5py"):
        bil.read_tiff("https://download.brainimagelibrary.org/x/y/z/volume.ims")
    with pytest.raises(bil.UnsupportedFormatError):
        bil.read_tiff("https://download.brainimagelibrary.org/x/y/z/notes.txt")


def test_open_zarr_and_levels() -> None:
    g = bil.open_zarr(ZARR_STORE)
    levels = bil.zarr_levels(g)
    # .zattrs declares 8 levels, the server has 7: levels must reflect the server.
    assert levels == ["0", "1", "2", "3", "4", "5", "6"]
    assert tuple(g["0"].shape) == (1, 1, 848, 6300, 9600)


def test_zarr_thumbnail_reads_coarsest_level() -> None:
    th = bil.zarr_thumbnail(bil.open_zarr(ZARR_STORE), max_size=256)
    assert th.ndim == 2 and max(th.shape) <= 256 and th.max() > 0
    # thumbnail() on the dataset id should pick the store, not crawl chunks
    th2 = bil.thumbnail(ZARR_STORE, max_size=256)
    assert th2.shape == th.shape


def test_dataset_without_images_raises_clearly() -> None:
    # A folder with only a zarr store's parent listing and no TIFFs is
    # handled above; here, an empty-ish inventory directory.
    with pytest.raises((bil.UnsupportedFormatError, bil.BilNotFoundError)):
        bil.thumbnail("https://download.brainimagelibrary.org/inventory/datasets/parquet/")
