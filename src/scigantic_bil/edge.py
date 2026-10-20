"""Bridge between a Brain Image Library deposit and an instrument box
running scigantic-edge.

Two directions, one record shape.

Deposit -> box: :func:`prior_capsule` turns a BIL metadata record into the
base64 capsule ``scigantic-trend -seed-from`` reads, so a new acquisition
is checked against what the lab last deposited: voxel step sizes, channel
count, objective, detector. The capsule is the four-key envelope
(type, author, description, card) that scigantic-edge's ``capsule``
package, the Python context producer and the Scigantic backend all write;
the card carries one ``bil-image`` context record per BIL Image row, with
the Instrument sheet's fields folded in.

Box -> deposit: :func:`submission_draft` reads the ``index.jsonl`` an edge
box wrote while watching the acquisition and fills in BIL's Image and
Instrument submission sheets from the headers it decoded (SmartSPIM
metadata.txt, Zeiss CZI, Luxendo and mesoSPIM sidecars, lattice Settings.txt,
BigDataViewer XML, Micro-Manager metadata, OME-TIFF, Imaris, KLB, ND2,
OME-Zarr stores and the ``manifest.json`` of an Allen Institute ``voxel``
acquisition). On 2026-10-17, 849 of 1,004 light-sheet records on BIL had an Instrument
block and about half of those had objective, detector and wavelength
fields; the rest were left blank by hand. These are exactly the values
the microscope wrote beside its planes.

Field names on the BIL side are the metadata API's own
(``stepsizex``, ``microscopemanufacturerandmodel`` and so on), so a
draft row can be pasted into the submission spreadsheet column for
column.

The prior's ``bil-image`` fields already cover what an OME-Zarr record on
the box can say about itself: ``stepSizeXUm``/``YUm``/``ZUm`` against the
store's level-0 scale in micrometres, ``channels`` against ``sizeC``,
``slices`` and ``sizeZ`` against ``sizeZ``, ``sizeX``/``sizeY`` against the
array shape, ``dimensionOrder`` against the axes names. Nothing OME-Zarr
specific is added to the prior.
"""

from __future__ import annotations

import base64
import json
import math
import posixpath
import re
from pathlib import Path
from typing import Any, Iterable, Iterator

from .api import retrieve
from .models import DatasetDetail

__all__ = [
    "BIL_IMAGE_COLUMNS",
    "BIL_INSTRUMENT_COLUMNS",
    "prior_records",
    "prior_capsule",
    "write_prior",
    "read_index",
    "submission_draft",
    "submission_drafts",
]

# BIL's submission template columns, in the order the API returns them.
BIL_IMAGE_COLUMNS = (
    "xaxis", "yaxis", "zaxis", "stepsizex", "stepsizey", "stepsizez", "stepsizet",
    "channels", "slices", "xsize", "ysize", "zsize", "dimensionorder", "flurophore",
    "displaycolor", "number", "gbytes", "files",
)
BIL_INSTRUMENT_COLUMNS = (
    "microscopetype", "microscopemanufacturerandmodel", "objectivename", "objectiveimmersion",
    "objectivena", "objectivemagnification", "detectortype", "detectormodel",
    "illuminationtypes", "illuminationwavelength", "detectionwavelength", "sampletemperature",
)


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        v = float(str(value).strip())
    except ValueError:
        return None
    return v if math.isfinite(v) else None  # NaN and +-inf are not values


def _int(value: Any) -> int | None:
    v = _num(value)
    return int(v) if v is not None and v > 0 else None


def _list(value: Any) -> list[Any]:
    """``value`` if the decoder wrote a list there, else an empty list."""
    return value if isinstance(value, list) else []


def _text(value: Any) -> str | None:
    s = str(value).strip() if value is not None else ""
    return s or None


# ---------------------------------------------------------------- deposit -> box


