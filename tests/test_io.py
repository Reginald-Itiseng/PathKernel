from __future__ import annotations

from pathlib import Path

from app.core.io import detect_kind, scan_folder


def test_detect_kind() -> None:
    assert detect_kind(Path("board.gtl")) == "gerber"
    assert detect_kind(Path("board.GBL")) == "gerber"
    assert detect_kind(Path("holes.drl")) == "excellon"
    assert detect_kind(Path("notes.txt")) == "excellon"
    assert detect_kind(Path("readme.md")) == "unknown"


def test_scan_folder_filters_known_extensions(tmp_path: Path) -> None:
    (tmp_path / "a.gtl").write_text("x", encoding="utf-8")
    (tmp_path / "b.drl").write_text("x", encoding="utf-8")
    (tmp_path / "c.md").write_text("x", encoding="utf-8")

    found = scan_folder(tmp_path)
    assert [p.name for p in found] == ["a.gtl", "b.drl"]

