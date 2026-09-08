from __future__ import annotations

import pytest

import scigantic_bil as bil
from tests.conftest import TIFF_STACK, ZARR_STORE


def test_resolve_url_forms() -> None:
    want = "https://download.brainimagelibrary.org/42/e4/42e4274e0579397f/subject_5/"
    assert bil.resolve_url("/bil/data/42/e4/42e4274e0579397f/subject_5") == want
    assert bil.resolve_url(want.rstrip("/")) == want
    assert bil.resolve_url(ZARR_STORE) == want
    assert bil.dataset_url("/bil/data/") == "https://download.brainimagelibrary.org/"


def test_list_files_parses_nginx_autoindex() -> None:
    entries = bil.list_files(TIFF_STACK)
    assert len(entries) == 1923
    first = entries[0]
    assert first.name == "Z00001_ch02.tif" and not first.is_dir
    assert first.size == 50912 and first.modified.startswith("15-Dec-2021")
    assert first.url.endswith("/LSFM/Neurotrace/Z00001_ch02.tif")
    assert first.extension == ".tif"
    assert bil.FileEntry("a.ome.tif", "u", 1, "", False).extension == ".ome.tif"
    assert bil.FileEntry("x.nii.gz", "u", 1, "", False).extension == ".nii.gz"
    assert bil.FileEntry("noext", "u", 1, "", False).extension == ""


def test_walk_does_not_enter_zarr_stores() -> None:
    entries = list(bil.walk(ZARR_STORE))
    assert len(entries) == 1
    assert entries[0].name == "subject_5.zarr" and bil.is_store_dir(entries[0])


def test_find_zarr() -> None:
    stores = bil.find_zarr(ZARR_STORE)
    assert stores == ["https://download.brainimagelibrary.org/42/e4/42e4274e0579397f/subject_5/subject_5.zarr/"]
    assert bil.find_zarr(TIFF_STACK) == []


def test_find_sorts_slices_naturally() -> None:
    hits = bil.find(TIFF_STACK, suffix=".tif", recursive=False)
    names = [h.name for h in hits]
    assert names[:3] == ["Z00001_ch02.tif", "Z00002_ch02.tif", "Z00003_ch02.tif"]
    assert names[-1] == "Z01923_ch02.tif"


def test_missing_path_is_not_found() -> None:
    with pytest.raises(bil.BilNotFoundError):
        bil.list_files("https://download.brainimagelibrary.org/00/00/doesnotexist/")


def test_download_small_file(tmp_path: object) -> None:
    from pathlib import Path

    entries = bil.list_files(TIFF_STACK)
    small = min(entries, key=lambda e: e.size or 1 << 60)
    out = bil.download(small, Path(str(tmp_path)))
    assert out.exists() and out.stat().st_size == small.size


def test_manifest_lists_every_file_with_md5() -> None:
    entries = bil.manifest(TIFF_STACK)
    assert len(entries) == 1923
    e = next(x for x in entries if x.name == "Z00001_ch02.tif")
    assert e.size == 50912 and len(e.md5) == 32 and e.path == "Z00001_ch02.tif"
    assert e.url.endswith("/LSFM/Neurotrace/Z00001_ch02.tif")
    zarr_entries = bil.manifest(ZARR_STORE)
    assert len(zarr_entries) == 15323
    assert any(x.path == "subject_5.zarr/.zattrs" for x in zarr_entries)


def test_walk_uses_manifest_and_collapses_store() -> None:
    entries = list(bil.walk(ZARR_STORE))
    assert [e.name for e in entries] == ["subject_5.zarr"]
    assert entries[0].url.endswith("/subject_5/subject_5.zarr/")
    flat = list(bil.walk(TIFF_STACK))
    assert len(flat) == 1923 and all(not e.is_dir for e in flat)


def test_manifest_rejects_non_id() -> None:
    with pytest.raises(ValueError):
        bil.manifest("https://download.brainimagelibrary.org/x/")
    with pytest.raises(bil.BilNotFoundError):
        bil.manifest("zzz-zzz-zzz")
