from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import math

from app.core.io import Line
from app.core.project import Layer, Project


@dataclass(slots=True)
class SurfacingParams:
    tool_diameter_mm: float
    bounds_min_x_mm: float
    bounds_max_x_mm: float
    bounds_min_y_mm: float
    bounds_max_y_mm: float
    stepover_mm: float = 0.0
    stepover_pct: float = 70.0
    margin_mm: float = 0.0
    sweep_axis: str = "x"  # "x" => rows along X, step in Y


@dataclass(slots=True)
class _DerivedSource:
    units: str
    primitives: list[Any]
    bounds: tuple[tuple[float, float], tuple[float, float]] | None = None


def build_surfacing_toolpath_layer(
    source_layer: Layer,
    project: Project,  # noqa: ARG001
    params: SurfacingParams,
    log: callable | None = None,
) -> Layer:
    tool_dia = max(0.001, float(params.tool_diameter_mm))
    margin = max(0.0, float(params.margin_mm))
    min_x = min(float(params.bounds_min_x_mm), float(params.bounds_max_x_mm))
    max_x = max(float(params.bounds_min_x_mm), float(params.bounds_max_x_mm))
    min_y = min(float(params.bounds_min_y_mm), float(params.bounds_max_y_mm))
    max_y = max(float(params.bounds_min_y_mm), float(params.bounds_max_y_mm))

    if (max_x - min_x) <= 1e-9 or (max_y - min_y) <= 1e-9:
        raise ValueError("Surfacing area is invalid. Bounding box width/height must be greater than zero.")

    if margin > 0.0:
        min_x += margin
        max_x -= margin
        min_y += margin
        max_y -= margin
        if (max_x - min_x) <= 1e-9 or (max_y - min_y) <= 1e-9:
            raise ValueError("Margin is too large for the selected surfacing area.")

    stepover = _resolve_stepover_mm(tool_dia, params.stepover_mm, params.stepover_pct)
    sweep_axis = _normalize_sweep_axis(params.sweep_axis)
    _log(
        log,
        (
            "surfacing: "
            f"bbox=({min_x:.4f},{min_y:.4f})..({max_x:.4f},{max_y:.4f}), "
            f"tool={tool_dia:.4f}mm, stepover={stepover:.4f}mm, axis={sweep_axis}"
        ),
    )

    lines = _raster_lines(
        min_x=min_x,
        max_x=max_x,
        min_y=min_y,
        max_y=max_y,
        stepover_mm=stepover,
        sweep_axis=sweep_axis,
    )
    if not lines:
        raise ValueError("Surfacing toolpath generation produced no paths.")

    primitives = [
        Line(
            start=(float(x0), float(y0)),
            end=(float(x1), float(y1)),
            diameter=tool_dia,
            level_polarity="dark",
        )
        for x0, y0, x1, y1 in lines
    ]

    bounds = ((float(min_x), float(max_x)), (float(min_y), float(max_y)))
    name = f"{source_layer.name}_surfacing_tp"
    meta = {
        "name": name,
        "kind": "surfacing_toolpath",
        "path": str(source_layer.path),
        "units": "mm",
        "derived_from": source_layer.name,
        "source_kind": source_layer.kind,
        "surfacing_tool_diameter_mm": f"{tool_dia:.6f}",
        "surfacing_stepover_mm": f"{stepover:.6f}",
        "surfacing_stepover_pct": f"{_stepover_pct(tool_dia, stepover):.6f}",
        "surfacing_margin_mm": f"{margin:.6f}",
        "surfacing_sweep_axis": sweep_axis,
        "surfacing_pass_count": str(len(primitives)),
        "surfacing_bounds_min_x_mm": f"{min_x:.6f}",
        "surfacing_bounds_max_x_mm": f"{max_x:.6f}",
        "surfacing_bounds_min_y_mm": f"{min_y:.6f}",
        "surfacing_bounds_max_y_mm": f"{max_y:.6f}",
        # Z-depth is intentionally controlled in RoutePro/Bungard run-time dialogs.
        "surfacing_depth_control": "machine_runtime",
    }

    return Layer(
        name=name,
        path=Path(str(source_layer.path)),
        kind="geometry",
        source=_DerivedSource(units="mm", primitives=primitives, bounds=bounds),
        color="#58A6FF",
        opacity=1.0,
        role="unassigned",
        bbox=(float(min_x), float(min_y), float(max_x), float(max_y)),
        metadata=meta,
    )


def _resolve_stepover_mm(tool_dia_mm: float, stepover_mm: float, stepover_pct: float) -> float:
    direct = float(stepover_mm)
    if direct > 0.0:
        return max(0.001, direct)
    pct = max(1.0, min(100.0, float(stepover_pct)))
    return max(0.001, tool_dia_mm * (pct / 100.0))


def _stepover_pct(tool_dia_mm: float, stepover_mm: float) -> float:
    if tool_dia_mm <= 1e-9:
        return 0.0
    return (float(stepover_mm) / float(tool_dia_mm)) * 100.0


def _normalize_sweep_axis(value: str) -> str:
    axis = (value or "").strip().lower()
    if axis not in {"x", "y"}:
        return "x"
    return axis


def _scan_positions(start_mm: float, end_mm: float, step_mm: float) -> list[float]:
    start = float(start_mm)
    end = float(end_mm)
    if end < start:
        start, end = end, start
    span = max(0.0, end - start)
    if span <= 1e-9:
        return [start]

    step = max(1e-6, float(step_mm))
    count = int(math.floor(span / step))
    out = [start + (i * step) for i in range(count + 1)]
    if out[-1] < (end - 1e-9):
        out.append(end)
    else:
        out[-1] = end
    return out


def _raster_lines(
    *,
    min_x: float,
    max_x: float,
    min_y: float,
    max_y: float,
    stepover_mm: float,
    sweep_axis: str,
) -> list[tuple[float, float, float, float]]:
    out: list[tuple[float, float, float, float]] = []
    axis = _normalize_sweep_axis(sweep_axis)
    if axis == "x":
        rows = _scan_positions(min_y, max_y, stepover_mm)
        for idx, y in enumerate(rows):
            if idx % 2 == 0:
                out.append((min_x, y, max_x, y))
            else:
                out.append((max_x, y, min_x, y))
        return out

    cols = _scan_positions(min_x, max_x, stepover_mm)
    for idx, x in enumerate(cols):
        if idx % 2 == 0:
            out.append((x, min_y, x, max_y))
        else:
            out.append((x, max_y, x, min_y))
    return out


def _log(log: callable | None, msg: str) -> None:
    if log is None:
        return
    try:
        log(msg)
    except Exception:
        pass
