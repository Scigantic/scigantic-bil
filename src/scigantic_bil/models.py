"""Typed records for the two shapes BIL exposes: one inventory row per
dataset (the daily TSV, see catalog.py) and one full metadata record per
dataset (the /retrieve endpoint, see api.py), plus a file entry from the
download server's directory listings (files.py)."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from ._client import DOWNLOAD_BASE

_BIL_DATA_PREFIX = "/bil/data/"


def encode_path(path: str) -> str:
    """Percent-encode a URL path the way BIL's nginx serves it, idempotently
    (decode first, then encode, so an already-encoded href is not encoded
    twice). Needed because 39 inventory paths carry spaces, ``#`` or
    non-ASCII characters (2026-07-31 inventory); an unencoded ``#`` is a
    URL fragment, so ``.../Virus_tracing-B1-#6/`` silently requested
    ``.../Virus_tracing-B1-`` and 404'd. The server itself lists that
    directory as ``Virus_tracing-B1-%236/``."""
    return quote(unquote(path), safe="/")


def encode_url(url: str) -> str:
    """encode_path() applied to the path of a full URL, host untouched."""
    parts = urlsplit(url)
    if not parts.scheme:
        return encode_path(url)
    # A raw '#' in the path would have been parsed as a fragment; put it back.
    path = parts.path + (("#" + parts.fragment) if parts.fragment else "")
    return urlunsplit((parts.scheme, parts.netloc, encode_path(path), parts.query, ""))


def dataset_url(bildirectory: str) -> str:
    """Map a BIL filesystem path (``/bil/data/42/e4/<uuid>/subject_5``) to
    its public HTTPS location on the download server. Verified 2026-09-08
    against BIL's documented pattern
    ``https://download.brainimagelibrary.org/<c1c2>/<c3c4>/<uuid>/...``.
    Always ends in a slash; the path is percent-encoded (see encode_path)."""
    path = bildirectory.strip()
    if path.startswith(_BIL_DATA_PREFIX):
        path = path[len(_BIL_DATA_PREFIX) :]
    elif path.startswith(DOWNLOAD_BASE):
        path = path[len(DOWNLOAD_BASE) :]
    path = encode_path(path.strip("/"))
    return f"{DOWNLOAD_BASE}/{path}/" if path else f"{DOWNLOAD_BASE}/"


def _int_or_none(value: str) -> int | None:
    value = value.strip()
    if not value or value.upper() in {"NA", "NONE", "NAN"}:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _dict_or_empty(value: str) -> dict[str, int]:
    """The inventory's ``file_types``/``frequencies`` columns are Python
    dict reprs (single quotes), not JSON, so they need literal_eval."""
    value = value.strip()
    if not value:
        return {}
    try:
        parsed = ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in parsed.items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


@dataclass(frozen=True)
class Dataset:
    """One row of BIL's daily inventory: the fields BIL itself indexes for
    every dataset. Text fields are as-submitted (species may be ``mouse``
    or ``Mouse``); filter case-insensitively, as BilCatalog.filter() does.
    ``extensions`` maps file extension (``.tif``, ``.jp2``, ``.swc``, or
    ``""`` for extensionless files such as zarr chunks) to file count."""

    bildid: str
    bildate: str
    contributor: str
    affiliation: str
    award_number: str
    project: str
    consortium: str
    bildirectory: str
    generalmodality: str
    technique: str
    species: str
    taxonomy: str
    genotype: str
    samplelocalid: str
    number_of_files: int | None
    size_bytes: int | None
    file_types: dict[str, int] = field(default_factory=dict)
    extensions: dict[str, int] = field(default_factory=dict)
    metadata_version: str = ""

    @classmethod
    def from_row(cls, row: dict[str, str]) -> Dataset:
        return cls(
            bildid=row.get("bildid", "").strip(),
            bildate=row.get("bildate", "").strip(),
            contributor=row.get("contributor", "").strip(),
            affiliation=row.get("affiliation", "").strip(),
            award_number=row.get("award_number", "").strip(),
            project=row.get("project", "").strip(),
            consortium=row.get("consortium", "").strip(),
            bildirectory=row.get("bildirectory", "").strip(),
            generalmodality=row.get("generalmodality", "").strip(),
            technique=row.get("technique", "").strip(),
            species=row.get("species", "").strip(),
            taxonomy=row.get("taxonomy", "").strip(),
            genotype=row.get("genotype", "").strip(),
            samplelocalid=row.get("samplelocalid", "").strip(),
            number_of_files=_int_or_none(row.get("number_of_files", "")),
            size_bytes=_int_or_none(row.get("size", "")),
            file_types=_dict_or_empty(row.get("file_types", "")),
            extensions=_dict_or_empty(row.get("frequencies", "")),
            metadata_version=row.get("metadata_version", "").strip(),
        )

    @property
    def url(self) -> str:
        """Public HTTPS root of this dataset on the download server."""
        return dataset_url(self.bildirectory)

    @property
    def size_gb(self) -> float | None:
        return None if self.size_bytes is None else self.size_bytes / 1e9

    @property
    def image_extensions(self) -> dict[str, int]:
        """Only the extensions this package can read (see images.py) or
        recognises as image data, with counts."""
        return {k: v for k, v in self.extensions.items() if k.lower() in IMAGE_EXTENSIONS}

    @property
    def is_light_sheet(self) -> bool:
        """True when the inventory's own technique field says light sheet.
        Undercounts: BIL tags many light-sheet datasets ``other`` in this
        field and only mentions light sheet in the abstract or instrument
        record. Use BilCatalog.light_sheet() for the union."""
        t = self.technique.lower()
        return "light sheet" in t or "lightsheet" in t or "light-sheet" in t or "lsfm" in t


IMAGE_EXTENSIONS = frozenset(
    {".tif", ".tiff", ".ome.tif", ".ome.tiff", ".jp2", ".ims", ".nii", ".nii.gz", ".png", ".jpg"}
)


@dataclass(frozen=True)
class Contributor:
    name: str
    contributor_type: str
    affiliation: str
    orcid: str


@dataclass(frozen=True)
class Publication:
    citation: str
    doi: str
    pmcid: str


@dataclass(frozen=True)
class DatasetDetail:
    """The full metadata record from ``/retrieve?bildid=``: everything the
    inventory row has, plus title, abstract, instrument, specimen, rights,
    contributors, funders and publications. ``raw`` keeps the untouched
    JSON for any field this dataclass does not surface."""

    bildid: str
    title: str
    abstract: str
    methods: str
    technique: str
    technique_other: str
    generalmodality: str
    bildirectory: str
    rights: str
    rights_uri: str
    rights_identifier: str
    doi: str
    dataset_size_gb: float | None
    number_of_files: int | None
    contributors: tuple[Contributor, ...]
    publications: tuple[Publication, ...]
    funders: tuple[dict[str, str], ...]
    instrument: dict[str, str]
    specimen: dict[str, str]
    images: tuple[dict[str, str], ...]
    submission: dict[str, str]
    raw: dict[str, Any] = field(repr=False, compare=False)

    @property
    def url(self) -> str:
        return dataset_url(self.bildirectory)

    @property
    def microscope_type(self) -> str:
        return self.instrument.get("microscopetype", "")

    @property
    def species(self) -> str:
        return self.specimen.get("species", "")

    @property
    def is_light_sheet(self) -> bool:
        """Light sheet by any of the three places BIL records it: the
        technique field, the instrument's microscope type, or the free-text
        ``other``/abstract fields."""
        blob = " ".join(
            [self.technique, self.technique_other, self.microscope_type, self.abstract, self.title]
        ).lower()
        return any(k in blob for k in ("light sheet", "lightsheet", "light-sheet", "lsfm"))

    @classmethod
    def from_json(cls, entry: dict[str, Any]) -> DatasetDetail:
        def first(key: str) -> dict[str, Any]:
            items = entry.get(key) or []
            if isinstance(items, list) and items:
                head = items[0]
                return dict(head) if isinstance(head, dict) else {}
            return dict(items) if isinstance(items, dict) else {}

        def all_of(key: str) -> list[dict[str, Any]]:
            items = entry.get(key) or []
            return [dict(i) for i in items if isinstance(i, dict)] if isinstance(items, list) else []

        ds = first("Dataset")
        assets = first("Assets")
        submission = {str(k): str(v) for k, v in first("Submission").items()}
        contributors = tuple(
            Contributor(
                name=str(c.get("contributorname", "")),
                contributor_type=str(c.get("contributortype", "")),
                affiliation=str(c.get("affiliation", "")),
                orcid=str(c.get("nameidentifier", "")),
            )
            for c in all_of("Contributors")
        )
        publications = tuple(
            Publication(
                citation=str(p.get("citation", "")),
                doi=str(p.get("relatedidentifier", "")),
                pmcid=str(p.get("pmcid", "")),
            )
            for p in all_of("Publication")
        )
        size = ds.get("dataset_size")
        try:
            size_gb: float | None = float(size) if size not in (None, "", "None") else None
        except (TypeError, ValueError):
            size_gb = None
        return cls(
            bildid=str(assets.get("bildid") or entry.get("bildid") or ""),
            title=str(ds.get("title", "")),
            abstract=str(ds.get("abstract", "")),
            methods=str(ds.get("methods", "")),
            technique=str(ds.get("technique", "")),
            technique_other=str(ds.get("other", "")),
            generalmodality=str(ds.get("generalmodality", "")),
            bildirectory=str(ds.get("bildirectory", "")),
            rights=str(ds.get("rights", "")),
            rights_uri=str(ds.get("rightsuri", "")),
            rights_identifier=str(ds.get("rightsidentifier", "")),
            doi=str(ds.get("doi") or assets.get("bildoi") or ""),
            dataset_size_gb=size_gb,
            number_of_files=_int_or_none(str(ds.get("number_of_files", ""))),
            contributors=contributors,
            publications=publications,
            funders=tuple({str(k): str(v) for k, v in f.items()} for f in all_of("Funders")),
            instrument={str(k): str(v) for k, v in first("Instrument").items()},
            specimen={str(k): str(v) for k, v in first("Specimen").items()},
            images=tuple({str(k): str(v) for k, v in i.items()} for i in all_of("Image")),
            submission=submission,
            raw=entry,
        )


@dataclass(frozen=True)
class FileEntry:
    """One file or directory on the download server, from either a
    directory listing (files.list_files) or a dataset manifest
    (files.manifest). ``path`` is relative to the dataset root when known;
    ``md5`` is only populated from a manifest."""

    name: str
    url: str
    size: int | None
    modified: str
    is_dir: bool
    path: str = ""
    md5: str = ""

    @property
    def extension(self) -> str:
        lower = self.name.lower()
        for ext in (".ome.tif", ".ome.tiff", ".nii.gz"):
            if lower.endswith(ext):
                return ext
        dot = lower.rfind(".")
        return lower[dot:] if dot >= 0 else ""