def prior_records(detail: DatasetDetail) -> list[dict[str, Any]]:
    """One ``bil-image`` context record per Image row of a BIL record.

    The record shape is scigantic-edge's ``producer.ContextRecord``
    (source, sizeBytes, mtime, decoded{format, summary, fields}); the
    fields are the ones ``signal.SetupValues`` reads for the ``bil-image``
    format. A record with no Image rows yields one record from the
    Instrument sheet alone, so the objective and detector still seed the
    prior.
    """
    inst = detail.instrument
    base: dict[str, Any] = {
        "bildid": detail.bildid,
        "microscopeType": _text(inst.get("microscopetype")),
        "microscopeModel": _text(inst.get("microscopemanufacturerandmodel")),
        "objectiveName": _text(inst.get("objectivename")),
        "objectiveImmersion": _text(inst.get("objectiveimmersion")),
        "objectiveMagnification": _num(inst.get("objectivemagnification")),
        "objectiveNA": _num(inst.get("objectivena")),
        "detectorType": _text(inst.get("detectortype")),
        "detectorModel": _text(inst.get("detectormodel")),
        "illuminationWavelength": _text(inst.get("illuminationwavelength")),
        "detectionWavelength": _text(inst.get("detectionwavelength")),
    }
    rows = list(detail.images) or [{}]
    out = []
    for i, img in enumerate(rows, 1):
        fields = dict(base)
        fields.update({
            "stepSizeXUm": _num(img.get("stepsizex")),
            "stepSizeYUm": _num(img.get("stepsizey")),
            "stepSizeZUm": _num(img.get("stepsizez")),
            "stepSizeT": _num(img.get("stepsizet")),
            "channels": _int(img.get("channels")),
            "slices": _int(img.get("slices")),
            "sizeX": _int(img.get("xsize")),
            "sizeY": _int(img.get("ysize")),
            "sizeZ": _int(img.get("zsize")),
            "dimensionOrder": _text(img.get("dimensionorder")),
            "axes": [_text(img.get(k)) for k in ("xaxis", "yaxis", "zaxis")],
            "fluorophore": _text(img.get("flurophore")),
        })
        steps = [fields["stepSizeXUm"], fields["stepSizeYUm"], fields["stepSizeZUm"]]
        parts = [f"BIL {detail.bildid}"]
        if fields["microscopeType"] or fields["microscopeModel"]:
            parts.append(" ".join(p for p in (fields["microscopeType"], fields["microscopeModel"]) if p))
        if all(s is not None for s in steps):
            parts.append("%g x %g x %g um steps" % tuple(steps))
        if fields["channels"]:
            parts.append(f"{fields['channels']} channel(s)")
        out.append({
            "source": f"bil:{detail.bildid}/Image[{i}]",
            "sizeBytes": 0,
            "mtime": 0,
            "decoded": {"format": "bil-image", "summary": ", ".join(parts), "fields": fields},
        })
    return out


def prior_capsule(detail: DatasetDetail | str) -> str:
    """Base64 capsule for ``scigantic-trend -seed-from``, built from a BIL
    record or id. Same envelope as scigantic-edge's capsule.Encode: the
    card is ``{"records": [...]}``."""
    if isinstance(detail, str):
        detail = retrieve(detail)
    envelope = {
        "type": "bil-dataset",
        "author": "Brain Image Library",
        "description": detail.title or detail.bildid,
        "card": {"records": prior_records(detail)},
    }
    return base64.b64encode(json.dumps(envelope, ensure_ascii=False).encode("utf-8")).decode("ascii")


def write_prior(detail: DatasetDetail | str, path: str | Path) -> Path:
    """Write :func:`prior_capsule` to ``path`` and return it."""
    out = Path(path)
    out.write_text(prior_capsule(detail), encoding="ascii")
    return out


# ---------------------------------------------------------------- box -> deposit


def read_index(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield the records of an edge box's ``index.jsonl`` (or a directory
    holding one), tolerating a torn last line.

    ``index.jsonl`` is an append log: a box in ``-watch`` mode writes a new
    line each time a source changes (an OME-Zarr store re-records as its
    shards land, a voxel ``manifest.json`` is rewritten on every status
    change). Only the last line for each source is yielded, in the order
    sources first appeared, which is what the box's own resume reads too.
    """
    p = Path(path)
    if p.is_dir():
        p = p / "index.jsonl"
    recs: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and isinstance(rec.get("decoded"), dict):
                recs.append(rec)
    yield from _latest(recs)


def _clean(r: Any) -> dict[str, Any] | None:
    """A record with a dict ``decoded``, a dict ``fields`` and string
    ``format`` and ``source``, or ``None`` for one that is not a record.
    Every reader below can then index into it without checking types."""
    if not isinstance(r, dict) or not isinstance(r.get("decoded"), dict):
        return None
    d = dict(r["decoded"])
    d["format"] = str(d.get("format") or "")
    d["fields"] = d["fields"] if isinstance(d.get("fields"), dict) else {}
    src = r.get("source")
    return dict(r, decoded=d, source=src if isinstance(src, str) else "")


def _latest(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The last record for each ``source``, in first-seen order. Records
    with no source are kept as they are."""
    by_source: dict[str, dict[str, Any]] = {}
    loose: list[tuple[int, dict[str, Any]]] = []
    order: dict[str, int] = {}
    for i, raw in enumerate(records):
        r = _clean(raw)
        if r is None:
            continue
        src = str(r.get("source", "")).replace("\\", "/").rstrip("/")
        if not src:
            loose.append((i, r))
            continue
        order.setdefault(src, i)
        by_source[src] = r
    keyed = [(order[k], v) for k, v in by_source.items()] + loose
    return [r for _, r in sorted(keyed, key=lambda t: t[0])]


