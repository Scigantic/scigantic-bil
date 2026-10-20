"""The scigantic-edge bridge. The capsule and draft shapes are checked
offline against a record captured from the metadata API on 2026-10-17
(ace-cup-eel, a Zeiss Z.1 light-sheet deposit) and against edge context
records in the shapes scigantic-edge's decoders write; one test fetches the
same record live to confirm the capture has not drifted."""

from __future__ import annotations

import base64
import json
from typing import Any
from pathlib import Path

import scigantic_bil as bil
import pytest

from scigantic_bil.edge import BIL_IMAGE_COLUMNS, BIL_INSTRUMENT_COLUMNS, prior_records, submission_draft, submission_drafts
from scigantic_bil.models import DatasetDetail

ACE_CUP_EEL = {
    "Submission": [{"bildate": "2022-08-17"}],
    "Assets": [{"bildid": "ace-cup-eel", "bildoi": ""}],
    "Dataset": [{"bildirectory": "/bil/data/42/e4/42e4274e0579397f/subject_5",
                 "title": "Light-sheet imaged brain follwing T.Gondii injection: Subject 5",
                 "technique": "other", "other": "iDISCO+ light sheet", "generalmodality": "connectivity",
                 "rightsidentifier": "CC-BY-SA-4.0", "dataset_size": "54.017502613", "number_of_files": "15323"}],
    "Instrument": [{"microscopetype": "Light-sheet", "microscopemanufacturerandmodel": "Zeiss Z.1", "objectivename": "",
                    "objectiveimmersion": "DBE/Air", "objectivena": "", "objectivemagnification": "", "detectortype": "CMOS",
                    "detectormodel": "", "illuminationtypes": "", "illuminationwavelength": "400–850 nm",
                    "detectionwavelength": "", "sampletemperature": ""}],
    "Image": [{"xaxis": "anterior-to-posterior", "yaxis": "superior-to-inferior", "zaxis": "left-to-right", "number": "1.0",
               "displaycolor": "(1,1,1)", "stepsizex": "25.0", "stepsizey": "25.0", "stepsizez": "25.0", "stepsizet": "",
               "channels": "", "slices": "", "xsize": "", "ysize": "", "zsize": "", "gbytes": "", "files": "", "dimensionorder": ""}],
    "Specimen": [{"species": "mouse"}],
    "Contributors": [], "Funders": [], "Publication": [],
}


def test_prior_records_from_captured_record() -> None:
    d = DatasetDetail.from_json(ACE_CUP_EEL)
    recs = prior_records(d)
    assert len(recs) == 1
    r = recs[0]
    assert r["source"] == "bil:ace-cup-eel/Image[1]"
    assert r["decoded"]["format"] == "bil-image"
    f = r["decoded"]["fields"]
    assert (f["stepSizeXUm"], f["stepSizeYUm"], f["stepSizeZUm"]) == (25.0, 25.0, 25.0)
    assert f["microscopeModel"] == "Zeiss Z.1" and f["detectorType"] == "CMOS"
    assert f["objectiveNA"] is None and f["channels"] is None  # blanks stay blank, never 0
    assert "25 x 25 x 25 um steps" in r["decoded"]["summary"]


def test_prior_capsule_is_the_shared_envelope(tmp_path: Path) -> None:
    d = DatasetDetail.from_json(ACE_CUP_EEL)
    out = bil.write_prior(d, tmp_path / "prior.b64")
    envelope = json.loads(base64.b64decode(out.read_text()))
    assert set(envelope) == {"type", "author", "description", "card"}
    assert envelope["type"] == "bil-dataset"
    assert envelope["card"]["records"][0]["decoded"]["fields"]["stepSizeZUm"] == 25.0


def test_prior_records_without_image_rows_still_carry_the_instrument() -> None:
    raw = dict(ACE_CUP_EEL)
    raw["Image"] = []
    recs = prior_records(DatasetDetail.from_json(raw))
    assert len(recs) == 1 and recs[0]["decoded"]["fields"]["microscopeModel"] == "Zeiss Z.1"
    assert recs[0]["decoded"]["fields"]["stepSizeXUm"] is None


