"""In-memory catalog of every BIL dataset, built from BIL's own daily
inventory rather than from 14,000 API calls.

BIL publishes ``https://download.brainimagelibrary.org/inventory/daily/
<YYYYMMDD>.tsv`` every few days: one row per dataset with contributor,
project, consortium, modality, technique, species, file count, size and a
per-extension file histogram. Measured 2026-09-08: 14,224 rows, 4.9 MB,
one GET. That is the whole archive, and it is what BilCatalog loads. The
full per-dataset record (abstract, instrument, rights, publications) stays
on the metadata API and is fetched on demand via api.retrieve().

The inventory's ``technique`` field is the only structured light-sheet
signal and it undercounts badly (348 datasets say light sheet; BIL's own
fulltext search finds 777, the rest tagged ``other``), so light_sheet()
unions both sources rather than trusting either alone.
"""

from __future__ import annotations

import csv
import io
import os
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Iterator

from . import api, cache
from ._client import DOWNLOAD_BASE, BilError, send
from .models import Dataset

if TYPE_CHECKING:
    import pandas as pd

_INVENTORY_DIR = f"{DOWNLOAD_BASE}/inventory/daily/"
_DAILY_RE = re.compile(r'href="((\d{8})\.tsv)"')

_LIGHT_SHEET_TERMS = ("light sheet", "lightsheet", "LSFM")


def available_inventory_dates() -> list[str]:
    """Dates (``YYYYMMDD``) of every daily inventory BIL currently serves,
    oldest first. Read live from the directory listing."""
    resp = send("GET", _INVENTORY_DIR, timeout=120.0)
    dates = sorted({m.group(2) for m in _DAILY_RE.finditer(resp.text)})
    if not dates:
        raise BilError(f"no daily inventory files found at {_INVENTORY_DIR}")
    return dates


def _inventory_text(date: str, refresh: bool = False) -> str:
    """The TSV for one date, kept as a plain file in the cache directory
    (``inventory-<date>.tsv``) rather than the JSON response cache: it is
    5 MB and immutable once published, so it never expires."""
    path = cache.cache_dir() / f"inventory-{date}.tsv"
    if path.exists() and not refresh and cache.is_cache_enabled():
        return path.read_text(encoding="utf-8")
    resp = send("GET", f"{_INVENTORY_DIR}{date}.tsv", timeout=300.0)
    text = resp.content.decode("utf-8")
    if cache.is_cache_enabled():
        # Unique per writer: two threads loading the catalog cold at the same
        # time (a notebook with a thread pool, a test session) must not share
        # a temp path, or the second os.replace() fails with FileNotFoundError
        # once the first has moved it. Measured: 2 of 6 concurrent cold loads
        # crashed before this. Both write an identical immutable snapshot, so
        # last-writer-wins is fine.
        tmp = path.with_suffix(f".tsv.{uuid.uuid4().hex}.part")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    return text