# Micrometres per unit, keyed by the spellings seen in BigDataViewer XML,
# OME-TIFF (symbols) and OME-NGFF axes (UDUNITS-2 names: "micrometer",
# "nanometer", "millimeter"). British spellings and plurals are folded in
# before lookup. An unknown or missing unit is taken as micrometres, the
# convention every other decoder here already relies on.
_UM_PER_UNIT = {
    "pm": 1e-6, "picometer": 1e-6,
    "nm": 1e-3, "nanometer": 1e-3,
    "um": 1.0, "\u00b5m": 1.0, "\u03bcm": 1.0, "micrometer": 1.0, "micron": 1.0,
    "mm": 1e3, "millimeter": 1e3,
    "cm": 1e4, "centimeter": 1e4,
    "m": 1e6, "meter": 1e6,
    "angstrom": 1e-4, "\u00e5": 1e-4, "\u212b": 1e-4,
}


def _um(value: Any, unit: Any = None) -> float | None:
    v = _num(value)
    if v is None:
        return None
    u = (str(unit) if unit else "").strip().lower().replace("metre", "meter")
    if u not in _UM_PER_UNIT and len(u) > 3 and u.endswith("s"):
        u = u[:-1]  # microns, micrometers; never "ms"
    return v * _UM_PER_UNIT.get(u, 1.0)


def _um_declared(value: Any, unit: Any) -> float | None:
    """Like :func:`_um`, but ``None`` when the unit is missing or not a
    length. OME-NGFF makes an axis unit optional, and a scale with no unit
    is in no known unit at all, so it is not reported as micrometres."""
    v = _num(value)
    u = (str(unit) if unit else "").strip().lower().replace("metre", "meter")
    if u not in _UM_PER_UNIT and len(u) > 3 and u.endswith("s"):
        u = u[:-1]
    if v is None or u not in _UM_PER_UNIT:
        return None
    return v * _UM_PER_UNIT[u]


def _fmt(v: float | None) -> str:
    if v is None:
        return ""
    return ("%.6g" % v) if abs(v - round(v)) > 1e-9 else str(int(round(v)))


def _image_row_from(fmt: str, f: dict[str, Any]) -> dict[str, str]:
    row: dict[str, str] = {}
    sx = sy = sz = None
    channels: int | None = None
    slices: int | None = None
    xsize = ysize = zsize = None
    order = None
    if fmt == "smartspim-metadata":
        sx = sy = _num(f.get("pixelSizeUm"))
        sz = _num(f.get("zStepUm"))
        channels = _int(f.get("channels"))
        ysize = _int(f.get("verticalResolution"))
    elif fmt == "czi":
        sx, sy, sz = _num(f.get("scalingXUm")), _num(f.get("scalingYUm")), _num(f.get("scalingZUm"))
        channels, slices = _int(f.get("sizeC")), _int(f.get("sizeZ"))
        xsize, ysize, zsize = _int(f.get("sizeX")), _int(f.get("sizeY")), _int(f.get("sizeZ"))
    elif fmt == "bdv-spimdata":
        u = f.get("voxelSizeUnit")
        sx, sy, sz = _um(f.get("voxelSizeX"), u), _um(f.get("voxelSizeY"), u), _um(f.get("voxelSizeZ"), u)
        channels, slices = _int(f.get("channels")), _int(f.get("sizeZ"))
        xsize, ysize, zsize = _int(f.get("sizeX")), _int(f.get("sizeY")), _int(f.get("sizeZ"))
    elif fmt == "ome-tiff":
        sx = _um(f.get("physicalSizeX"), f.get("physicalSizeXUnit"))
        sy = _um(f.get("physicalSizeY"), f.get("physicalSizeYUnit"))
        sz = _um(f.get("physicalSizeZ"), f.get("physicalSizeZUnit"))
        channels, slices = _int(f.get("sizeC")), _int(f.get("sizeZ"))
        xsize, ysize, zsize = _int(f.get("sizeX")), _int(f.get("sizeY")), _int(f.get("sizeZ"))
        order = _text(f.get("dimensionOrder"))
    elif fmt == "imaris":
        u = f.get("unit")
        sx, sy, sz = _um(f.get("voxelSizeX"), u), _um(f.get("voxelSizeY"), u), _um(f.get("voxelSizeZ"), u)
        channels, slices = _int(f.get("channels")), _int(f.get("sizeZ"))
        xsize, ysize, zsize = _int(f.get("sizeX")), _int(f.get("sizeY")), _int(f.get("sizeZ"))
    elif fmt == "klb":
        sx, sy, sz = _num(f.get("pixelSizeX")), _num(f.get("pixelSizeY")), _num(f.get("pixelSizeZ"))
        channels, slices = _int(f.get("sizeC")), _int(f.get("sizeZ"))
        xsize, ysize, zsize = _int(f.get("sizeX")), _int(f.get("sizeY")), _int(f.get("sizeZ"))
    elif fmt == "mesospim-meta":
        sx = sy = _num(f.get("pixelSizeUm"))
        sz = _num(f.get("zStepUm"))
        channels, slices = _int(f.get("channels")), _int(f.get("zPlanes"))
        xsize, ysize = _int(f.get("imageWidth")), _int(f.get("imageLength"))
    elif fmt == "luxendo-stack":
        sx, sy, sz = _num(f.get("voxelSizeXUm")), _num(f.get("voxelSizeYUm")), _num(f.get("voxelSizeZUm"))
        slices = _int(f.get("sizeZ"))
        xsize, ysize, zsize = _int(f.get("sizeX")), _int(f.get("sizeY")), _int(f.get("sizeZ"))
        on = _list(f.get("lasersOnNm"))
        channels = len(on) if on else None
    elif fmt == "lls-settings":
        sz = _num(f.get("stepUm"))
        channels, slices = _int(f.get("channels")), _int(f.get("planes"))
        xsize, ysize = _int(f.get("imageWidth")), _int(f.get("imageLength"))
    elif fmt == "micromanager-metadata":
        sx = sy = _num(f.get("pixelSizeUm"))
        sz = _num(f.get("zStepUm"))
        channels, slices = _int(f.get("channels")), _int(f.get("slices"))
        xsize, ysize = _int(f.get("imageWidth")), _int(f.get("imageLength"))
    elif fmt == "nd2":
        sx = sy = _num(f.get("pixelSizeUm"))
        xsize, ysize = _int(f.get("imageWidth")), _int(f.get("imageLength"))
    elif fmt == "ome-zarr":
        # voxelSize* is the level-0 scale transform in voxelSizeUnit (an
        # NGFF axis unit name, "micrometer" in every store seen so far).
        # A missing or non-length unit leaves the step sizes blank.
        u = f.get("voxelSizeUnit")
        sx, sy, sz = (_um_declared(f.get(k), u) for k in ("voxelSizeX", "voxelSizeY", "voxelSizeZ"))
        channels, slices = _int(f.get("sizeC")), _int(f.get("sizeZ"))
        xsize, ysize, zsize = _int(f.get("sizeX")), _int(f.get("sizeY")), _int(f.get("sizeZ"))
        names = [str(a.get("name") or "") for a in _list(f.get("axes")) if isinstance(a, dict)]
        order = "".join(names).upper() or None
    else:
        return row
    row["stepsizex"], row["stepsizey"], row["stepsizez"] = _fmt(sx), _fmt(sy), _fmt(sz)
    row["channels"] = _fmt(channels)
    row["slices"] = _fmt(slices)
    row["xsize"], row["ysize"], row["zsize"] = _fmt(xsize), _fmt(ysize), _fmt(zsize)
    row["dimensionorder"] = order or ""
    return row