# Context records in the shapes scigantic-edge writes (header/smartspim.go,
# czi/czi.go, header/png.go), trimmed to the fields the draft reads.
SMARTSPIM = {"source": "run/metadata.txt", "sizeBytes": 3982, "mtime": 0, "decoded": {"format": "smartspim-metadata", "summary": "", "fields": {
    "objective": "LCT 3.6x", "magnification": 3.6, "verticalResolution": 1600.0, "pixelSizeUm": 1.8, "zStepUm": 2.0,
    "immersion": None, "channels": 3, "channelWavelengthsNm": [445.0, 488.0, 561.0], "tilePositions": 40}}}
CZI = {"source": "run/stack.czi", "sizeBytes": 1, "mtime": 0, "decoded": {"format": "czi", "summary": "", "fields": {
    "sizeX": 1920, "sizeY": 1920, "sizeZ": 1200, "sizeC": 2, "scalingXUm": 0.2285, "scalingYUm": 0.2285, "scalingZUm": 0.914,
    "microscopeName": "Lightsheet Z.1", "microscopeSystem": "Lightsheet Z.1", "objectiveName": "W Plan-Apochromat 20x/1.0 UV-VIS",
    "objectiveNA": 1.0, "objectiveMagnification": 20.0, "objectiveImmersion": "Water", "detectorModels": ["pco.edge 5.5"],
    "channelExcitationNm": [488.0, 561.0], "channelEmissionNm": [509.0, 581.0], "acquisitionMode": "LightSheet", "lightSheet": True}}}
PLANE = {"source": "run/Ex_488_Em_525/436550/436550_310350/000000.png", "sizeBytes": 1493527, "mtime": 0,
         "decoded": {"format": "png", "summary": "", "fields": {"imageWidth": 2000, "imageLength": 1600, "dtype": "uint16"}}}


def test_submission_draft_from_smartspim_records(tmp_path: Path) -> None:
    index = tmp_path / "index.jsonl"
    planes = [dict(PLANE, source=PLANE["source"].replace("000000", f"{i:06d}")) for i in range(3)]
    index.write_text("\n".join(json.dumps(r) for r in [planes[0], planes[1], SMARTSPIM, planes[2]]) + '\n{"torn', encoding="utf-8")
    draft = submission_draft(tmp_path)
    assert set(draft["image"]) == set(BIL_IMAGE_COLUMNS)
    assert set(draft["instrument"]) == set(BIL_INSTRUMENT_COLUMNS)
    img, inst = draft["image"], draft["instrument"]
    assert (img["stepsizex"], img["stepsizey"], img["stepsizez"], img["channels"]) == ("1.8", "1.8", "2", "3")
    assert img["ysize"] == "1600" and img["xsize"] == ""
    assert inst["microscopemanufacturerandmodel"] == "LifeCanvas SmartSPIM"
    assert inst["objectivename"] == "LCT 3.6x" and inst["objectivemagnification"] == "3.6"
    assert inst["illuminationwavelength"] == "445, 488, 561 nm"
    assert img["files"] == "3" and draft["formats"] == {"png": 3, "smartspim-metadata": 1}
    assert draft["sources"] == ["run/metadata.txt"]


def test_submission_draft_prefers_richer_format_and_fills_blanks() -> None:
    draft = submission_draft([CZI, SMARTSPIM])  # same directory: one acquisition
    inst = draft["instrument"]
    # SmartSPIM ranks first for the columns it has; the CZI fills what it lacks.
    assert inst["microscopemanufacturerandmodel"] == "LifeCanvas SmartSPIM"
    assert inst["objectivena"] == "1" and inst["detectormodel"] == "pco.edge 5.5"
    assert draft["image"]["stepsizez"] == "2" and draft["image"]["zsize"] == "1200"
    only_czi = submission_draft([CZI])
    assert only_czi["instrument"]["microscopemanufacturerandmodel"] == "Zeiss Lightsheet Z.1"
    assert only_czi["instrument"]["microscopetype"] == "Light-sheet"
    assert only_czi["image"]["stepsizex"] == "0.2285" and only_czi["image"]["channels"] == "2"


