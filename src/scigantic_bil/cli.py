"""Command-line interface: ``scigantic-bil summary | search | light-sheet |
filter | info | files | thumbnail``. Every command has a Python twin."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from typing import Sequence

from .api import retrieve
from .catalog import BilCatalog
from .files import find_zarr, list_files, walk
from .images import thumbnail
from .models import Dataset


def _print_datasets(rows: list[Dataset], limit: int, as_json: bool) -> None:
    rows = rows[:limit] if limit > 0 else rows
    if as_json:
        print(json.dumps([dataclasses.asdict(d) for d in rows], indent=2))
        return
    for d in rows:
        size = f"{d.size_gb:8.1f} GB" if d.size_gb is not None else "     ? GB"
        n = f"{d.number_of_files:>7}" if d.number_of_files is not None else "      ?"
        print(f"{d.bildid}  {size}  {n} files  {d.species:<10.10}  {d.technique:<28.28}  {d.contributor}")


def _cmd_summary(args: argparse.Namespace) -> int:
    cat = BilCatalog.load(date=args.date)
    print(json.dumps(cat.summary(), indent=2))
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    cat = BilCatalog.load(date=args.date)
    rows = cat.search(args.text)
    print(f"{len(rows)} datasets mention {args.text!r}", file=sys.stderr)
    _print_datasets(rows, args.limit, args.json)
    return 0


def _cmd_light_sheet(args: argparse.Namespace) -> int:
    cat = BilCatalog.load(date=args.date)
    rows = cat.light_sheet()
    total = sum(d.size_bytes or 0 for d in rows) / 1e12
    print(f"{len(rows)} light-sheet datasets, {total:.1f} TB", file=sys.stderr)
    _print_datasets(rows, args.limit, args.json)
    return 0


def _cmd_filter(args: argparse.Namespace) -> int:
    cat = BilCatalog.load(date=args.date)
    rows = cat.filter(
        species=args.species,
        technique=args.technique,
        modality=args.modality,
        contributor=args.contributor,
        project=args.project,
        consortium=args.consortium,
        extension=args.extension,
        min_size_gb=args.min_gb,
        max_size_gb=args.max_gb,
    )
    print(f"{len(rows)} datasets match", file=sys.stderr)
    _print_datasets(rows, args.limit, args.json)
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    d = retrieve(args.bildid)
    if args.json:
        print(json.dumps(d.raw, indent=2))
        return 0
    print(f"{d.bildid}  {d.title}")
    print(f"  url:        {d.url}")
    print(f"  modality:   {d.generalmodality}   technique: {d.technique}" + (f" ({d.technique_other})" if d.technique_other else ""))
    print(f"  microscope: {d.microscope_type or '?'}   species: {d.species or '?'}")
    size = f"{d.dataset_size_gb:.1f} GB" if d.dataset_size_gb is not None else "?"
    print(f"  size:       {size}   files: {d.number_of_files if d.number_of_files is not None else '?'}")
    print(f"  rights:     {d.rights_identifier or d.rights or '?'}")
    if d.doi:
        print(f"  doi:        {d.doi}")
    for c in d.contributors[:5]:
        print(f"  contributor: {c.name} ({c.contributor_type}) {c.affiliation}")
    for p in d.publications[:3]:
        print(f"  publication: {p.doi or p.citation[:100]}")
    if d.abstract:
        print(f"  abstract:   {d.abstract[:600]}")
    return 0


def _cmd_files(args: argparse.Namespace) -> int:
    entries = list(walk(args.target)) if args.recursive else list_files(args.target)
    if args.json:
        print(json.dumps([dataclasses.asdict(e) for e in entries], indent=2))
        return 0
    for e in entries:
        size = "-" if e.size is None else str(e.size)
        print(f"{size:>14}  {e.modified}  {e.name}{'/' if e.is_dir else ''}")
    stores = find_zarr(args.target) if args.zarr else []
    for s in stores:
        print(f"zarr store: {s}")
    return 0


def _cmd_thumbnail(args: argparse.Namespace) -> int:
    import numpy as np

    img = thumbnail(args.target, max_size=args.size, index=args.index, channel=args.channel)
    out = args.output
    if out.lower().endswith(".npy"):
        np.save(out, img)
    else:
        try:
            from PIL import Image
        except ImportError:
            print("Pillow is not installed; write a .npy instead or pip install pillow", file=sys.stderr)
            return 1
        a = np.asarray(img, dtype=np.float64)
        lo, hi = np.percentile(a, (1, 99.5))
        a = np.clip((a - lo) / max(hi - lo, 1e-9), 0, 1) * 255
        Image.fromarray(a.astype(np.uint8)).save(out)
    print(f"wrote {out} shape={tuple(img.shape)} dtype={img.dtype}", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scigantic-bil", description="Brain Image Library from the command line")
    p.add_argument("--date", default=None, help="inventory date YYYYMMDD (default: newest)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("summary", help="counts and totals for the whole archive")
    s.set_defaults(func=_cmd_summary)

    s = sub.add_parser("search", help="fulltext search across all metadata")
    s.add_argument("text")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_search)

    s = sub.add_parser("light-sheet", help="every light-sheet dataset (technique field + fulltext)")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_light_sheet)

    s = sub.add_parser("filter", help="structured filter over the inventory")
    for name in ("species", "technique", "modality", "contributor", "project", "consortium", "extension"):
        s.add_argument(f"--{name}")
    s.add_argument("--min-gb", type=float, default=None)
    s.add_argument("--max-gb", type=float, default=None)
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_filter)

    s = sub.add_parser("info", help="full metadata record for one dataset")
    s.add_argument("bildid")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_info)

    s = sub.add_parser("files", help="list a dataset's files on the download server")
    s.add_argument("target", help="BIL id, /bil/data path, or https URL")
    s.add_argument("--recursive", "-r", action="store_true")
    s.add_argument("--zarr", action="store_true", help="also report *.zarr stores")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_files)

    s = sub.add_parser("thumbnail", help="preview one slice (middle by default) to a PNG or .npy")
    s.add_argument("target", help="BIL id, /bil/data path, https URL, or a file URL")
    s.add_argument("output")
    s.add_argument("--size", type=int, default=512)
    s.add_argument("--index", type=int, default=None)
    s.add_argument("--channel", default=None)
    s.set_defaults(func=_cmd_thumbnail)

    args = p.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