def _instrument_row_from(fmt: str, f: dict[str, Any]) -> dict[str, str]:
    row: dict[str, str] = {}
    if fmt == "smartspim-metadata":
        row["microscopetype"] = "Light-sheet"
        row["microscopemanufacturerandmodel"] = "LifeCanvas SmartSPIM"
        row["objectivename"] = _text(f.get("objective")) or ""
        row["objectivemagnification"] = _fmt(_num(f.get("magnification")))
        row["objectiveimmersion"] = _text(f.get("immersion")) or ""
        lines = [str(int(_num(w) or 0)) for w in _list(f.get("channelWavelengthsNm")) if _num(w)]
        row["illuminationwavelength"] = ", ".join(lines) + (" nm" if lines else "")
        row["illuminationtypes"] = "laser" if lines else ""
    elif fmt == "czi":
        mode = str(f.get("acquisitionMode") or "")
        row["microscopetype"] = "Light-sheet" if f.get("lightSheet") or "ightsheet" in mode.lower() else ""
        model = _text(f.get("microscopeName")) or _text(f.get("microscopeSystem")) or ""
        row["microscopemanufacturerandmodel"] = f"Zeiss {model}".strip()
        row["objectivename"] = _text(f.get("objectiveName")) or ""
        row["objectivena"] = _fmt(_num(f.get("objectiveNA")))
        row["objectivemagnification"] = _fmt(_num(f.get("objectiveMagnification")))
        row["objectiveimmersion"] = _text(f.get("objectiveImmersion")) or ""
        dets = [d for d in _list(f.get("detectorModels")) if d]
        row["detectormodel"] = ", ".join(str(d) for d in dets)
        row["detectortype"] = "CMOS" if dets else ""
        ex = [_fmt(_num(v)) for v in _list(f.get("channelExcitationNm")) if _num(v)]
        em = [_fmt(_num(v)) for v in _list(f.get("channelEmissionNm")) if _num(v)]
        row["illuminationwavelength"] = ", ".join(ex) + (" nm" if ex else "")
        row["detectionwavelength"] = ", ".join(em) + (" nm" if em else "")
    elif fmt == "imaris":
        mode = _text(f.get("microscopeMode")) or ""
        row["microscopetype"] = "Light-sheet" if "lightsheet" in mode.lower().replace(" ", "") else mode
        row["objectivemagnification"] = _fmt(_num(f.get("magnification")))
        row["objectivena"] = _fmt(_num(f.get("numericalAperture")))
        ex = [_fmt(_num(v)) for v in _list(f.get("excitationWavelengthsNm")) if _num(v)]
        row["illuminationwavelength"] = ", ".join(ex) + (" nm" if ex else "")
    elif fmt == "bdv-spimdata":
        row["microscopetype"] = "Light-sheet"
    elif fmt == "mesospim-meta":
        row["microscopetype"] = "Light-sheet"
        row["microscopemanufacturerandmodel"] = "mesoSPIM"
        row["objectivename"] = _text(f.get("zoom")) or ""
        row["objectivemagnification"] = _fmt(_num(f.get("magnification")))
        row["detectortype"] = "CMOS" if f.get("cameraType") else ""
        row["detectormodel"] = _text(f.get("cameraType")) or ""
        lines = [str(int(_num(str(w).split()[0]) or 0)) for w in _list(f.get("lasers")) if _num(str(w).split()[0])]
        row["illuminationwavelength"] = ", ".join(lines) + (" nm" if lines else "")
        row["illuminationtypes"] = "laser" if lines else ""
    elif fmt == "luxendo-stack":
        row["microscopetype"] = "Light-sheet"
        model = _text(f.get("scopeType")) or ""
        row["microscopemanufacturerandmodel"] = ("Luxendo " + model).strip()
        row["objectivename"] = _text(f.get("objectiveName")) or ""
        row["objectivena"] = _fmt(_num(f.get("numericalAperture")))
        row["objectivemagnification"] = _fmt(_num(f.get("magnification")))
        row["objectiveimmersion"] = _text(f.get("objectiveImmersion")) or ""
        on = [_fmt(_num(v)) for v in _list(f.get("lasersOnNm")) if _num(v)]
        row["illuminationwavelength"] = ", ".join(on) + (" nm" if on else "")
        row["illuminationtypes"] = "laser" if on else ""
    elif fmt == "lls-settings":
        row["microscopetype"] = "Light-sheet"
        row["microscopemanufacturerandmodel"] = "Lattice light-sheet (Janelia design)"
        row["detectortype"] = "CMOS" if f.get("cameraModel") else ""
        row["detectormodel"] = _text(f.get("cameraModel")) or ""
        lines = [_fmt(_num(v)) for v in _list(f.get("laserLinesNm")) if _num(v)]
        row["illuminationwavelength"] = ", ".join(lines) + (" nm" if lines else "")
        row["illuminationtypes"] = "laser" if lines else ""
    elif fmt == "nd2":
        row["microscopemanufacturerandmodel"] = "Nikon"
        row["objectivename"] = _text(f.get("objectiveName")) or ""
        row["objectivena"] = _fmt(_num(f.get("numericalAperture")))
        row["objectivemagnification"] = _fmt(_num(f.get("magnification")))
        row["detectormodel"] = _text(f.get("cameraName")) or ""
    elif fmt == "voxel-manifest":
        # voxel is a generic acquisition engine; its template instrument is
        # a simulator. The manifest says light-sheet only when the
        # instrument's name does (exaSPIM, diSPIM, ...), so the type is left
        # blank otherwise rather than guessed from the engine's origin.
        name = _text(f.get("instrumentName")) or ""
        row["microscopetype"] = "Light-sheet" if _names_light_sheet(name) else ""
        row["microscopemanufacturerandmodel"] = name
        row["objectivename"] = _text(f.get("objectiveName")) or ""
        row["objectivena"] = _fmt(_num(f.get("objectiveNA")))
        row["objectivemagnification"] = _fmt(_num(f.get("objectiveMagnification")))
        row["detectormodel"] = _text(f.get("cameraModel")) or ""
        lines = [_fmt(_num(v)) for v in _list(f.get("laserWavelengthsNm")) if _num(v)]
        row["illuminationwavelength"] = ", ".join(lines) + (" nm" if lines else "")
        row["illuminationtypes"] = "laser" if lines else ""
        em_table = f.get("channelEmissionNm")
        em = [_fmt(_num(v)) for v in (em_table.values() if isinstance(em_table, dict) else []) if _num(v)]
        row["detectionwavelength"] = ", ".join(em) + (" nm" if em else "")
    return row


