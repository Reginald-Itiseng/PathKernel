from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import math

from app.core.io import Arc, Circle, Drill, Line
from app.core.project import Layer, Project


@dataclass(slots=True)
class DrillToolpathParams:
    tool_diameter_mm: float
    lateral_stepover_pct: float = 50.0
    boring_cycle_mode: str = "drill_at_center"
    drilling_depth_mm: float = 0.0
    boring_speed_mm_s: float = 0.0


@dataclass(slots=True)
class _DerivedSource:
    units: str
    primitives: list[Any]
    bounds: tuple[tuple[float, float], tuple[float, float]] | None = None


def build_drill_toolpath_layer(
    source_layer: Layer,
    project: Project,  # noqa: ARG001
    params: DrillToolpathParams,
    log: callable | None = None,
) -> Layer:
    _log(log, "drill: deriving holes from Excellon geometry")
    holes = _collect_holes(source_layer)
    if not holes:
        raise ValueError("No drill holes found in selected Excellon layer.")

    tool_dia = max(0.001, float(params.tool_diameter_mm))
    stepover_pct = max(1.0, min(100.0, float(params.lateral_stepover_pct)))
    stepover_mm = max(0.0005, (tool_dia * 0.5) * (stepover_pct / 100.0))

    _log(
        log,
        (
            f"drill: strategy=A single-tool boring, tool={tool_dia:.4f}mm, "
            f"stepover={stepover_pct:.1f}% ({stepover_mm:.4f}mm radial)"
        ),
    )

    out_primitives: list[Any] = []
    plunge_count = 0
    bore_hole_count = 0
    bore_pass_count = 0
    skipped_too_small = 0

    for idx, (x, y, hole_dia) in enumerate(holes):
        if hole_dia <= 0.0:
            continue
        if hole_dia < (tool_dia - 0.0005):
            skipped_too_small += 1
            _log(
                log,
                (
                    f"drill: skipping hole {idx + 1}/{len(holes)} dia={hole_dia:.4f}mm "
                    f"(smaller than tool {tool_dia:.4f}mm)"
                ),
            )
            continue
        orbit_radius = (hole_dia - tool_dia) * 0.5
        # Strategy A semantics:
        # - always plunge at center first (represented by a tiny line segment),
        # - if hole > tool, add concentric full arcs for boring expansion.
        #
        # The tiny line preserves an explicit plunge command in HPGL export while
        # remaining visually negligible in the viewport.
        plunge_end = (x + 1e-4, y)
        out_primitives.append(
            Line(
                start=(x, y),
                end=plunge_end,
                diameter=tool_dia,
                level_polarity="dark",
            )
        )
        plunge_count += 1

        if orbit_radius <= 0.0005:
            # Display-only helper for plunge holes (export ignores circles).
            out_primitives.append(
                Circle(
                    position=(x, y),
                    diameter=tool_dia,
                    level_polarity="dark",
                    flashed=True,
                )
            )
            continue

        bore_hole_count += 1
        passes = max(1, int(math.ceil(orbit_radius / stepover_mm)))
        for pass_idx in range(1, passes + 1):
            pass_radius = orbit_radius * (pass_idx / passes)
            start = (x + pass_radius, y)
            out_primitives.append(
                Arc(
                    start=start,
                    end=start,
                    center=(x, y),
                    direction="counterclockwise",
                    diameter=tool_dia,
                    level_polarity="dark",
                )
            )
            bore_pass_count += 1

        _log(
            log,
            (
                f"drill: hole {idx + 1}/{len(holes)} dia={hole_dia:.4f}mm "
                f"-> bore radius={orbit_radius:.4f}mm in {passes} pass(es)"
            ),
        )

    if not out_primitives:
        raise ValueError("Drill toolpath generation produced no commands. Tool may be too large for all holes.")

    bounds = _bounds_from_holes(holes)
    source = _DerivedSource(units="mm", primitives=out_primitives, bounds=bounds)
    name = f"{source_layer.name}_drill"
    meta = {
        "name": name,
        "kind": "drill_toolpath",
        "path": str(source_layer.path),
        "units": "mm",
        "derived_from": source_layer.name,
        "source_kind": source_layer.kind,
        "drill_strategy": "A",
        "drill_tool_diameter_mm": f"{tool_dia:.6f}",
        "drill_lateral_stepover_pct": f"{stepover_pct:.3f}",
        "drill_lateral_stepover_mm": f"{stepover_mm:.6f}",
        "drill_boring_cycle_mode": str(params.boring_cycle_mode),
        "drill_depth_mm": f"{max(0.0, float(params.drilling_depth_mm)):.6f}",
        "drill_boring_speed_mm_s": f"{max(0.0, float(params.boring_speed_mm_s)):.6f}",
        "drill_hole_count": str(len(holes)),
        "drill_plunge_count": str(plunge_count),
        "drill_bore_hole_count": str(bore_hole_count),
        "drill_bore_pass_count": str(bore_pass_count),
        "drill_skipped_small_holes": str(skipped_too_small),
    }
    return Layer(
        name=name,
        path=Path(str(source_layer.path)),
        kind="geometry",
        source=source,
        color="#7EE787",
        opacity=1.0,
        role="holes",
        bbox=_bbox_from_bounds(bounds),
        metadata=meta,
    )


