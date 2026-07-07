from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from shapely.geometry import LineString
from shapely.ops import unary_union

from app.core.background_tasks import _run_task, hatching_params_payload
from app.core.hatching import HatchingParams, build_hatching_toolpath_layer
from app.core.io import AMGroup, Circle, Line, Polygon as OutlinePolygon, RoundRectangle
from app.core.isolation import _build_copper_geometry
from app.core.project import Layer, Project
from app.core.project_store import deserialize_layer, serialize_layer


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


def _trace_layer(name: str, x_offset: float) -> Layer:
    return Layer(
        name=name,
        path=Path(f"{name}.gbr"),
        kind="gerber",
        source=SimpleNamespace(
            units="mm",
            primitives=[
                Line(
                    start=(x_offset + 2.0, 5.0),
                    end=(x_offset + 8.0, 5.0),
                    diameter=0.5,
                    level_polarity="dark",
                ),
            ],
            bounds=((x_offset + 2.0, x_offset + 8.0), (4.75, 5.25)),
        ),
        role="top",
        bbox=(x_offset + 2.0, 4.75, x_offset + 8.0, 5.25),
    )


def _cutout_layer(name: str, x_offset: float) -> Layer:
    return Layer(
        name=name,
        path=Path(f"{name}.gko"),
        kind="gerber",
        source=SimpleNamespace(
            units="mm",
            primitives=[
                OutlinePolygon(
                    vertices=[
                        (x_offset + 0.0, 0.0),
                        (x_offset + 10.0, 0.0),
                        (x_offset + 10.0, 10.0),
                        (x_offset + 0.0, 10.0),
                    ],
                )
            ],
        ),
        role="cutout",
        bbox=(x_offset + 0.0, 0.0, x_offset + 10.0, 10.0),
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


def test_hatching_swept_toolpath_does_not_overlap_copper_trace() -> None:
    source = Layer(
        name="trace_copper",
        path=Path("trace.gbr"),
        kind="gerber",
        source=SimpleNamespace(
            units="mm",
            primitives=[
                Line(
                    start=(2.0, 5.0),
                    end=(8.0, 5.0),
                    diameter=0.5,
                    level_polarity="dark",
                ),
            ],
            bounds=((2.0, 8.0), (4.75, 5.25)),
        ),
        role="top",
        bbox=(2.0, 4.75, 8.0, 5.25),
    )
    hatch = build_hatching_toolpath_layer(
        source,
        Project(),
        HatchingParams(
            tool_diameter_mm=0.5,
            hatching_margin_mm=0.4,
            boundary_margin_mm=2.0,
        ),
    )

    swept = unary_union(
        [
            LineString([primitive.start, primitive.end]).buffer(0.25)
            for primitive in hatch.source.primitives
        ]
    )
    copper = _build_copper_geometry(source)

    assert copper is not None
    assert swept.intersection(copper).area <= 1e-9


def test_hatching_board_domain_ignores_unselected_cutout_board() -> None:
    selected = _trace_layer("selected_top", 0.0)
    hatch = build_hatching_toolpath_layer(
        selected,
        Project(),
        HatchingParams(
            tool_diameter_mm=0.5,
            hatching_margin_mm=0.5,
            copper_keepout_margin_mm=0.0,
        ),
        board_layers=[
            _cutout_layer("selected_edge", 0.0),
            _cutout_layer("unselected_edge", 20.0),
        ],
    )

    assert hatch.bbox is not None
    assert hatch.bbox[2] <= 10.0


def test_multi_hatching_avoids_all_selected_copper_traces() -> None:
    left = _trace_layer("left_top", 0.0)
    right = _trace_layer("right_top", 20.0)
    result = _run_task(
        "hatching_generate_multi",
        {
            "source_layers": [serialize_layer(left), serialize_layer(right)],
            "params": hatching_params_payload(
                HatchingParams(
                    tool_diameter_mm=0.5,
                    hatching_margin_mm=0.5,
                    copper_keepout_margin_mm=0.0,
                )
            ),
            "board_layers": [
                serialize_layer(_cutout_layer("left_edge", 0.0)),
                serialize_layer(_cutout_layer("right_edge", 20.0)),
            ],
            "copper_keepout_layers": [serialize_layer(left), serialize_layer(right)],
        },
        lambda _text: None,
    )
    hatch = deserialize_layer(dict(result["generated_layer"]))
    swept = unary_union(
        [
            LineString([primitive.start, primitive.end]).buffer(0.25)
            for primitive in hatch.source.primitives
        ]
    )
    copper = unary_union([_build_copper_geometry(left), _build_copper_geometry(right)])

    assert hatch.metadata.get("source_layer_count") == "2"
    assert hatch.bbox is not None
    assert hatch.bbox[0] >= 0.0
    assert hatch.bbox[2] <= 30.0
    assert swept.intersection(copper).area <= 1e-9


def _amgroup_copper_layer() -> Layer:
    """Layer with an AMGroup pad (aperture-macro flash, common from Altium/PADS/OrCAD)."""
    return Layer(
        name="am_copper",
        path=Path("am_top.gbr"),
        kind="gerber",
        source=SimpleNamespace(
            units="mm",
            primitives=[
                AMGroup(
                    primitives=[
                        RoundRectangle(
                            position=(5.0, 5.0),
                            width=2.0,
                            height=1.0,
                            radius=0.25,
                            level_polarity="dark",
                        ),
                    ],
                    level_polarity="dark",
                    flashed=True,
                    group_id="am:test",
                ),
            ],
            bounds=((4.0, 6.0), (4.5, 5.5)),
        ),
        role="top",
        bbox=(4.0, 4.5, 6.0, 5.5),
    )


def test_amgroup_pad_included_in_copper_geometry() -> None:
    """AMGroup primitives must not be silently dropped by _build_copper_geometry."""
    copper = _build_copper_geometry(_amgroup_copper_layer())

    assert copper is not None, "AMGroup pad produced no copper geometry"
    assert not copper.is_empty, "AMGroup pad copper geometry is empty"
    # The pad is 2×1 mm so the area must be meaningfully positive.
    assert copper.area > 0.5


def test_amgroup_pad_generates_isolation_keepout() -> None:
    """Isolation toolpath must respect an AMGroup pad — hatch must stay clear of it."""
    from app.core.hatching import HatchingParams, build_hatching_toolpath_layer

    source = _amgroup_copper_layer()
    hatch = build_hatching_toolpath_layer(
        source,
        Project(),
        HatchingParams(
            tool_diameter_mm=0.3,
            hatching_margin_mm=0.3,
            boundary_margin_mm=3.0,
        ),
    )

    swept = unary_union(
        [
            LineString([p.start, p.end]).buffer(0.15)
            for p in hatch.source.primitives
        ]
    )
    copper = _build_copper_geometry(source)

    assert copper is not None
    assert swept.intersection(copper).area <= 1e-9, (
        "Hatch toolpath overlaps AMGroup pad — pad was not included in keepout"
    )
