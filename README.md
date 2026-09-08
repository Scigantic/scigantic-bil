<h1 align="center">scigantic-bil</h1>

<p align="center">
    <a href="https://github.com/Scigantic/scigantic-bil/actions/workflows/ci.yml">
        <img alt="CI" src="https://github.com/Scigantic/scigantic-bil/actions/workflows/ci.yml/badge.svg" /></a>
    <a href="https://pypi.org/project/scigantic-bil/">
        <img alt="PyPI" src="https://img.shields.io/pypi/v/scigantic-bil" /></a>
    <a href="https://pypi.org/project/scigantic-bil/">
        <img alt="PyPI - Python Version" src="https://img.shields.io/pypi/pyversions/scigantic-bil" /></a>
    <a href="https://github.com/Scigantic/scigantic-bil/blob/main/LICENSE">
        <img alt="License" src="https://img.shields.io/github/license/Scigantic/scigantic-bil" /></a>
</p>

Search the [Brain Image Library](https://www.brainimagelibrary.org/) and read its light-sheet, fMOST and STPT volumes over HTTP. No download, no account, no local copy.

```python
import scigantic_bil as bil

cat = bil.BilCatalog.load()             # every BIL dataset, one 5 MB GET
for d in cat.light_sheet()[:5]:         # 808 light-sheet datasets, 278 TB
    print(d.bildid, d.contributor, d.species, f"{d.size_gb:.0f} GB")

img = bil.thumbnail("ace-bin-run")      # middle z-slice of a 27 GB stack, one 16 MB read
```

## Installation

```console
$ pip install scigantic-bil
$ pip install "scigantic-bil[zarr]"     # also open OME-Zarr stores in place
```

## Why this exists

The Brain Image Library is the BRAIN Initiative's repository for whole-brain microscopy: 14,224 datasets and 6 PB as of its 2026-07-31 inventory, including the largest public collection of cleared-tissue light-sheet brains. Everything is served over plain HTTPS with directory listings and range requests, and the metadata API needs no key. That makes it readable in place, but nothing in the ecosystem did so.

BIL's own [`brainimagelibrary`](https://pypi.org/project/brainimagelibrary/) package (py-brain-sdk, 0.0.23, GPL-3.0) wraps the metadata API and downloads datasets, with resumable transfers and citation lookups. Its source (read, not assumed, on 2026-09-08) contains no image reading at all: the only route from a BIL id to pixels is `DatasetInventory.download()`, which fetches the files to disk. For a 27 GB light-sheet stack that is the whole stack, to look at one slice.

This package is the other half. It reads BIL in place:

- **A typed, in-memory catalog of the whole archive** from BIL's daily inventory TSV, with structured filters and a light-sheet finder that unions the inventory's technique field with BIL's fulltext index. The technique field alone says light sheet for 348 datasets; the union finds 808. The rest are tagged `other` and only say light sheet in their abstract or instrument record.
- **Single-slice TIFF reads** straight from the download server. The dominant BIL layout is one TIFF per z-plane, about 16 MB each; one slice is one request.
- **OME-Zarr stores** opened lazily through zarr's HTTP store, with pyramid levels checked against what the server actually has (one BIL store declares eight levels and serves seven).
- **Thumbnails that read as little as possible**: the middle slice of a TIFF stack, or the coarsest level of a zarr pyramid.
- **A seekable HTTP file object** (`HttpFile`) so tifffile fetches only the pages you ask for from a large multi-page TIFF.
- **BIL's per-dataset manifest** (path, size, MD5, URL for every file) as one gzipped GET, so a deep tree lists in one request.

Measured on 2026-09-08 from a residential connection, against the live archive:

| | measured |
|---|---|
| Load the full catalog (14,224 datasets) | 1.0 s cold, 0.3 s from the on-disk copy |
| Light-sheet datasets found | 348 by technique field, 808 with fulltext union |
| Read one 16 MB light-sheet slice (4501 x 3828 uint16) | 0.40 s, 45 MB/s single stream |
| Thumbnail of a 1,923-slice, 27 GB stack | 0.25 s to 0.56 s, one file read |
| Thumbnail of a 50 GB OME-Zarr store (848 x 6300 x 9600) | 0.65 s, coarsest level only |
| Eight parallel streams | 19 MB/s aggregate, slower than one stream, so reads are sequential |
| Manifest for a 1,923-file dataset | 352 KB gzipped, one request |

Dependencies are `requests`, `numpy` and `tifffile`. zarr and pandas are extras.

## Data license

BIL data is distributed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/); some datasets additionally carry the Allen Institute Terms of Use, recorded per dataset in `DatasetDetail.rights`. This package's code is MIT-0. The permissive code license does not extend to the data: anything you derive from BIL images and redistribute needs attribution and the same license. Cite the dataset's DOI (`DatasetDetail.doi`) and its publications (`DatasetDetail.publications`).

## Catalog

