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
metadata.txt, Zeiss CZI, BigDataViewer XML, OME-TIFF, Imaris, KLB). On
2026-10-17, 849 of 1,004 light-sheet records on BIL had an Instrument
block and about half of those had objective, detector and wavelength
fields; the rest were left blank by hand. These are exactly the values
the microscope wrote beside its planes.

Field names on the BIL side are the metadata API's own
(``stepsizex``, ``microscopemanufacturerandmodel`` and so on), so a
draft row can be pasted into the submission spreadsheet column for
column.
"""

from __future__ import annotations

import base64
import json
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
    return v if v == v else None  # NaN check


def _int(value: Any) -> int | None:
    v = _num(value)
    return int(v) if v is not None and v > 0 else None


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
    holding one), tolerating a torn last line."""
    p = Path(path)
    if p.is_dir():
        p = p / "index.jsonl"
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
                yield rec


def _um(value: Any, unit: Any = None) -> float | None:
    v = _num(value)
    if v is None:
        return None
    u = (str(unit) if unit else "").strip().lower()
    scale = {"nm": 1e-3, "mm": 1e3, "m": 1e6, "cm": 1e4, "pm": 1e-6}.get(u, 1.0)
    return v * scale


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
        lines = [str(int(_num(w) or 0)) for w in (f.get("channelWavelengthsNm") or []) if _num(w)]
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
        dets = [d for d in (f.get("detectorModels") or []) if d]
        row["detectormodel"] = ", ".join(str(d) for d in dets)
        row["detectortype"] = "CMOS" if dets else ""
        ex = [_fmt(_num(v)) for v in (f.get("channelExcitationNm") or []) if _num(v)]
        em = [_fmt(_num(v)) for v in (f.get("channelEmissionNm") or []) if _num(v)]
        row["illuminationwavelength"] = ", ".join(ex) + (" nm" if ex else "")
        row["detectionwavelength"] = ", ".join(em) + (" nm" if em else "")
    elif fmt == "imaris":
        mode = _text(f.get("microscopeMode")) or ""
        row["microscopetype"] = "Light-sheet" if "lightsheet" in mode.lower().replace(" ", "") else mode
        row["objectivemagnification"] = _fmt(_num(f.get("magnification")))
        row["objectivena"] = _fmt(_num(f.get("numericalAperture")))
        ex = [_fmt(_num(v)) for v in (f.get("excitationWavelengthsNm") or []) if _num(v)]
        row["illuminationwavelength"] = ", ".join(ex) + (" nm" if ex else "")
    elif fmt == "bdv-spimdata":
        row["microscopetype"] = "Light-sheet"
    return row


# Formats ranked by how much of the submission they can fill; the best
# available one supplies each column, later ones only fill blanks.
_PREFERENCE = ("smartspim-metadata", "czi", "bdv-spimdata", "ome-tiff", "imaris", "klb")


def _dir_of(source: str) -> str:
    source = source.replace("\\", "/")
    return source.rsplit("/", 1)[0] if "/" in source else ""


_PLANE_FORMATS = frozenset({"png", "tiff"})


def submission_drafts(records: Iterable[dict[str, Any]] | str | Path) -> dict[str, dict[str, Any]]:
    """One draft per acquisition directory.

    A BIL deposit describes one acquisition, and an edge box may watch a
    share holding several. Records are grouped by the directory of each
    non-plane record (a SmartSPIM metadata.txt, a CZI, a BigDataViewer
    XML, an OME-TIFF stack, an Imaris or KLB file); plane files (PNG,
    plain TIFF) are counted under the deepest such directory that
    contains them. Returns ``{directory: draft}`` with the same draft
    shape as :func:`submission_draft`.
    """
    recs = list(read_index(records)) if isinstance(records, (str, Path)) else list(records)
    groups: dict[str, list[dict[str, Any]]] = {}
    planes: list[dict[str, Any]] = []
    for r in recs:
        fmt = str((r.get("decoded") or {}).get("format", ""))
        if fmt in _PLANE_FORMATS:
            planes.append(r)
        else:
            groups.setdefault(_dir_of(str(r.get("source", ""))), []).append(r)
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
    "formats": {...}}`` with every BIL column present (blank where
    nothing on the box says), the records each value came from, and a
    per-format count of what was read. Plane files (PNG, plain TIFF) are
    counted, not used: they say width and height, which BIL does not ask
    for.
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
    return {"image": image, "instrument": instrument, "sources": sources, "formats": counts}
