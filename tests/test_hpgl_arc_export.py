from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.core.hpgl import HPGLExportOptions, export_layers_to_hpgl
from app.core.io import Arc
from app.core.project import Layer


def _arc_layer(*, direction: str = "counterclockwise", mirror_x: bool = False) -> Layer:
    return Layer(
        name="arc_layer",
        path=Path("arc_layer.gbr"),
        kind="geometry",
        role="cutout",
        mirror_x=mirror_x,
        source=SimpleNamespace(
            units="mm",
            primitives=[
                Arc(
                    start=(1.0, 0.0),
                    end=(-1.0, 0.0),
                    center=(0.0, 0.0),
                    direction=direction,
                    diameter=0.2,
                ),
            ],
        ),
        metadata={"kind": "toolpath"},
    )


def test_hpgl_export_preserves_arc_as_aa() -> None:
    result = export_layers_to_hpgl(
        [_arc_layer(direction="counterclockwise", mirror_x=False)],
        options=HPGLExportOptions(units_per_mm=40.0, normalize_to_origin=False),
    )

    assert "AA0,0,180;" in result.hpgl_text


def test_hpgl_export_flips_arc_direction_for_mirror_x() -> None:
    result = export_layers_to_hpgl(
        [_arc_layer(direction="counterclockwise", mirror_x=True)],
        options=HPGLExportOptions(units_per_mm=40.0, normalize_to_origin=False),
    )

    assert "AA0,0,-180;" in result.hpgl_text