```python
cat = bil.BilCatalog.load()                     # newest daily inventory
cat = bil.BilCatalog.load(date="20260731")      # a specific snapshot
len(cat), cat.date                              # (14224, '20260731')

cat.summary()                                   # datasets, files, TB, top modality/technique/species/extensions
cat["ace-cup-eel"]                              # one Dataset by id
cat.filter(technique="fMOST", species="mouse")  # case-insensitive substring match, any combination
cat.filter(extension=".swc")                    # datasets shipping neuron reconstructions
cat.filter(max_size_gb=2)                       # small enough to pull whole
cat.search("iDISCO")                            # BIL's fulltext index, joined to inventory rows
cat.light_sheet()                               # technique field + fulltext, deduplicated
cat.to_dataframe(cat.filter(technique="STPT"))  # pandas, with pip install "scigantic-bil[pandas]"
```

A `Dataset` carries what the inventory indexes: contributor, affiliation, award, project, consortium, modality, technique, species, genotype, file count, size, and a per-extension file histogram (`extensions`). `Dataset.url` is the dataset's root on the download server.

## Metadata

The full record lives on the metadata API and is fetched on demand:

```python
d = bil.retrieve("ace-cup-eel")
d.title, d.abstract, d.rights_identifier        # 'Light-sheet imaged brain ...', ..., 'CC-BY-SA-4.0'
d.microscope_type, d.species                    # 'Light-sheet', 'mouse'
d.instrument                                    # {'microscopetype': 'Light-sheet', 'microscopemanufacturerandmodel': 'Zeiss Z.1', ...}
d.specimen, d.images                            # specimen record; per-image axes, step sizes, channels
d.contributors, d.publications, d.funders
d.is_light_sheet                                # checks technique, instrument and free text together

bil.retrieve_many(["ace-cup-eel", "ace-bin-run"])   # batched POST, unknown ids dropped
bil.fulltext("CLARITY")                             # BIL ids only
bil.query("specimen", species="mouse")              # one structured element=value pair
```

Structured queries match exactly (`query("instrument", microscopetype="Light-sheet")` found 6 datasets on 2026-09-08 where `fulltext("light sheet")` found 777), so use fulltext for discovery and the catalog's filters for structure.

## Files

```python
bil.list_files("ace-bin-run")            # one directory: name, size, modified, url
bil.manifest("ace-bin-run")              # every file, with relative path and MD5, one gzipped GET
list(bil.walk("ace-cup-eel"))            # recursive; a zarr store appears once, as a directory
bil.find("ace-bin-run", suffix=".tif")   # natural sort, so Z00002 follows Z00001
bil.find_zarr("ace-cup-eel")             # ['https://download.brainimagelibrary.org/.../subject_5.zarr/']
bil.download(entry, "out/")              # the one function that writes image bytes to disk
```

Any of these accept a BIL id, a `Dataset`, a `/bil/data/...` path from the metadata, or a download-server URL.

## Images

```python
stack = bil.slices("ace-bin-run")        # 1,923 FileEntry in z order, nothing fetched yet
img = bil.read_tiff(stack[961])          # (4501, 3828) uint16, one request
vol = bil.read_stack("ace-bin-run", start=900, stop=960, step=10)   # (6, 4501, 3828)
bil.thumbnail("ace-bin-run", max_size=512)                          # middle slice, stride-downsampled
bil.thumbnail("ace-bin-run", index=100, channel="ch02")
```

Formats this package does not decode raise `UnsupportedFormatError` naming what does: JPEG 2000 (`.jp2`, 5,787 datasets, mostly STPT and fMOST sections; use `download()` then glymur), Imaris (`.ims`, h5py), NIfTI. Reading `.jp2` in place is the obvious next addition; the format has resolution levels built in, so a thumbnail should not need the whole file.

### OME-Zarr

```python
g = bil.open_zarr("ace-cup-eel")         # lazy; nothing read until sliced
bil.zarr_levels(g)                       # ['0', ..., '6'], as served, not as declared
g["6"][0, 0, 400]                        # one plane of the coarsest level, a few chunks
bil.zarr_thumbnail(g, max_size=512)
```

A zarr store is a directory, so it never shows in the inventory's extension histogram; `find_zarr()` is how to know a dataset ships one. Requires `pip install "scigantic-bil[zarr]"` (zarr 3, fsspec, aiohttp) and Python 3.11 or newer, which is zarr 3's own floor; on 3.10 the extra installs nothing and `open_zarr()` raises a clear ImportError.

## Caching

On by default. Metadata responses, directory listings and manifests are cached to `~/.cache/scigantic-bil` (macOS: `~/Library/Caches/scigantic-bil`; override with `SCIGANTIC_BIL_CACHE` or `enable_cache(cache_dir=...)`) and expire after 7 days, since BIL republishes its inventory every few days. Inventory snapshots are immutable once published and are kept as plain TSV files without expiry. Image bytes are never cached.

```python
bil.disable_cache()
bil.enable_cache(ttl_days=1)
bil.clear_cache()
```

## Command line

```console
$ scigantic-bil summary
$ scigantic-bil light-sheet --limit 20
$ scigantic-bil search "iDISCO" --json
$ scigantic-bil filter --technique fMOST --species mouse --max-gb 100
$ scigantic-bil info ace-cup-eel
$ scigantic-bil files ace-bin-run --zarr
$ scigantic-bil thumbnail ace-bin-run slice.png --size 512
```

## Testing

Every test runs live against BIL, no mocks, the same philosophy as the rest of the scigantic-* packages. The suite takes about 15 seconds. CI runs Python 3.10 through 3.14 plus `mypy --strict`.

## License

MIT-0 for the code. See [Data license](#data-license) for the data.
