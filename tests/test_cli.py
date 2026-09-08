from __future__ import annotations

import json

import pytest

from scigantic_bil.cli import main


def test_summary_and_search(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["summary"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["datasets"] >= 14_000
    assert main(["search", "iDISCO", "--limit", "3"]) == 0
    text = capsys.readouterr().out
    assert len(text.strip().splitlines()) == 3


def test_light_sheet_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["light-sheet", "--json", "--limit", "2"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2 and {"bildid", "bildirectory"} <= set(rows[0])


def test_info_files_thumbnail(tmp_path: object, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["info", "ace-cup-eel"]) == 0
    assert "CC-BY-SA-4.0" in capsys.readouterr().out
    assert main(["files", "ace-cup-eel", "--zarr"]) == 0
    assert "zarr store:" in capsys.readouterr().out
    out = f"{tmp_path}/th.png"
    assert main(["thumbnail", "ace-bin-run", out, "--size", "64"]) == 0
    from PIL import Image

    assert max(Image.open(out).size) <= 64
