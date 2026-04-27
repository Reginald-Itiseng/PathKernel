from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.project import Layer, Project
from app.core.surfacing import SurfacingParams, build_surfacing_toolpath_layer


def _source_layer() -> Layer:
    return Layer(
        name="surface_ref",
        path=Path("surface_ref.gbr"),
        kind="gerber",
        source=SimpleNamespace(units="mm", primitives=[]),
        role="artwork",
    )


def test_surfacing_generates_zigzag_rows_for_bbox() -> None:
    layer = build_surfacing_toolpath_layer(
        _source_layer(),
        Project(),
        SurfacingParams(
            tool_diameter_mm=2.0,
            bounds_min_x_mm=0.0,
            bounds_max_x_mm=10.0,
            bounds_min_y_mm=0.0,
            bounds_max_y_mm=5.0,
            stepover_mm=2.0,
            sweep_axis="x",
        ),
    )

    lines = list(layer.source.primitives)
    assert len(lines) == 4
    assert (float(lines[0].start[0]), float(lines[0].start[1]), float(lines[0].end[0]), float(lines[0].end[1])) == (
        0.0,
        0.0,
        10.0,
        0.0,
    )
    assert (float(lines[1].start[0]), float(lines[1].start[1]), float(lines[1].end[0]), float(lines[1].end[1])) == (
        10.0,
        2.0,
        0.0,
        2.0,
    )
    assert layer.metadata.get("kind") == "surfacing_toolpath"
    assert layer.metadata.get("surfacing_pass_count") == "4"
    assert layer.bbox == (0.0, 0.0, 10.0, 5.0)


def test_surfacing_auto_stepover_from_tool_pct() -> None:
    layer = build_surfacing_toolpath_layer(
        _source_layer(),
        Project(),
        SurfacingParams(
            tool_diameter_mm=4.0,
            bounds_min_x_mm=0.0,
            bounds_max_x_mm=10.0,
            bounds_min_y_mm=0.0,
            bounds_max_y_mm=8.0,
            stepover_mm=0.0,
            stepover_pct=50.0,
            sweep_axis="y",
        ),
    )

    lines = list(layer.source.primitives)
    assert len(lines) == 6
    assert (float(lines[0].start[0]), float(lines[0].start[1]), float(lines[0].end[0]), float(lines[0].end[1])) == (
        0.0,
        0.0,
        0.0,
        8.0,
    )
    assert layer.metadata.get("surfacing_stepover_mm") == "2.000000"


def test_surfacing_rejects_invalid_area_or_margin() -> None:
    with pytest.raises(ValueError, match="Bounding box"):
        build_surfacing_toolpath_layer(
            _source_layer(),
            Project(),
            SurfacingParams(
                tool_diameter_mm=2.0,
                bounds_min_x_mm=5.0,
                bounds_max_x_mm=5.0,
                bounds_min_y_mm=0.0,
                bounds_max_y_mm=10.0,
                stepover_mm=1.0,
            ),
        )

    with pytest.raises(ValueError, match="Margin"):
        build_surfacing_toolpath_layer(
            _source_layer(),
            Project(),
            SurfacingParams(
                tool_diameter_mm=2.0,
                bounds_min_x_mm=0.0,
                bounds_max_x_mm=10.0,
                bounds_min_y_mm=0.0,
                bounds_max_y_mm=10.0,
                stepover_mm=1.0,
                margin_mm=5.1,
            ),
        )
