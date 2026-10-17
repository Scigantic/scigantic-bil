"""The scigantic-edge bridge. The capsule and draft shapes are checked
offline against a record captured from the metadata API on 2026-10-17
(ace-cup-eel, a Zeiss Z.1 light-sheet deposit) and against edge context
records in the shapes scigantic-edge's decoders write; one test fetches the
same record live to confirm the capture has not drifted."""

from __future__ import annotations

import base64
import json
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
    index.write_text("\n".join(json.dumps(r) for r in [PLANE, PLANE, SMARTSPIM, PLANE]) + '\n{"torn', encoding="utf-8")
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