def _names_light_sheet(name: str) -> bool:
    flat = name.lower().replace("-", "").replace("_", "").replace(" ", "")
    return any(tag in flat for tag in ("lightsheet", "spim", "lsfm"))


# Formats ranked by how much of the submission they can fill; the best
# available one supplies each column, later ones only fill blanks.
_PREFERENCE = ("smartspim-metadata", "czi", "luxendo-stack", "mesospim-meta", "lls-settings", "voxel-manifest",
               "bdv-spimdata", "micromanager-metadata", "ome-zarr", "ome-tiff", "imaris", "klb", "nd2")


def _dir_of(source: str) -> str:
    source = source.replace("\\", "/")
    return source.rsplit("/", 1)[0] if "/" in source else ""


_PLANE_FORMATS = frozenset({"png", "tiff", "dcimg"})
_STORE_METADATA = frozenset({"zarr.json", ".zattrs", ".zgroup", ".zarray"})


def _store_of(source: str) -> str:
    """The store directory an ``ome-zarr`` record describes: the deepest
    path component ending in ``.zarr``, so a record keyed on the store's
    ``zarr.json`` or ``.zattrs`` and one keyed on the directory agree. A
    source with no such component is its own store (or, for a bare
    metadata file, its directory)."""
    s = source.replace("\\", "/").rstrip("/")
    parts = s.split("/")
    for i in range(len(parts), 0, -1):
        if parts[i - 1].lower().endswith(".zarr"):
            return "/".join(parts[:i])
    if parts[-1] in _STORE_METADATA:
        return "/".join(parts[:-1])
    return s


