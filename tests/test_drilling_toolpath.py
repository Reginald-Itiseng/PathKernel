from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.drilling import DrillToolpathParams, build_drill_toolpath_layer
from app.core.io import Drill
from app.core.background_tasks import _run_task, drill_params_payload
from app.core.project import Layer, Project
from app.core.project_store import deserialize_layer, serialize_layer


def _source_layer() -> Layer:
    return Layer(
        name="board_drills",
        path=Path("board.drl"),
        kind="excellon",
        source=SimpleNamespace(
            units="mm",
            primitives=[
                Drill(position=(0.0, 0.0), diameter=0.8),
                Drill(position=(5.0, 0.0), diameter=1.6),
            ],
        ),
        role="drills",
    )


def _single_drill_source(name: str, x_mm: float) -> Layer:
    return Layer(
        name=name,
        path=Path(f"{name}.drl"),
        kind="excellon",
        source=SimpleNamespace(
            units="mm",
            primitives=[Drill(position=(x_mm, 0.0), diameter=1.0)],
        ),
        role="drills",
    )


def test_strategy_a_skips_holes_smaller_than_tool_by_default() -> None:
    layer = build_drill_toolpath_layer(
        _source_layer(),
        Project(),
        DrillToolpathParams(tool_diameter_mm=1.0, lateral_stepover_pct=50.0),
    )

    assert layer.metadata.get("drill_skipped_small_holes") == "1"
    assert layer.metadata.get("drill_oversize_small_holes") == "0"
    assert layer.metadata.get("drill_plunge_count") == "1"
    assert len(list(layer.source.primitives)) == 3


def test_strategy_a_can_allow_tool_larger_than_hole() -> None:
    layer = build_drill_toolpath_layer(
        _source_layer(),
        Project(),
        DrillToolpathParams(
            tool_diameter_mm=1.0,
            lateral_stepover_pct=50.0,
            allow_oversize_tool_for_small_holes=True,
        ),
    )

    assert layer.metadata.get("drill_skipped_small_holes") == "0"
    assert layer.metadata.get("drill_oversize_small_holes") == "1"
    assert layer.metadata.get("drill_plunge_count") == "2"
    assert len(list(layer.source.primitives)) == 5
    assert layer.bbox == pytest.approx((-0.5, -0.8, 5.8, 0.8))


def test_multi_drill_worker_merges_selected_layers_into_one_geometry_layer() -> None:
    result = _run_task(
        "drill_generate_multi",
        {
            "source_layers": [
                serialize_layer(_single_drill_source("drills_a", 0.0)),
                serialize_layer(_single_drill_source("drills_b", 5.0)),
            ],
            "params": drill_params_payload(DrillToolpathParams(tool_diameter_mm=1.0)),
        },
        lambda _text: None,
    )
    layer = deserialize_layer(dict(result["generated_layer"]))

    assert layer.metadata.get("kind") == "drill_toolpath"
    assert layer.metadata.get("source_layer_count") == "2"
    assert layer.metadata.get("derived_from") == "drills_a,drills_b"
    assert layer.kind == "geometry"
    assert len(list(layer.source.primitives)) == 4
