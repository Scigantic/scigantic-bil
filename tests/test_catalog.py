from __future__ import annotations

import scigantic_bil as bil


def test_available_dates_are_sorted_yyyymmdd() -> None:
    dates = bil.available_inventory_dates()
    assert dates == sorted(dates)
    assert all(len(d) == 8 and d.isdigit() for d in dates)
    assert dates[-1] >= "20260731"


def test_catalog_loads_whole_archive(catalog: bil.BilCatalog) -> None:
    # 14,224 datasets on 2026-07-31; the archive only grows.
    assert len(catalog) >= 14_000
    assert catalog.date == bil.available_inventory_dates()[-1]
    assert "ace-cup-eel" in catalog
    d = catalog["ace-cup-eel"]
    assert d.url.startswith("https://download.brainimagelibrary.org/42/e4/42e4274e0579397f/")
    assert d.size_bytes and d.size_bytes > 50_000_000_000
    assert d.number_of_files == 15323
    assert d.extensions == {"": 15323}  # zarr chunks are extensionless


def test_second_load_is_served_from_disk(catalog: bil.BilCatalog) -> None:
    path = bil.cache_dir() / f"inventory-{catalog.date}.tsv"
    assert path.exists() and path.stat().st_size > 4_000_000
    again = bil.BilCatalog.load()
    assert len(again) == len(catalog)


def test_filter_is_case_insensitive_and_composes(catalog: bil.BilCatalog) -> None:
    mouse = catalog.filter(species="mouse")
    assert len(mouse) >= 11_000  # 'Mouse' and 'mouse' both appear in the inventory
    assert len(catalog.filter(species="MOUSE")) == len(mouse)
    fmost = catalog.filter(technique="fmost", species="mouse", extension=".tif")
    assert fmost and all("fmost" in d.technique.lower() for d in fmost)
    assert all(".tif" in {k.lower() for k in d.extensions} for d in fmost)
    small = catalog.filter(max_size_gb=1.0)
    assert small and all(d.size_gb is not None and d.size_gb <= 1.0 for d in small)


def test_light_sheet_unions_technique_and_fulltext(catalog: bil.BilCatalog) -> None:
    by_technique = catalog.filter(light_sheet=True)
    union = catalog.light_sheet()
    assert len(by_technique) >= 340
    # 348 by technique vs 808 in the union on 2026-09-08: the technique
    # field alone misses more than half.
    assert len(union) >= len(by_technique) * 2
    ids = {d.bildid for d in union}
    assert {d.bildid for d in by_technique} <= ids
    assert "ace-cup-eel" in ids  # technique 'other', light sheet only in the abstract
    assert "ace-bin-run" in ids  # technique 'light sheet microscopy'


def test_search_joins_fulltext_hits_to_inventory_rows(catalog: bil.BilCatalog) -> None:
    hits = catalog.search("iDISCO")
    assert hits and all(isinstance(h, bil.Dataset) for h in hits)
    assert any(h.bildid == "ace-cup-eel" for h in hits)


def test_summary_shape(catalog: bil.BilCatalog) -> None:
    s = catalog.summary()
    assert s["datasets"] == len(catalog)
    assert s["terabytes"] > 5_000
    assert "fMOST" in s["technique"] and "STPT" in s["technique"]
    assert ".tif" in s["extensions"] and ".jp2" in s["extensions"]


def test_to_dataframe(catalog: bil.BilCatalog) -> None:
    import pandas as pd

    df = catalog.to_dataframe(catalog.filter(technique="STPT")[:20])
    assert isinstance(df, pd.DataFrame) and len(df) == 20
    assert {"bildid", "size_gb", "url", "technique"} <= set(df.columns)


def test_from_tsv_tolerates_blank_and_na_cells() -> None:
    text = (
        "metadata_version\tbildid\tbildate\tcontributor\taffiliation\taward_number\tproject\tconsortium\t"
        "bildirectory\tgeneralmodality\ttechnique\tspecies\ttaxonomy\tgenotype\tsamplelocalid\t"
        "number_of_files\tsize\tfile_types\tfrequencies\tmime_types\n"
        "2.0\tabc-def-ghi\t2024-01-01\tX\tY\t\t\t\t/bil/data/ab/cd/abcdef/sub\tother\tother\tmouse\t\tNA\t\t"
        "NA\t\t{'images': 3}\tnot a dict\t\n"
    )
    cat = bil.BilCatalog.from_tsv(text, date="20240101")
    d = cat["abc-def-ghi"]
    assert d.number_of_files is None and d.size_bytes is None and d.size_gb is None
    assert d.file_types == {"images": 3} and d.extensions == {}
    assert d.url == "https://download.brainimagelibrary.org/ab/cd/abcdef/sub/"


def test_concurrent_cold_loads_do_not_race(tmp_path: object) -> None:
    import concurrent.futures as cf
    from pathlib import Path

    bil.enable_cache(cache_dir=str(Path(str(tmp_path)) / "race"))
    try:
        with cf.ThreadPoolExecutor(6) as ex:
            sizes = list(ex.map(lambda _: len(bil.BilCatalog.load()), range(6)))
        assert len(set(sizes)) == 1
        assert not list((Path(str(tmp_path)) / "race").glob("*.part"))
    finally:
        bil.enable_cache(cache_dir=str(Path(str(tmp_path)).parent / "bil-cache0"))