def _store_name(store: str) -> str:
    base = store.rsplit("/", 1)[-1]
    low = base.lower()
    for suffix in (".ome.zarr", ".zarr"):
        if low.endswith(suffix):
            return base[: -len(suffix)]
    return base


def _voxel_volume(store: str) -> tuple[str, str] | None:
    """``(acquisition root, volume directory)`` for a store laid out as
    voxel writes it, ``<root>/volumes/NNNNNN/<profile>/<channel>.ome.zarr``;
    ``None`` for any other path."""
    parts = store.split("/")
    for i in range(len(parts) - 2):
        if parts[i] == "volumes" and parts[i + 1].isdigit():
            return "/".join(parts[:i]), "/".join(parts[: i + 2])
    return None


def _manifest_candidates(store: str, f: dict[str, Any]) -> list[str]:
    """Where a store's ``manifest.json`` sidecar would sit in the index:
    the sidecar resolved against the store directory (the decoder writes
    sidecars relative to the store) and the sidecar as written."""
    out: list[str] = []
    for sc in _list(f.get("sidecars")):
        s = str(sc).replace("\\", "/")
        if s.rsplit("/", 1)[-1] != "manifest.json":
            continue
        if not s.startswith("/"):
            out.append(posixpath.normpath(posixpath.join(store, s)))
        out.append(s)
    return out


def _store_home(source: str, f: dict[str, Any], manifests: dict[str, str], store_paths: list[tuple[str, str]]) -> str:
    """The acquisition directory an ``ome-zarr`` record belongs to; the
    rule is spelled out on :func:`submission_drafts`."""
    store = _store_of(source)
    if f.get("layout") == "group":
        # A plain container group (AIND's SPIM.ome.zarr holding one store
        # per tile and channel) is the acquisition its member stores sit
        # in, so it joins their draft instead of starting one of its own.
        return store
    for cand in _manifest_candidates(store, f):
        if cand in manifests:
            return manifests[cand]
    for path, home in store_paths:
        if store in (path, posixpath.normpath(posixpath.join(home, path)) if home else path) or path.endswith("/" + store):
            return home
    vol = _voxel_volume(store)
    if vol is not None:
        return vol[0]
    return _dir_of(store)


def submission_drafts(records: Iterable[dict[str, Any]] | str | Path) -> dict[str, dict[str, Any]]:
    """One draft per acquisition directory.

    A BIL deposit describes one acquisition, and an edge box may watch a
    share holding several. Records are grouped by the directory of each
    non-plane record (a SmartSPIM metadata.txt, a CZI, a BigDataViewer
    XML, an OME-TIFF stack, an Imaris or KLB file); plane files (PNG,
    plain TIFF) are counted under the deepest such directory that
    contains them. Returns ``{directory: draft}`` with the same draft
    shape as :func:`submission_draft`.

    An ``ome-zarr`` record is keyed on its store (the deepest path
    component ending in ``.zarr``, whether the record names the directory
    or its ``zarr.json``) and goes to the first of these that applies:

    1. the directory of a ``voxel-manifest`` record that one of the store's
       ``sidecars`` points at (``../../../manifest.json`` resolved against
       the store, or the sidecar as written);
    2. the directory of a ``voxel-manifest`` record whose ``storePaths``
       lists the store (relative to the manifest's directory, as the
       decoder writes them, or equal to the store's path);
    3. the path prefix before ``/volumes/NNNNNN/``, voxel's layout, when
       no manifest has been indexed yet;
    4. the store's parent directory, like any other file. The public
       AIND layout ``SPIM.ome.zarr/tile_NNNNNN_ch_NNN.zarr`` therefore
       collapses to one draft per ``SPIM.ome.zarr``.

    A voxel acquisition is one BIL submission made of many single-channel
    stores (one per volume and channel). Within such a group the Image
    row's ``channels`` is the number of distinct channels across the
    volumes (each store's ``fields.manifest.channelId``, or its
    ``<channel>.ome.zarr`` name when the decoder paired no manifest), not
    the ``sizeC`` of one store (always 1); volumes are counted the same
    way (``fields.manifest.volumeIndex``, else the ``volumes/NNNNNN``
    directory). The draft's ``acquisition`` block carries what BIL's
    sheets have no column for: store and volume counts, channel names,
    the manifest's status, revision and timestamps.
    """
    recs = list(read_index(records)) if isinstance(records, (str, Path)) else _latest(records)
    manifests: dict[str, str] = {}
    store_paths: list[tuple[str, str]] = []
    for r in recs:
        d = r.get("decoded") or {}
        if str(d.get("format", "")) != "voxel-manifest":
            continue
        src = str(r.get("source", "")).replace("\\", "/")
        manifests[src] = _dir_of(src)
        for p in _list((d.get("fields") or {}).get("storePaths")):
            store_paths.append((str(p).replace("\\", "/").rstrip("/"), manifests[src]))
    groups: dict[str, list[dict[str, Any]]] = {}
    planes: list[dict[str, Any]] = []
    for r in recs:
        d = r.get("decoded") or {}
        fmt = str(d.get("format", ""))
        src = str(r.get("source", ""))
        if fmt in _PLANE_FORMATS:
            planes.append(r)
        elif fmt == "ome-zarr":
            groups.setdefault(_store_home(src, d.get("fields") or {}, manifests, store_paths), []).append(r)
        else:
            groups.setdefault(_dir_of(src), []).append(r)
    if not groups:
        groups[""] = []
    dirs = sorted(groups, key=len, reverse=True)
    for pr in planes:
        d = _dir_of(str(pr.get("source", "")))
        home = next((g for g in dirs if g == "" or d == g or d.startswith(g + "/")), dirs[-1])
        groups[home].append(pr)
    return {d: _draft(groups[d]) for d in sorted(groups)}