def test_two_acquisitions_are_two_drafts_not_one_blend() -> None:
    other = json.loads(json.dumps(CZI))
    other["source"] = "zeiss/brain07/stack.czi"
    far_plane = json.loads(json.dumps(PLANE))
    far_plane["source"] = "zeiss/brain07/preview.png"
    with pytest.raises(ValueError, match="several acquisition directories"):
        submission_draft([SMARTSPIM, other])
    drafts = submission_drafts([SMARTSPIM, PLANE, other, far_plane])
    assert set(drafts) == {"run", "zeiss/brain07"}
    assert drafts["run"]["instrument"]["microscopemanufacturerandmodel"] == "LifeCanvas SmartSPIM"
    assert drafts["run"]["formats"] == {"smartspim-metadata": 1, "png": 1}
    assert drafts["zeiss/brain07"]["instrument"]["microscopemanufacturerandmodel"] == "Zeiss Lightsheet Z.1"
    assert drafts["zeiss/brain07"]["formats"] == {"czi": 1, "png": 1}
    assert submission_draft([SMARTSPIM, other], cohort="run")["image"]["stepsizez"] == "2"


MESOSPIM = {"source": "cns/Tile0_Ch638.tiff_meta.txt", "sizeBytes": 1, "mtime": 0, "decoded": {"format": "mesospim-meta", "summary": "", "fields": {
    "pixelSizeUm": 0.85, "zStepUm": 3.0, "zPlanes": 1400, "imageWidth": 5056, "imageLength": 2960, "channels": 1, "zoom": "5x", "magnification": 5.0,
    "cameraType": "Photometrics", "lasers": ["638 nm"]}}}
LUXENDO = {"source": "muvi/uni_tp-0_ch-0.json", "sizeBytes": 1, "mtime": 0, "decoded": {"format": "luxendo-stack", "summary": "", "fields": {
    "scopeType": "MuVi-SPIM", "voxelSizeXUm": 0.195, "voxelSizeYUm": 0.195, "voxelSizeZUm": 1.0, "sizeX": 2048, "sizeY": 2048, "sizeZ": 181,
    "objectiveName": "Olympus XLUMPLFLN 20XW", "magnification": 20.0, "numericalAperture": 1.0, "objectiveImmersion": "water", "lasersOnNm": [488.0]}}}


def test_submission_draft_from_mesospim_and_luxendo_records() -> None:
    d = submission_draft([MESOSPIM])
    assert d["instrument"]["microscopemanufacturerandmodel"] == "mesoSPIM"
    assert d["instrument"]["objectivename"] == "5x" and d["instrument"]["illuminationwavelength"] == "638 nm"
    assert (d["image"]["stepsizex"], d["image"]["stepsizez"], d["image"]["slices"], d["image"]["xsize"]) == ("0.85", "3", "1400", "5056")
    d = submission_draft([LUXENDO])
    assert d["instrument"]["microscopemanufacturerandmodel"] == "Luxendo MuVi-SPIM"
    assert d["instrument"]["objectivena"] == "1" and d["instrument"]["objectiveimmersion"] == "water"
    assert d["image"]["stepsizex"] == "0.195" and d["image"]["zsize"] == "181" and d["image"]["channels"] == "1"


def test_submission_draft_with_nothing_useful_is_all_blank() -> None:
    draft = submission_draft([PLANE])
    assert all(v == "" for k, v in draft["image"].items() if k != "files")
    assert all(v == "" for v in draft["instrument"].values())


def test_live_record_matches_capture() -> None:
    live = bil.retrieve("ace-cup-eel")
    f_live = prior_records(live)[0]["decoded"]["fields"]
    f_cap = prior_records(DatasetDetail.from_json(ACE_CUP_EEL))[0]["decoded"]["fields"]
    for k in ("stepSizeXUm", "stepSizeYUm", "stepSizeZUm", "microscopeModel", "microscopeType", "detectorType"):
        assert f_live[k] == f_cap[k], k


