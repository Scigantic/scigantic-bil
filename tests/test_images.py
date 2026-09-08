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
    # block rounding and one direct span can overlap a cached block slightly
    assert 0 < fh.bytes_fetched <= (entry.size or 0) * 1.1
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
    with pytest.raises(bil.UnsupportedFormatError, match="h5py"):
        bil.read_tiff("https://download.brainimagelibrary.org/x/y/z/volume.ims")
    with pytest.raises(bil.UnsupportedFormatError):
        bil.read_tiff("https://download.brainimagelibrary.org/x/y/z/notes.txt")


def test_open_zarr_and_levels() -> None:
    pytest.importorskip("zarr")
    g = bil.open_zarr(ZARR_STORE)
    levels = bil.zarr_levels(g)
    # .zattrs declares 8 levels, the server has 7: levels must reflect the server.
    assert levels == ["0", "1", "2", "3", "4", "5", "6"]
    assert tuple(g["0"].shape) == (1, 1, 848, 6300, 9600)


def test_zarr_thumbnail_reads_coarsest_level() -> None:
    pytest.importorskip("zarr")
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


def test_open_zarr_without_zarr_installed_is_a_clear_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def no_zarr(name: str, *a: object, **k: object) -> object:
        if name == "zarr" or name.startswith("zarr."):
            raise ImportError("simulated missing zarr")
        return real_import(name, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", no_zarr)
    with pytest.raises(ImportError, match="scigantic-bil\\[zarr\\]"):
        bil.open_zarr(ZARR_STORE)


def test_httpfile_large_reads_bypass_block_cache() -> None:
    entry = bil.slices(TIFF_STACK)[20]
    fh = bil.HttpFile(entry.url, size=entry.size)
    fh.seek(0)
    small = fh.read(1000)  # block cache: one 256 KB request
    assert len(small) == 1000 and fh.requests_made == 1 and fh.bytes_fetched == 256 << 10
    fh.seek(1 << 20)
    big = fh.read(2 << 20)  # direct: exactly one 2 MB request
    assert len(big) == 2 << 20 and fh.requests_made == 2 and fh.bytes_fetched == (256 << 10) + (2 << 20)


def test_tiff_info_and_region_on_21gb_ome_tiff() -> None:
    pytest.importorskip("zarr")
    entry = [f for f in bil.list_files("ace-dud-fib") if f.name.endswith(".ome.tiff")][0]
    info = bil.tiff_info(entry)
    assert info["bigtiff"] and info["ome"] and info["pages"] == 92 and info["tiled"]
    assert info["shape"][-2:] == (33210, 14904)
    assert info["metadata_bytes_read"] < 40_000_000  # 25 MB measured with 256 KB blocks, 81 MB with 1 MB
    region = bil.read_region(entry, rows=(16000, 16512), cols=(7000, 7512))
    assert region.shape == (512, 512) and region.dtype == np.uint16


def test_preview_plane_bounds_reads_on_a_10gb_plane() -> None:
    import time

    entries = bil.first_images("ace-dud-vow")
    entry = next(e for e in entries if e.name == "mosaic_DAPI_z3.tif")
    assert entry.size and entry.size > 10_000_000_000
    t0 = time.time()
    th = bil.preview_plane(entry, max_size=128)
    assert th.ndim == 2 and max(th.shape) <= 128 and th.max() > 0
    assert time.time() - t0 < 120


def test_preview_plane_strip_sampling_keeps_width() -> None:
    entry = bil.first_images("ace-ace-leg")[30]  # 5-page 9660 x 16644 stripped file, 1.6 GB
    th = bil.preview_plane(entry, max_size=128)
    assert th.ndim == 2 and th.shape[1] > 16


def test_pillow_fallback_decodes_imagej_deflate_stack() -> None:
    entries = bil.first_images("ace-owl-cot")
    entry = next(e for e in entries if e.name == "atlaslabel_def.tif")
    a = bil.read_tiff(entry)  # tifffile+libdeflate: INSUFFICIENT_SPACE; Pillow decodes it
    assert a.shape == (253, 575, 377) and a.dtype == np.uint16


def test_terafly_levels_and_thumbnail() -> None:
    levels = bil.terafly_levels("ace-cap-cop")
    assert [d for d, _ in levels][0] == (793, 1268, 350) and len(levels) == 6
    th = bil.thumbnail("ace-cap-cop", max_size=256)
    assert th.ndim == 2 and max(th.shape) <= 256 and th.max() > 0
    assert bil.terafly_levels(TIFF_STACK) == []


def test_thumbnail_error_names_the_right_tool() -> None:
    with pytest.raises(bil.UnsupportedFormatError, match="navis"):
        bil.thumbnail("ace-nap-out")  # .swc only


def test_read_jp2_stpt_section_exact() -> None:
    entries = bil.slices("ace-bit-wig")  # STPT, 267 x ~12 MB JPEG 2000 sections
    assert entries and entries[0].extension == ".jp2"
    a = bil.read_jp2(entries[len(entries) // 2])
    assert a.shape == (11377, 8557) and a.dtype == np.uint16 and a.max() > 1000
    b = bil.read_image(entries[len(entries) // 2])
    assert b.shape == a.shape
    with pytest.raises(bil.UnsupportedFormatError, match="read_jp2"):
        bil.read_tiff(entries[0])


def test_read_jp2_reduced_resolution_rgb_section() -> None:
    entries = bil.first_images("ace-bed-rag")  # Dong-lab tracing, 75 MB RGB sections
    entry = entries[len(entries) // 2]
    a = bil.read_jp2(entry, reduce=3)
    assert a.ndim == 3 and a.shape[-1] == 3 and a.shape[0] == 12000 // 8 and a.shape[1] == 16000 // 8


def test_jp2_thumbnails_across_producers() -> None:
    import time

    for bildid, ndim in (("ace-bit-wig", 2), ("ace-zip-hen", 3)):
        t0 = time.time()
        th = bil.thumbnail(bildid, max_size=256)
        assert th.ndim == ndim and max(th.shape[:2]) <= 256 and th.max() > 0
        assert time.time() - t0 < 120
