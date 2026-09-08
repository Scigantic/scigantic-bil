# Changelog

## 0.2.0

Everything here came out of running the package against the whole archive
(random samples across techniques, the largest trees and files, odd paths,
sixteen threads on a cold cache) on 2026-09-08.

Fixed:

- Paths with `#`, spaces or non-ASCII characters are percent-encoded
  (`encode_path`, `encode_url`, idempotent). An unencoded `#` was a URL
  fragment, so 39 inventory datasets could not be listed or read.
- Directory-listing names are decoded (`Virus_tracing-B1-#6`), URLs keep
  the server's encoding.
- Two-word and one-word dataset ids (`act-nod`, 861 of them) were rejected
  by the id pattern.
- Six concurrent cold catalog loads crashed on a shared temp file name;
  each writer now uses its own.
- `walk()`/`thumbnail()` sat for over twenty minutes on a 1.6 million file
  fMOST tree. `find_zarr()` now has a directory budget, `thumbnail()`
  uses a bounded first-folder search (`first_images`), and TeraFly trees
  are recognised and previewed from their coarsest resolution folder.
- `manifest()` refuses manifests over 64 MB gzipped (a 4.7 million file
  dataset's is 710 MB) with `ManifestTooLargeError`; `walk()` and
  `find_zarr()` fall back to crawling. `manifest_size()` is the cheap
  check.
- A strip-sampled preview came out one pixel wide (decoded strip shape was
  misread).
- A dataset renamed on the server since the inventory was built, and a
  dataset still under the landing zone (`/bil/lz/`), each get a clear
  `BilNotFoundError` instead of a bare 404.
- `tiff_info()` no longer fails on files where tifffile cannot build a
  series ("incompatible keyframe"); pages are reported anyway.

Added:

- `preview_plane()`: bounded preview of a TIFF of any size (coarsest
  pyramid level, or every k-th row by offset for uncompressed planes, or
  sampled strips, or a centre region of a tiled page). A 10 GB single-strip
  plane previews from 63 MB of reads in 11 s.
- `read_region()`: a rectangle of a tiled page through tifffile's zarr
  store, fetching only the covering tiles. `tiff_info()`: shape, pages,
  tiling and pyramid levels from the IFDs alone.
- `terafly_levels()`, `terafly_thumbnail()`, `first_images()`,
  `extensions_under()`, `manifest_size()`.
- `HttpFile` uses 256 KB blocks for metadata walks and one direct range
  request for any read of a block or more. Opening a 21.7 GB, 92-page
  BigTIFF went from 81 MB in 78 requests to 25 MB; reading a 1 GB page
  went from 108 s at 10 MB/s to 27 s at 38 MB/s.
- Latency-bound sampling reads (rows 20 MB apart, TeraFly blocks) run
  through a small thread pool; bulk transfers stay sequential.
- `imagecodecs` and `pillow` are now dependencies: fMOST blocks are LZW,
  and ImageJ Deflate stacks that libdeflate rejects with
  INSUFFICIENT_SPACE decode with Pillow.
- Unsupported-format errors name the tool for the extension found (glymur
  for `.jp2`, navis for `.swc`, nibabel, h5py, numpy for `.dax`).

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