def test_cli_subcommands(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    from scigantic_bil.cli import main

    index = tmp_path / "index.jsonl"
    index.write_text(json.dumps(SMARTSPIM) + "\n", encoding="utf-8")
    assert main(["submission-draft", str(index), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["run"]["instrument"]["objectivename"] == "LCT 3.6x"
    assert main(["edge-prior", "ace-cup-eel", str(tmp_path / "p.b64")]) == 0
    assert (tmp_path / "p.b64").stat().st_size > 100


# OME-Zarr stores and voxel manifests. voxel_run1_index.jsonl is the
# scigantic-edge decoder's own index.jsonl over a real headless run of the
# Allen Institute voxel engine's simulated instrument (2 XY tiles x 2
# single-channel profiles, 10 frames of 128 x 192 uint16, 2026-10-20): one
# voxel-manifest record and four ome-zarr records, each carrying the
# manifest's view of that store under fields.manifest. The exaSPIM records
# are hand-written to the same contract from two public tiles' metadata on
# s3://aind-open-data (Zarr v2 / NGFF 0.4 and Zarr v3 / NGFF 0.5, tczyx).
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "edge"
VOXEL_RUN1 = FIXTURES / "voxel_run1_index.jsonl"
EXASPIM_PUBLIC = FIXTURES / "exaspim_public_index.jsonl"


def _records(path: Path) -> list[dict]:  # type: ignore[type-arg]
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _voxel_parts() -> tuple[dict, list[dict]]:  # type: ignore[type-arg]
    recs = _records(VOXEL_RUN1)
    manifests = [r for r in recs if r["decoded"]["format"] == "voxel-manifest"]
    stores = [r for r in recs if r["decoded"]["format"] == "ome-zarr"]
    assert len(manifests) == 1 and len(stores) == 4
    return manifests[0], stores


def test_voxel_acquisition_is_one_submission() -> None:
    drafts = submission_drafts(VOXEL_RUN1)
    assert len(drafts) == 1
    (root,) = drafts
    assert root.endswith("/run1/store/run1")  # the directory holding manifest.json
    d = submission_draft(VOXEL_RUN1)
    img, inst, acq = d["image"], d["instrument"], d["acquisition"]
    assert (img["stepsizex"], img["stepsizey"], img["stepsizez"]) == ("0.47", "0.47", "1")
    assert img["channels"] == "2"  # gfp + rfp across volumes, not the sizeC=1 of one store
    assert (img["slices"], img["zsize"], img["ysize"], img["xsize"]) == ("10", "10", "128", "192")
    assert img["dimensionorder"] == "CZYX"
    assert acq["status"] == "completed" and acq["revision"] == 15
    assert acq["stores"] == 4 and acq["volumes"] == 4 and acq["volumesPlanned"] == 4 and acq["volumesCompleted"] == 4
    assert acq["channels"] == ["gfp", "rfp"] and acq["manifest"].endswith("/run1/store/run1/manifest.json")
    assert acq["startedAt"] < acq["endedAt"]
    # The template instrument is a simulator: no light-sheet claim, model as named,
    # no objective name/NA or camera model in the record, so those stay blank.
    assert inst["microscopetype"] == "" and inst["microscopemanufacturerandmodel"] == "sim"
    assert inst["objectivemagnification"] == "8" and inst["objectivena"] == "" and inst["objectivename"] == ""
    assert inst["illuminationwavelength"] == "488, 561 nm" and inst["illuminationtypes"] == "laser"
    assert inst["detectionwavelength"] == "510, 600 nm"
    assert inst["detectormodel"] == "" and inst["detectortype"] == ""
    assert d["formats"] == {"voxel-manifest": 1, "ome-zarr": 4}
    assert d["sources"][0].endswith("/manifest.json")
    assert {s.rsplit("/", 1)[-1] for s in d["sources"][1:]} == {"gfp.ome.zarr", "rfp.ome.zarr"}


def test_voxel_stores_group_under_their_manifest_by_each_rule() -> None:
    manifest, stores = _voxel_parts()
    root = manifest["source"].rsplit("/", 1)[0]
    # Rule 1: the sidecar ../../../../manifest.json resolved against the store lands on the
    # manifest record, also when the record is keyed on the store's zarr.json.
    keyed = json.loads(json.dumps(stores))
    for r in keyed:
        r["source"] += "/zarr.json"
    assert set(submission_drafts([manifest] + keyed)) == {root}
    assert submission_draft([manifest] + keyed)["image"]["channels"] == "2"
    # Rule 2: no sidecars; the manifest's storePaths (relative to its directory) name the stores.
    no_sidecar = json.loads(json.dumps(stores))
    for r in no_sidecar:
        r["decoded"]["fields"]["sidecars"] = []
    assert set(submission_drafts([manifest] + no_sidecar)) == {root}
    stale = json.loads(json.dumps(manifest))
    stale["decoded"]["fields"]["storePaths"] = []
    stale["source"] = root + "/elsewhere/manifest.json"
    # Neither rule 1 nor 2 pairs the stores with a manifest in another directory: rule 3 keeps
    # them together on their own, under the /volumes/ prefix, and the stray manifest is its own draft.
    assert set(submission_drafts([stale] + no_sidecar)) == {root, root + "/elsewhere"}
    # Rule 3 alone: no manifest record at all yet. Channel ids still come from each store's
    # own manifest view, the counts from the stores.
    only_stores = submission_drafts(no_sidecar)
    assert set(only_stores) == {root}
    d = only_stores[root]
    assert d["image"]["channels"] == "2" and d["image"]["slices"] == "10"
    assert d["acquisition"] == {"stores": 4, "volumes": 4, "channels": ["gfp", "rfp"], "storeStates": {"complete": 4}}
    assert d["instrument"]["microscopemanufacturerandmodel"] == ""
    # Without the per-store manifest view either, the <channel>.ome.zarr names and volume
    # directories carry the same counts.
    bare = json.loads(json.dumps(no_sidecar))
    for r in bare:
        r["decoded"]["fields"].pop("manifest", None)
    assert submission_draft(bare)["acquisition"] == {"stores": 4, "volumes": 4, "channels": ["gfp", "rfp"], "storeStates": {"complete": 4}}
    # A voxel run under a different root is a different draft, not a blend.
    other = json.loads(json.dumps(no_sidecar[:1]))
    other[0]["source"] = other[0]["source"].replace("/run1/volumes/", "/run2/volumes/")
    assert len(submission_drafts(no_sidecar + other)) == 2


def test_voxel_manifest_alone_seeds_the_draft_before_stores_arrive() -> None:
    manifest, _ = _voxel_parts()
    d = submission_draft([manifest])
    assert d["image"]["channels"] == "2" and d["image"]["stepsizex"] == "" and d["image"]["slices"] == ""
    assert d["acquisition"]["status"] == "completed" and d["acquisition"]["stores"] == 0
    assert "volumes" not in d["acquisition"] and d["acquisition"]["channels"] == ["gfp", "rfp"]
    assert d["instrument"]["illuminationwavelength"] == "488, 561 nm"


def test_voxel_manifest_light_sheet_only_when_named() -> None:
    manifest = json.loads(json.dumps(_voxel_parts()[0]))
    manifest["decoded"]["fields"]["instrumentName"] = "exaSPIM-1"
    inst = submission_draft([manifest])["instrument"]
    assert inst["microscopetype"] == "Light-sheet" and inst["microscopemanufacturerandmodel"] == "exaSPIM-1"
    manifest["decoded"]["fields"]["instrumentName"] = "confocal-2"
    assert submission_draft([manifest])["instrument"]["microscopetype"] == ""


def test_public_exaspim_stores_map_without_a_manifest() -> None:
    drafts = submission_drafts(EXASPIM_PUBLIC)
    assert set(drafts) == {"exaSPIM_851474_2026-07-10_15-23-22/SPIM.ome.zarr", "exaSPIM_2026-08-04_10-27-41/SPIM"}
    v2 = drafts["exaSPIM_851474_2026-07-10_15-23-22/SPIM.ome.zarr"]
    img = v2["image"]
    assert (img["stepsizex"], img["stepsizey"], img["stepsizez"]) == ("5.984", "5.984", "8")
    assert (img["channels"], img["slices"], img["ysize"], img["xsize"]) == ("1", "2560", "1330", "1774")
    assert img["dimensionorder"] == "TCZYX"
    assert all(v == "" for v in v2["instrument"].values())
    assert v2["acquisition"] == {"stores": 1, "volumes": 1, "channels": ["488"], "storeStates": {"unknown": 1}}
    assert v2["formats"] == {"ome-zarr": 1}
    v3 = drafts["exaSPIM_2026-08-04_10-27-41/SPIM"]
    assert (v3["image"]["stepsizex"], v3["image"]["stepsizez"], v3["image"]["slices"]) == ("15.04", "20", "1024")
    assert v3["sources"] == ["exaSPIM_2026-08-04_10-27-41/SPIM/tile_000000_ch_488.ome.zarr/zarr.json"]


def test_ome_zarr_voxel_size_units_reach_micrometres() -> None:
    from scigantic_bil.edge import _um

    assert _um(0.47, "micrometer") == 0.47 and _um(0.47, "um") == 0.47
    assert _um(0.47, "\u00b5m") == 0.47 and _um(0.47, "\u03bcm") == 0.47  # micro sign, Greek mu
    assert _um(0.47, "microns") == 0.47 and _um(0.47, "micrometres") == 0.47
    assert _um(470, "nanometer") == pytest.approx(0.47) and _um(470, "nm") == pytest.approx(0.47)
    assert _um(0.001, "millimeter") == pytest.approx(1.0) and _um(0.001, "mm") == pytest.approx(1.0)
    assert _um(2, None) == 2.0 and _um(None, "micrometer") is None
    rec = json.loads(json.dumps(_records(EXASPIM_PUBLIC)[0]))
    f = rec["decoded"]["fields"]
    f["voxelSizeUnit"], f["voxelSizeX"], f["voxelSizeY"], f["voxelSizeZ"] = "nanometer", 5984, 5984, 8000
    img = submission_draft([rec])["image"]
    assert (img["stepsizex"], img["stepsizez"]) == ("5.984", "8")


def test_draft_without_stores_has_an_empty_acquisition_block() -> None:
    assert submission_draft([SMARTSPIM])["acquisition"] == {}


# --- found by stress-testing the bridge against the edge binary's own output ---

def _store(source: str, **fields: Any) -> dict[str, Any]:
    base = {"layout": "multiscales", "voxelSizeUnit": "micrometer", "state": "complete", "axes": [
        {"name": n} for n in "czyx"]}
    base.update(fields)
    return {"source": source, "sizeBytes": 1, "mtime": 0, "decoded": {"format": "ome-zarr", "summary": "", "fields": base}}


def test_watch_mode_rerecords_keep_only_the_latest_line(tmp_path: Path) -> None:
    """index.jsonl is an append log; a store re-recorded as its shards land
    must count once and draft from its last state, not its first."""
    early = _store("acq/volumes/000001/p/gfp.ome.zarr", state="partial", sizeZ=4, sizeY=8, sizeX=8, voxelSizeX=1, voxelSizeY=1, voxelSizeZ=2)
    late = _store("acq/volumes/000001/p/gfp.ome.zarr", state="complete", sizeZ=40, sizeY=8, sizeX=8, voxelSizeX=1, voxelSizeY=1, voxelSizeZ=2)
    index = tmp_path / "index.jsonl"
    index.write_text("\n".join(json.dumps(r) for r in [early, early, late]) + "\n", encoding="utf-8")
    d = submission_draft(index)
    assert d["acquisition"]["stores"] == 1 and d["acquisition"]["storeStates"] == {"complete": 1}
    assert d["image"]["slices"] == "40"
    assert submission_draft([early, late])["image"]["slices"] == "40"


def test_container_group_joins_the_draft_of_its_tiles() -> None:
    """AIND's SPIM.ome.zarr is recorded as a group and entered; it must not
    split the acquisition into an empty draft of its own."""
    group = _store("ds/SPIM.ome.zarr", layout="group", state="no-array", axes=[])
    tile = _store("ds/SPIM.ome.zarr/tile_000000_ch_488.zarr", sizeZ=10, sizeY=20, sizeX=30, voxelSizeX=1, voxelSizeY=1, voxelSizeZ=1)
    drafts = submission_drafts([group, tile])
    assert list(drafts) == ["ds/SPIM.ome.zarr"]
    assert drafts["ds/SPIM.ome.zarr"]["acquisition"]["stores"] == 1


def test_channels_at_different_resolutions_are_not_blended() -> None:
    """The 2026-08-04 public exaSPIM acquisition stores 561 nm at half the
    488 nm resolution; the Image row must describe one real store and the
    disagreement must be reported, not averaged into a row that is neither."""
    fine = _store("a/SPIM/tile_000000_ch_488.ome.zarr", sizeZ=1024, sizeY=2660, sizeX=3548, voxelSizeX=15.04, voxelSizeY=15.04, voxelSizeZ=20)
    coarse = _store("a/SPIM/tile_000000_ch_561.ome.zarr", sizeZ=512, sizeY=1330, sizeX=1774, voxelSizeX=30.08, voxelSizeY=30.08, voxelSizeZ=40)
    shapeless = _store("a/SPIM/tile_000001_ch_561.ome.zarr", state="no-array", voxelSizeX=30.08, voxelSizeY=30.08, voxelSizeZ=40)
    for order in ([coarse, shapeless, fine], [shapeless, fine, coarse]):
        d = submission_draft(order)
        img = d["image"]
        assert (img["stepsizex"], img["stepsizez"], img["xsize"], img["slices"]) == ("15.04", "20", "3548", "1024")
        assert d["acquisition"]["mismatch"]["voxelSizeX"] == [15.04, 30.08]
        assert d["acquisition"]["mismatch"]["sizeZ"] == [512, 1024]
        assert img["channels"] == "2" and d["acquisition"]["channels"] == ["488", "561"]
        assert d["acquisition"]["volumes"] == 2


def test_aind_tile_names_count_tiles_and_channels() -> None:
    stores = [_store(f"r/SPIM.ome.zarr/tile_x_000{x}_y_0000_z_0000_ch_{ch}.zarr", sizeZ=5, sizeY=5, sizeX=5,
                     voxelSizeX=0.748, voxelSizeY=0.748, voxelSizeZ=1) for x in range(3) for ch in ("488", "638")]
    d = submission_draft(stores)
    assert d["acquisition"]["volumes"] == 3 and d["acquisition"]["channels"] == ["488", "638"]
    assert d["image"]["channels"] == "2"


def test_ome_zarr_scale_without_a_unit_is_not_called_micrometres() -> None:
    d = submission_draft([_store("x/a.zarr", voxelSizeUnit=None, voxelSizeX=0.5, voxelSizeY=0.5, voxelSizeZ=2, sizeX=4, sizeY=4, sizeZ=4)])
    assert d["image"]["stepsizex"] == "" and d["image"]["xsize"] == "4"
    d = submission_draft([_store("x/a.zarr", voxelSizeUnit="pixel", voxelSizeX=0.5, voxelSizeY=0.5, voxelSizeZ=2, sizeX=4, sizeY=4, sizeZ=4)])
    assert d["image"]["stepsizex"] == ""
    d = submission_draft([_store("x/a.zarr", voxelSizeUnit="nanometer", voxelSizeX=500, voxelSizeY=500, voxelSizeZ=2000, sizeX=4, sizeY=4, sizeZ=4)])
    assert (d["image"]["stepsizex"], d["image"]["stepsizez"]) == ("0.5", "2")


def test_malformed_records_are_skipped_or_read_as_blank() -> None:
    """A torn or hand-edited index must never crash a draft: fields that
    are not objects, lists that are scalars, NaN, infinity and huge ints."""
    bad: list[Any] = [
        "not a record", 7, None, {"source": "a/x.zarr"}, {"source": "a/y.zarr", "decoded": []},
        {"source": "a/z.zarr", "decoded": {"format": "ome-zarr", "fields": [1, 2]}},
        {"source": 5, "decoded": {"format": None, "fields": None}},
        {"source": "a/manifest.json", "decoded": {"format": "voxel-manifest", "fields": {
            "storePaths": True, "channelIds": 3, "laserWavelengthsNm": 488, "volumeCount": float("inf")}}},
        _store("a/volumes/000001/p/g.ome.zarr", sizeX=float("inf"), sizeY=float("nan"), sizeZ=10**40,
               voxelSizeX="NaN", voxelSizeY=None, voxelSizeZ=[], axes=5, sidecars=1, manifest=[]),
    ]
    drafts = submission_drafts(bad)
    json.dumps(drafts)
    img = drafts["a"]["image"]
    assert img["xsize"] == "" and img["ysize"] == "" and img["stepsizex"] == ""
