from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.cutout import CutoutParams, build_cutout_toolpath_layer
from app.core.background_tasks import _run_task, cutout_params_payload
from app.core.io import Polygon as OutlinePolygon
from app.core.project import Layer, Project
from app.core.project_store import deserialize_layer, serialize_layer


def _square_cutout_layer() -> Layer:
    return _named_square_cutout_layer("board_edge", 0.0)


def _named_square_cutout_layer(name: str, x_offset: float) -> Layer:
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
                ),
            ],
        ),
        role="cutout",
    )


def _toolpath_length(layer: Layer) -> float:
    total = 0.0
    for primitive in list(layer.source.primitives):
        sx, sy = primitive.start
        ex, ey = primitive.end
        total += ((ex - sx) ** 2 + (ey - sy) ** 2) ** 0.5
    return total


def test_cutout_holding_breaks_leave_uncut_gaps() -> None:
    layer = build_cutout_toolpath_layer(
        _square_cutout_layer(),
        Project(),
        CutoutParams(
            tool_diameter_mm=1.0,
            compensation="onpath",
            holding_tab_count=4,
            holding_tab_width_mm=1.0,
        ),
    )

    assert layer.metadata.get("holding_tab_count") == "4"
    assert layer.metadata.get("holding_tab_width_mm") == "1.000000"
    assert _toolpath_length(layer) == pytest.approx(36.0)


def test_cutout_without_holding_breaks_keeps_full_loop() -> None:
    layer = build_cutout_toolpath_layer(
        _square_cutout_layer(),
        Project(),
        CutoutParams(tool_diameter_mm=1.0, compensation="onpath"),
    )

    assert layer.metadata.get("holding_tab_count") == "0"
    assert _toolpath_length(layer) == pytest.approx(40.0)


def test_multi_cutout_worker_merges_selected_layers_into_one_geometry_layer() -> None:
    result = _run_task(
        "cutout_generate_multi",
        {
            "source_layers": [
                serialize_layer(_named_square_cutout_layer("edge_a", 0.0)),
                serialize_layer(_named_square_cutout_layer("edge_b", 20.0)),
            ],
            "params": cutout_params_payload(CutoutParams(tool_diameter_mm=1.0, compensation="onpath")),
            "loop_compensations_by_layer": [["onpath"], ["onpath"]],
        },
        lambda _text: None,
    )
    layer = deserialize_layer(dict(result["generated_layer"]))

    assert layer.metadata.get("kind") == "cutout_toolpath"
    assert layer.metadata.get("source_layer_count") == "2"
    assert layer.metadata.get("derived_from") == "edge_a,edge_b"
    assert layer.kind == "geometry"
    assert _toolpath_length(layer) == pytest.approx(80.0)