def submission_draft(records: Iterable[dict[str, Any]] | str | Path, cohort: str | None = None) -> dict[str, Any]:
    """Draft BIL Image and Instrument sheet rows from edge context records.

    ``records`` is an ``index.jsonl`` path (or its directory) or an
    iterable of decoded records. When the records span several acquisition
    directories, ``cohort`` names the one to draft (see
    :func:`submission_drafts`); with no ``cohort`` and several
    directories, a ``ValueError`` lists them rather than blending two
    instruments into one deposit.

    Returns ``{"image": {...}, "instrument": {...}, "sources": [...],
    "formats": {...}, "acquisition": {...}}`` with every BIL column
    present (blank where nothing on the box says), the records each value
    came from, a per-format count of what was read, and, for OME-Zarr
    stores and voxel manifests, the counts and status BIL has no column
    for (empty otherwise; see :func:`submission_drafts`). Plane files
    (PNG, plain TIFF) are counted, not used: they say width and height,
    which BIL does not ask for.
    """
    drafts = submission_drafts(records)
    if cohort is not None:
        if cohort not in drafts:
            raise ValueError(f"no acquisition directory {cohort!r}; have: {sorted(drafts)}")
        return drafts[cohort]
    if len(drafts) > 1:
        raise ValueError("records span several acquisition directories; pass cohort= one of: " + ", ".join(sorted(drafts)))
    return next(iter(drafts.values()))


def _draft(recs: list[dict[str, Any]]) -> dict[str, Any]:
    by_format: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    for r in recs:
        d = r.get("decoded") or {}
        fmt = str(d.get("format", ""))
        counts[fmt] = counts.get(fmt, 0) + 1
        by_format.setdefault(fmt, []).append(r)
    image = {c: "" for c in BIL_IMAGE_COLUMNS}
    instrument = {c: "" for c in BIL_INSTRUMENT_COLUMNS}
    sources: list[str] = []
    stores = [r for r in by_format.get("ome-zarr", []) if _has_image(r)]
    if stores:
        # One store supplies the whole Image row. Filling blanks from the
        # next store would pair one store's voxel size with another's
        # shape when the channels of an acquisition differ in resolution.
        by_format["ome-zarr"] = [min(stores, key=_reference_rank)]
    for fmt in _PREFERENCE:
        for r in by_format.get(fmt, []):
            f = (r.get("decoded") or {}).get("fields") or {}
            used = False
            for col, val in _image_row_from(fmt, f).items():
                if val and not image.get(col):
                    image[col] = val
                    used = True
            for col, val in _instrument_row_from(fmt, f).items():
                if val and not instrument.get(col):
                    instrument[col] = val
                    used = True
            if used:
                sources.append(str(r.get("source", "")))
    planes = sum(n for fmt, n in counts.items() if fmt in _PLANE_FORMATS)
    if planes and not image["slices"]:
        # One plane per file is the SmartSPIM and UltraMicroscope layout;
        # a count of files is a lower bound on slices x channels x tiles,
        # reported as files rather than guessed into slices.
        image["files"] = str(planes)
    all_stores = [r for r in recs if str((r.get("decoded") or {}).get("format", "")) == "ome-zarr"]
    acquisition = _acquisition(all_stores, by_format.get("voxel-manifest", []), image, sources)
    return {"image": image, "instrument": instrument, "sources": sources, "formats": counts, "acquisition": acquisition}


