from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.core.hatching import HatchingParams, build_hatching_toolpath_layer
from app.core.io import Circle
from app.core.project import Layer, Project


def _copper_layer() -> Layer:
    return Layer(
        name="top_copper",
        path=Path("top.gbr"),
        kind="gerber",
        source=SimpleNamespace(
            units="mm",
            primitives=[
                Circle(
                    position=(5.0, 5.0),
                    diameter=2.0,
                    level_polarity="dark",
                ),
            ],
            bounds=((4.0, 6.0), (4.0, 6.0)),
        ),
        role="top",
        bbox=(4.0, 4.0, 6.0, 6.0),
    )


def test_hatching_generates_copper_clearing_toolpath() -> None:
    layer = build_hatching_toolpath_layer(
        _copper_layer(),
        Project(),
        HatchingParams(
            tool_diameter_mm=0.5,
            hatching_margin_mm=0.5,
            boundary_margin_mm=3.0,
        ),
    )

    assert layer.metadata.get("kind") == "hatching_toolpath"
    assert layer.metadata.get("derived_from") == "top_copper"
    assert layer.metadata.get("hatching_step_mm") == "0.500000"
    assert len(list(layer.source.primitives)) > 0
    assert layer.bbox is not None


def test_hatching_uses_own_toolpath_kind_not_surfacing() -> None:
    layer = build_hatching_toolpath_layer(
        _copper_layer(),
        Project(),
        HatchingParams(
            tool_diameter_mm=0.5,
            hatching_margin_mm=0.5,
            boundary_margin_mm=3.0,
            hatch_angle_deg=45.0,
        ),
    )

    assert layer.name.endswith("_hatch_tp")
    assert layer.metadata.get("kind") != "surfacing_toolpath"
    assert "surfacing_stepover_mm" not in layer.metadata
