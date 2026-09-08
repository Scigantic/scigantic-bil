# Changelog

## 0.1.0

First release.

- `BilCatalog`: the whole Brain Image Library in memory from BIL's daily
  inventory TSV (14,224 datasets on 2026-07-31), with case-insensitive
  structured filters, fulltext search joined to inventory rows, a
  light-sheet finder that unions the technique field with BIL's fulltext
  index (348 vs 808 datasets), a summary, and an optional pandas view.
- `retrieve()`, `retrieve_many()`, `query()`, `fulltext()`: typed wrappers
  over the metadata API, including the batched POST form of `/retrieve`.
- `list_files()`, `manifest()`, `walk()`, `find()`, `find_zarr()`,
  `download()`: lazy listing over the download server's nginx autoindex
  and BIL's per-dataset gzipped manifests. `walk()` never enters a zarr
  or N5 store (crawling one chunk directory at a time took 218 s on a
  real store before that guard existed).
- `read_tiff()`, `read_stack()`, `slices()`, `thumbnail()`, `downsample()`:
  single-slice TIFF reads over HTTP, sequential on purpose (eight
  parallel streams measured slower than one against BIL's server).
- `HttpFile`: a seekable read-only file over HTTP range requests with a
  block cache, so tifffile fetches only the pages requested.
- `open_zarr()`, `zarr_levels()`, `zarr_thumbnail()`: OME-Zarr stores
  opened lazily; pyramid levels are verified against the server listing
  because at least one BIL store declares a level it does not serve.
- Response cache on by default, 7-day expiry, image bytes never cached.
- CLI: `summary`, `search`, `light-sheet`, `filter`, `info`, `files`,
  `thumbnail`.
