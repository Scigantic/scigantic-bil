from __future__ import annotations

import pytest

import scigantic_bil as bil
from tests.conftest import TIFF_STACK, ZARR_STORE

# Two datasets BIL lists for BrAinPI, verified live 2026-10-02.
OMEHANS = "ace-bet-fox"  # one .omehans under /bil/assets, 3 channels, 12000 x 16000 x 140
IMARIS_ONLY = "ace-can-elk"  # nine Imaris .ims files under /bil/data, no TIFF or zarr anywhere


def test_disk_path_forms() -> None:
    assert bil.disk_path("/bil/assets/ace/bet/fox/asset/brainpi/x.omehans") == "/bil/assets/ace/bet/fox/asset/brainpi/x.omehans"
    assert bil.disk_path("/bil/data/ab/cd/abcdef/sub/") == "/bil/data/ab/cd/abcdef/sub"
    url = "https://download.brainimagelibrary.org/ca/27/ca273783c1dba805/2019Q1_U01Zhang/Virus_tracing-B1-%236/"
    assert bil.disk_path(url) == "/bil/data/ca/27/ca273783c1dba805/2019Q1_U01Zhang/Virus_tracing-B1-#6"
    entry = bil.FileEntry("a.ims", "https://download.brainimagelibrary.org/ab/cd/abcdef/a.ims", 1, "", False)
    assert bil.disk_path(entry) == "/bil/data/ab/cd/abcdef/a.ims"
    with pytest.raises(ValueError):
        bil.disk_path("https://example.org/x.ims")


def test_detail_carries_brainpi_fields() -> None:
    d = bil.retrieve(OMEHANS)
    assert d.has_brainpi
    assert d.brainpiroot.startswith("/bil/assets/ace/bet/fox/") and d.brainpidata == ("0539046893.omehans",)
    assert bil.brainpi_paths(d) == ["/bil/assets/ace/bet/fox/asset/brainpi/0539046893.omehans"]
    plain = bil.retrieve(TIFF_STACK)
    assert not plain.has_brainpi and bil.brainpi_paths(plain) == [] and bil.brainpi_views(plain) == []


def test_links_come_from_the_service_not_a_prefix() -> None:
    v = bil.brainpi_links("/bil/assets/ace/bet/fox/asset/brainpi/0539046893.omehans")
    assert v.available and v.omezarr and v.neuroglancer and v.neuroglancer_info
    assert v.omezarr.startswith(bil.BRAINPI_BASE + "/omezarr/") and v.omezarr.endswith(".omehans.ome.zarr")
    assert v.neuroglancer_info == v.neuroglancer + "/info"
    # /bil/data/... is served under a bil_data/ prefix, which is why URLs are
    # never assembled client-side.
    views = bil.brainpi_views(IMARIS_ONLY, limit=1)
    assert len(views) == 1 and views[0].omezarr and "/omezarr/bil_data/" in views[0].omezarr
    # BIL lists nothing for this stack, yet BrAinPI serves one slice (as a
    # 2-D pyramid of that slice), not the directory of slices, and not a
    # missing file. brainpidata is BIL's curation, not the service's limit.
    tif = bil.slices(TIFF_STACK)[0]
    assert bil.brainpi_links(tif).available
    assert not bil.brainpi_links(tif.url.rsplit("/", 1)[0] + "/").available
    assert not bil.brainpi_links(tif.url.replace("Z00001", "Z99999")).available
    # An existing OME-Zarr store BIL does not list is served too.
    assert bil.brainpi_links(bil.find_zarr(ZARR_STORE)[0]).available


def test_omezarr_view_opens_with_zarr_and_levels_survive_no_listing() -> None:
    pytest.importorskip("zarr")
    url = bil.first_omezarr(OMEHANS)
    assert url
    g = bil.open_zarr(url)
    levels = bil.zarr_levels(g)  # the view has no directory listing; levels come from .zattrs
    assert len(levels) >= 10 and levels[0] == "0"
    assert tuple(g[levels[0]].shape) == (1, 3, 140, 12000, 16000)
    th = bil.zarr_thumbnail(g, max_size=128)
    assert th.ndim == 2 and max(th.shape) <= 128 and th.max() > 0


def test_imaris_only_dataset_previews_through_brainpi(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("zarr")
    d = bil.retrieve(IMARIS_ONLY)
    assert d.has_brainpi and bil.find_zarr(IMARIS_ONLY) == []
    g = bil.open_zarr(d)  # no store on the download server: falls through to the BrAinPI view
    assert bil.zarr_levels(g)
    th = bil.thumbnail(IMARIS_ONLY, max_size=128)
    assert th.ndim == 2 and max(th.shape) <= 128 and th.max() > 0


def test_dataset_without_views_still_names_the_tool() -> None:
    with pytest.raises(bil.UnsupportedFormatError, match="navis"):
        bil.thumbnail("ace-nap-out")  # .swc only, no BrAinPI entry


def test_cli_views(capsys: pytest.CaptureFixture[str]) -> None:
    from scigantic_bil.cli import main

    assert main(["views", OMEHANS]) == 0
    out = capsys.readouterr().out
    assert "neuroglancer" in out and ".omehans.ome.zarr" in out
    assert main(["views", TIFF_STACK]) == 0
    assert "lists no BrAinPI files" in capsys.readouterr().err
