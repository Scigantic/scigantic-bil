"""Live tests: every test talks to the real Brain Image Library, matching the
rest of the scigantic-* family. A private cache directory per session keeps
runs independent of anything already on the developer's machine."""

from __future__ import annotations

import pytest

import scigantic_bil as bil


@pytest.fixture(scope="session", autouse=True)
def _isolated_cache(tmp_path_factory: pytest.TempPathFactory) -> None:
    bil.enable_cache(cache_dir=str(tmp_path_factory.mktemp("bil-cache")))


@pytest.fixture(scope="session")
def catalog() -> bil.BilCatalog:
    return bil.BilCatalog.load()


# Two datasets with known, stable shapes, verified live 2026-09-08.
TIFF_STACK = "ace-bin-run"  # Kim lab LSFM, 1,923 single-slice TIFFs, one folder
ZARR_STORE = "ace-cup-eel"  # UC Irvine iDISCO light sheet, one OME-Zarr v2 store, 7 levels