_STATE_RANK = {"complete": 0, "partial": 1, "metadata-only": 2}
_COMPARED = ("voxelSizeX", "voxelSizeY", "voxelSizeZ", "voxelSizeUnit", "sizeX", "sizeY", "sizeZ", "dtype")
_AIND_TILE_CHANNEL = re.compile(r"^(?P<tile>.+?)_ch_(?P<ch>[A-Za-z0-9]+)$")


def _fields(r: dict[str, Any]) -> dict[str, Any]:
    f = (r.get("decoded") or {}).get("fields")
    return f if isinstance(f, dict) else {}


def _has_image(r: dict[str, Any]) -> bool:
    f = _fields(r)
    return f.get("layout") != "group" and any(f.get(k) is not None for k in ("sizeX", "voxelSizeX"))


def _reference_rank(r: dict[str, Any]) -> tuple[int, int, float, str]:
    """The store an acquisition's Image row is drafted from: a store with
    a level-0 shape before one without, a complete store before a partial
    one, then the finest voxel."""
    f = _fields(r)
    vx = _num(f.get("voxelSizeX"))
    return (0 if f.get("sizeX") is not None else 1, _STATE_RANK.get(str(f.get("state")), 3),
            vx if vx is not None else float("inf"), str(r.get("source", "")))


def _mismatch(stores: list[dict[str, Any]]) -> dict[str, list[Any]]:
    """Fields the stores of one acquisition disagree on, with the distinct
    values; a store that lacks a field does not count as disagreeing."""
    out: dict[str, list[Any]] = {}
    for k in _COMPARED:
        vals = {json.dumps(_fields(r)[k]) for r in stores if _fields(r).get(k) is not None}
        if len(vals) > 1:
            out[k] = sorted((json.loads(v) for v in vals),
                            key=lambda v: (not isinstance(v, (int, float)), v if isinstance(v, (int, float)) else 0, str(v)))
    return out


_MANIFEST_KEYS = (("status", "status"), ("revision", "revision"), ("startedAt", "startedAt"), ("endedAt", "endedAt"),
                  ("volumesPlanned", "volumeCount"), ("volumesCompleted", "completedVolumes"))


def _acquisition(stores: list[dict[str, Any]], manifests: list[dict[str, Any]],
                 image: dict[str, str], sources: list[str]) -> dict[str, Any]:
    """The ``acquisition`` block of a draft, and the one Image column it
    corrects: ``channels`` of a voxel acquisition, counted across its
    single-channel stores."""
    out: dict[str, Any] = {}
    if not stores and not manifests:
        return out
    images = [r for r in stores if _fields(r).get("layout") != "group"]
    out["stores"] = len(images)
    states: dict[str, int] = {}
    for r in images:
        st = str(_fields(r).get("state") or "unknown")
        states[st] = states.get(st, 0) + 1
    if states:
        out["storeStates"] = dict(sorted(states.items()))
    diff = _mismatch(images)
    if diff:
        out["mismatch"] = diff
    stores = images
    first_store_of: dict[str, str] = {}
    volumes: set[str] = set()
    for r in stores:
        store = _store_of(str(r.get("source", "")))
        view = ((r.get("decoded") or {}).get("fields") or {}).get("manifest")
        view = view if isinstance(view, dict) else {}
        vol = _voxel_volume(store)
        if not view and vol is None:
            # AIND's file standard names each store tile_..._ch_<channel>;
            # count tiles and channels from that, and nothing else.
            aind = _AIND_TILE_CHANNEL.match(_store_name(store))
            if aind:
                volumes.add(aind.group("tile"))
                first_store_of.setdefault(aind.group("ch"), store)
            continue
        volumes.add(str(view.get("volumeIndex")) if view.get("volumeIndex") is not None else (vol[1] if vol else store))
        first_store_of.setdefault(_text(view.get("channelId")) or _store_name(store), store)
    if volumes:
        out["volumes"] = len(volumes)
        out["channels"] = sorted(first_store_of)
    if manifests:
        m = manifests[0]
        f = (m.get("decoded") or {}).get("fields") or {}
        out["manifest"] = str(m.get("source", ""))
        for k_out, k_in in _MANIFEST_KEYS:
            if f.get(k_in) is not None:
                out[k_out] = f[k_in]
        if "channels" not in out and _list(f.get("channelIds")):
            out["channels"] = [str(c) for c in _list(f.get("channelIds"))]
    if volumes:
        image["channels"] = str(len(first_store_of))
        for store in first_store_of.values():
            if not any(_store_of(s) == store for s in sources):
                sources.append(store)
    elif out.get("channels") and not image["channels"]:
        image["channels"] = str(len(out["channels"]))
    return out
