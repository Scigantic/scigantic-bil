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


def test_paths_with_hash_space_and_unicode_are_encoded() -> None:
    # 39 inventory rows carry '#', spaces or non-ASCII; an unencoded '#' is
    # a URL fragment and 404'd. The server lists the directory as %236.
    url = bil.dataset_url("/bil/data/ca/27/ca273783c1dba805/2019Q1_U01Zhang/Virus_tracing-B1-#6/")
    assert url.endswith("/Virus_tracing-B1-%236/")
    assert bil.encode_url(url) == url  # idempotent
    assert bil.encode_path("S100β_staining") == "S100%CE%B2_staining"
    entries = bil.list_files("ace-act-cow")
    assert len(entries) == 44744
    assert entries[0].name.startswith("SC_SLICE") and "%23" in entries[0].url


def test_listing_names_are_decoded_urls_stay_encoded() -> None:
    from scigantic_bil.files import _parse_listing

    html = '<a href="Virus_tracing-B1-%236/">Virus_tracing-B1-%236/</a>  15-Dec-2021 01:58  -\n'
    rows = _parse_listing(html, "https://x/y/")
    assert rows[0]["name"] == "Virus_tracing-B1-#6" and rows[0]["url"] == "https://x/y/Virus_tracing-B1-%236/"


def test_two_word_and_one_word_ids_are_accepted() -> None:
    # 748 two-word and 113 one-word ids in the 2026-07-31 inventory.
    from scigantic_bil.files import _bildid_of

    assert _bildid_of("act-nod") == "act-nod"
    assert _bildid_of("ace") == "ace"
    assert _bildid_of("ace-cup-eel") == "ace-cup-eel"
    assert _bildid_of("https://x/") is None
    assert bil.manifest("act-nod")


def test_manifest_size_guard() -> None:
    size = bil.manifest_size("ace-owl-cot")  # 4.7 M files
    assert size is not None and size > 500_000_000
    with pytest.raises(bil.ManifestTooLargeError, match="pass max_bytes=None"):
        bil.manifest("ace-owl-cot")
    # An already-cached manifest is returned regardless of max_bytes (no
    # download cost), so the limit is exercised on an id no other test loads.
    with pytest.raises(bil.ManifestTooLargeError):
        bil.manifest("ace-cab-leg", max_bytes=1000)
    assert bil.manifest_size("zzz-zzz-zzz") is None


def test_find_zarr_is_bounded_on_huge_trees() -> None:
    import time

    t0 = time.time()
    assert bil.find_zarr("ace-cap-cop") == []  # 1.6 M-file TeraFly tree, manifest too large
    assert time.time() - t0 < 60


def test_first_images_bounded_descent() -> None:
    hits = bil.first_images("ace-owl-cot")  # 4.7 M files, one request per level
    assert hits and all(h.name.lower().endswith((".tif", ".tiff")) for h in hits)
    assert bil.first_images("ace-nap-out") == []  # SWC-only dataset


def test_landing_zone_paths_are_refused_clearly() -> None:
    with pytest.raises(bil.BilNotFoundError, match="landing zone"):
        bil.resolve_url("/bil/lz/bakerk/360338984d84469c/702265")


def test_stale_inventory_path_error_names_parent() -> None:
    with pytest.raises(bil.BilNotFoundError, match="list the parent"):
        bil.list_files("ace-oat-let")  # inventory says 'ventral midbrain', server has 'ventral_midbrain'
