from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.centering_holes import (
    CenteringHolesParams,
    build_centering_holes_toolpath_layer,
    centering_hole_mirror_axis,
)
from app.core.project import Layer, Project


def _source_layer() -> Layer:
    return Layer(
        name="board_ref",
        path=Path("board_ref.gbr"),
        kind="gerber",
        source=SimpleNamespace(units="mm", primitives=[]),
        role="top",
    )


def test_centering_horizontal_positions() -> None:
    layer = build_centering_holes_toolpath_layer(
        _source_layer(),
        Project(),
        CenteringHolesParams(
            board_min_x_mm=0.0,
            board_max_x_mm=100.0,
            board_min_y_mm=0.0,
            board_max_y_mm=50.0,
            orientation="horizontal",
            outline_to_hole_center_mm=10.0,
            hole_diameter_mm=1.0,
            tool_diameter_mm=1.0,
        ),
    )

    assert layer.metadata.get("kind") == "centering_holes_toolpath"
    assert layer.metadata.get("centering_hole_count") == "2"
    lines = list(layer.source.primitives)
    assert len(lines) == 2
    assert float(lines[0].start[0]) == pytest.approx(-10.0)
    assert float(lines[0].start[1]) == pytest.approx(25.0)
    assert float(lines[1].start[0]) == pytest.approx(110.0)
    assert float(lines[1].start[1]) == pytest.approx(25.0)
    assert centering_hole_mirror_axis(layer) == ("mirror_y", pytest.approx(25.0))


def test_centering_vertical_boring_passes_when_hole_larger_than_tool() -> None:
    layer = build_centering_holes_toolpath_layer(
        _source_layer(),
        Project(),
        CenteringHolesParams(
            board_min_x_mm=0.0,
            board_max_x_mm=100.0,
            board_min_y_mm=0.0,
            board_max_y_mm=50.0,
            orientation="vertical",
            outline_to_hole_center_mm=5.0,
            hole_diameter_mm=2.0,
            tool_diameter_mm=1.0,
            lateral_stepover_pct=50.0,
        ),
    )

    # 2 plunge lines + (2 passes * 2 holes) arcs
    assert len(list(layer.source.primitives)) == 6
    assert layer.metadata.get("centering_bore_hole_count") == "2"
    assert layer.metadata.get("centering_bore_pass_count") == "4"
    assert centering_hole_mirror_axis(layer) == ("mirror_x", pytest.approx(50.0))


def test_centering_rejects_invalid_bounds() -> None:
    with pytest.raises(ValueError, match="bounding box"):
        build_centering_holes_toolpath_layer(
            _source_layer(),
            Project(),
            CenteringHolesParams(
                board_min_x_mm=10.0,
                board_max_x_mm=10.0,
                board_min_y_mm=0.0,
                board_max_y_mm=50.0,
            ),
        )