def _collect_holes(source_layer: Layer) -> list[tuple[float, float, float]]:
    out: list[tuple[float, float, float]] = []
    primitives = list(getattr(getattr(source_layer, "source", None), "primitives", []) or [])
    for primitive in primitives:
        cls = primitive.__class__.__name__.lower()
        if cls in {"drill", "circle"} and hasattr(primitive, "position") and hasattr(primitive, "diameter"):
            try:
                pos = getattr(primitive, "position")
                x = float(pos[0])
                y = float(pos[1])
                dia = float(getattr(primitive, "diameter"))
            except Exception:
                continue
            if dia > 0.0:
                # Apply layer-local transform so drill generation follows moved/rotated boards.
                tx, ty = _apply_layer_transform_xy(x, y, source_layer)
                out.append((tx, ty, dia))
            continue
        if isinstance(primitive, Drill):
            try:
                x = float(primitive.position[0])
                y = float(primitive.position[1])
                dia = float(primitive.diameter)
            except Exception:
                continue
            if dia > 0.0:
                # Apply layer-local transform so drill generation follows moved/rotated boards.
                tx, ty = _apply_layer_transform_xy(x, y, source_layer)
                out.append((tx, ty, dia))
    return out


def _apply_layer_transform_xy(x: float, y: float, layer: Layer) -> tuple[float, float]:
    # Keep transform order aligned with cutout/isolation:
    # mirror -> rotate -> translate.
    out_x = float(x)
    out_y = float(y)
    if bool(getattr(layer, "mirror_x", False)):
        out_x = -out_x
    if bool(getattr(layer, "mirror_y", False)):
        out_y = -out_y

    rot = float(getattr(layer, "rotation_deg", 0.0) or 0.0)
    if abs(rot) > 1e-9:
        theta = math.radians(rot)
        c = math.cos(theta)
        s = math.sin(theta)
        rx = (out_x * c) - (out_y * s)
        ry = (out_x * s) + (out_y * c)
        out_x, out_y = rx, ry

    dx = float(getattr(layer, "offset_x_mm", 0.0) or 0.0)
    dy = float(getattr(layer, "offset_y_mm", 0.0) or 0.0)
    out_x += dx
    out_y += dy
    return out_x, out_y


def _bounds_from_holes(holes: list[tuple[float, float, float]]) -> tuple[tuple[float, float], tuple[float, float]] | None:
    if not holes:
        return None
    min_x = float("inf")
    min_y = float("inf")
    max_x = float("-inf")
    max_y = float("-inf")
    for x, y, dia in holes:
        r = max(0.0, dia * 0.5)
        min_x = min(min_x, x - r)
        min_y = min(min_y, y - r)
        max_x = max(max_x, x + r)
        max_y = max(max_y, y + r)
    if min_x == float("inf"):
        return None
    return ((min_x, max_x), (min_y, max_y))


def _bbox_from_bounds(bounds: tuple[tuple[float, float], tuple[float, float]] | None):
    if not bounds:
        return None
    try:
        (min_x, max_x), (min_y, max_y) = bounds
        return (float(min_x), float(min_y), float(max_x), float(max_y))
    except Exception:
        return None


def _log(log: callable | None, msg: str) -> None:
    if log is None:
        return
    try:
        log(msg)
    except Exception:
        pass