@dataclass
class BilCatalog:
    """Every dataset in the Brain Image Library, as of one inventory date.

    ``BilCatalog.load()`` fetches the latest daily inventory (cached on
    disk after the first call). Filters return plain lists of Dataset so
    they compose with ordinary Python; ``to_dataframe()`` hands the same
    rows to pandas when that is installed.
    """

    date: str
    datasets: list[Dataset]
    _by_id: dict[str, Dataset] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._by_id = {d.bildid: d for d in self.datasets}

    @classmethod
    def load(cls, date: str | None = None, refresh: bool = False) -> BilCatalog:
        """Load the inventory for ``date`` (``YYYYMMDD``), or the newest one
        BIL serves. ``refresh=True`` re-downloads even if cached."""
        if date is None:
            date = available_inventory_dates()[-1]
        text = _inventory_text(date, refresh=refresh)
        return cls.from_tsv(text, date=date)

    @classmethod
    def from_tsv(cls, text: str, date: str = "") -> BilCatalog:
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        rows = [Dataset.from_row(r) for r in reader]
        rows = [r for r in rows if r.bildid]
        return cls(date=date, datasets=rows)

    def __len__(self) -> int:
        return len(self.datasets)

    def __iter__(self) -> Iterator[Dataset]:
        return iter(self.datasets)

    def __contains__(self, bildid: object) -> bool:
        return bildid in self._by_id

    def get(self, bildid: str) -> Dataset | None:
        return self._by_id.get(bildid)

    def __getitem__(self, bildid: str) -> Dataset:
        try:
            return self._by_id[bildid]
        except KeyError:
            raise KeyError(f"{bildid!r} is not in the {self.date} inventory") from None

    def subset(self, bildids: Iterable[str]) -> list[Dataset]:
        """Datasets for the given ids, in the given order, skipping unknown
        ids (an API query can return ids newer than the inventory)."""
        return [self._by_id[b] for b in bildids if b in self._by_id]

    def filter(
        self,
        *,
        species: str | None = None,
        technique: str | None = None,
        modality: str | None = None,
        contributor: str | None = None,
        affiliation: str | None = None,
        project: str | None = None,
        consortium: str | None = None,
        award_number: str | None = None,
        extension: str | None = None,
        min_size_gb: float | None = None,
        max_size_gb: float | None = None,
        light_sheet: bool | None = None,
    ) -> list[Dataset]:
        """Case-insensitive substring match on any combination of inventory
        fields. ``extension`` matches datasets containing at least one file
        with that extension (``".tif"``, ``".swc"``, ``".jp2"``).
        ``light_sheet=True`` uses only the inventory's technique field; use
        BilCatalog.light_sheet() for the fuller union."""

        def has(value: str, needle: str | None) -> bool:
            return needle is None or needle.lower() in value.lower()

        ext = extension.lower() if extension else None
        out: list[Dataset] = []
        for d in self.datasets:
            if not (
                has(d.species, species)
                and has(d.technique, technique)
                and has(d.generalmodality, modality)
                and has(d.contributor, contributor)
                and has(d.affiliation, affiliation)
                and has(d.project, project)
                and has(d.consortium, consortium)
                and has(d.award_number, award_number)
            ):
                continue
            if ext is not None and not any(k.lower() == ext for k in d.extensions):
                continue
            if min_size_gb is not None and (d.size_gb is None or d.size_gb < min_size_gb):
                continue
            if max_size_gb is not None and (d.size_gb is None or d.size_gb > max_size_gb):
                continue
            if light_sheet is not None and d.is_light_sheet != light_sheet:
                continue
            out.append(d)
        return out

    def search(self, text: str) -> list[Dataset]:
        """Datasets whose full metadata mentions ``text``, via BIL's own
        fulltext index (one API call), joined back to inventory rows."""
        return self.subset(api.fulltext(text))

    def light_sheet(self) -> list[Dataset]:
        """Every dataset BIL knows to be light-sheet: the inventory's
        technique field OR a fulltext hit on "light sheet" / "lightsheet" /
        "LSFM". Measured 2026-09-08: 348 by technique alone, 778 combined."""
        ids: dict[str, None] = {}
        for d in self.filter(light_sheet=True):
            ids[d.bildid] = None
        for term in _LIGHT_SHEET_TERMS:
            for b in api.fulltext(term):
                ids[b] = None
        return self.subset(ids)

    def summary(self) -> dict[str, Any]:
        """Counts and totals for a quick orientation: datasets, files, TB,
        and the top values of modality, technique, species, consortium."""
        sizes = [d.size_bytes for d in self.datasets if d.size_bytes]
        files = [d.number_of_files for d in self.datasets if d.number_of_files]

        def top(values: Iterable[str], n: int = 10) -> dict[str, int]:
            c = Counter(v for v in values if v)
            return dict(c.most_common(n))

        return {
            "inventory_date": self.date,
            "datasets": len(self.datasets),
            "files": sum(files),
            "terabytes": round(sum(sizes) / 1e12, 1),
            "modality": top(d.generalmodality for d in self.datasets),
            "technique": top(d.technique for d in self.datasets),
            "species": top(d.species.lower() for d in self.datasets),
            "consortium": top(d.consortium for d in self.datasets),
            "extensions": top(
                (ext for d in self.datasets for ext in d.extensions), n=15
            ),
        }

    def to_dataframe(self, datasets: Iterable[Dataset] | None = None) -> "pd.DataFrame":
        """The inventory (or a filtered subset) as a pandas DataFrame, one
        row per dataset. Requires ``pip install scigantic-bil[pandas]``."""
        try:
            import pandas as pd
        except ImportError as exc:
            raise ImportError("pandas is not installed; pip install 'scigantic-bil[pandas]'") from exc
        rows = list(datasets) if datasets is not None else self.datasets
        records = []
        for d in rows:
            rec: dict[str, Any] = {
                "bildid": d.bildid,
                "bildate": d.bildate,
                "contributor": d.contributor,
                "affiliation": d.affiliation,
                "project": d.project,
                "consortium": d.consortium,
                "generalmodality": d.generalmodality,
                "technique": d.technique,
                "species": d.species,
                "taxonomy": d.taxonomy,
                "genotype": d.genotype,
                "number_of_files": d.number_of_files,
                "size_gb": d.size_gb,
                "extensions": d.extensions,
                "url": d.url,
            }
            records.append(rec)
        return pd.DataFrame.from_records(records)
