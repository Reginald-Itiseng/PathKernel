from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import math

from app.core.io import Arc, Line
from app.core.project import Layer, Project


@dataclass(slots=True)
class CenteringHolesParams:
    board_min_x_mm: float
    board_max_x_mm: float
    board_min_y_mm: float
    board_max_y_mm: float
    orientation: str = "horizontal"  # "horizontal" | "vertical"
    outline_to_hole_center_mm: float = 12.0
    hole_diameter_mm: float = 1.0
    tool_diameter_mm: float = 1.0
    lateral_stepover_pct: float = 50.0
    extra_depth_mm: float = 0.0


@dataclass(slots=True)
class _DerivedSource:
    units: str
    primitives: list[Any]
    bounds: tuple[tuple[float, float], tuple[float, float]] | None = None


def build_centering_holes_toolpath_layer(
    source_layer: Layer,
    project: Project,  # noqa: ARG001
    params: CenteringHolesParams,
    log: callable | None = None,
) -> Layer:
    min_x = min(float(params.board_min_x_mm), float(params.board_max_x_mm))
    max_x = max(float(params.board_min_x_mm), float(params.board_max_x_mm))
    min_y = min(float(params.board_min_y_mm), float(params.board_max_y_mm))
    max_y = max(float(params.board_min_y_mm), float(params.board_max_y_mm))
    if (max_x - min_x) <= 1e-9 or (max_y - min_y) <= 1e-9:
        raise ValueError("Centering holes require a valid board bounding box.")

    orientation = _normalize_orientation(params.orientation)
    dist = max(0.0, float(params.outline_to_hole_center_mm))
    hole_dia = max(0.001, float(params.hole_diameter_mm))
    tool_dia = max(0.001, float(params.tool_diameter_mm))
    stepover_pct = max(1.0, min(100.0, float(params.lateral_stepover_pct)))
    stepover_mm = max(0.0005, (tool_dia * 0.5) * (stepover_pct / 100.0))
    extra_depth = max(0.0, float(params.extra_depth_mm))

    centers = _centering_hole_centers(
        min_x=min_x,
        max_x=max_x,
        min_y=min_y,
        max_y=max_y,
        orientation=orientation,
        distance_mm=dist,
    )
    _log(
        log,
        (
            "centering: "
            f"orientation={orientation}, distance={dist:.4f}mm, "
            f"hole={hole_dia:.4f}mm, tool={tool_dia:.4f}mm"
        ),
    )

    primitives: list[Any] = []
    bore_hole_count = 0
    bore_pass_count = 0
    for idx, (x, y) in enumerate(centers):
        # Tiny plunge segment keeps explicit plunge semantics for HPGL export.
        plunge_end = (x + 1e-4, y)
        primitives.append(
            Line(
                start=(x, y),
                end=plunge_end,
                diameter=tool_dia,
                level_polarity="dark",
            )
        )

        orbit_radius = (hole_dia - tool_dia) * 0.5
        if orbit_radius <= 0.0005:
            _log(log, f"centering: hole {idx + 1}/2 plunge-only (tool >= hole).")
            continue

        bore_hole_count += 1
        passes = max(1, int(math.ceil(orbit_radius / stepover_mm)))
        for pass_idx in range(1, passes + 1):
            pass_radius = orbit_radius * (pass_idx / passes)
            start = (x + pass_radius, y)
            primitives.append(
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
                f"centering: hole {idx + 1}/2 bore radius={orbit_radius:.4f}mm "
                f"in {passes} pass(es)"
            ),
        )

    reach_r = max(tool_dia, hole_dia) * 0.5
    centers_x = [c[0] for c in centers]
    centers_y = [c[1] for c in centers]
    bx0 = min(centers_x) - reach_r
    bx1 = max(centers_x) + reach_r
    by0 = min(centers_y) - reach_r
    by1 = max(centers_y) + reach_r
    bounds = ((float(bx0), float(bx1)), (float(by0), float(by1)))

    name = f"{source_layer.name}_centering_tp"
    meta = {
        "name": name,
        "kind": "centering_holes_toolpath",
        "path": str(source_layer.path),
        "units": "mm",
        "derived_from": source_layer.name,
        "source_kind": source_layer.kind,
        "centering_orientation": orientation,
        "centering_outline_to_hole_center_mm": f"{dist:.6f}",
        "centering_hole_diameter_mm": f"{hole_dia:.6f}",
        "centering_tool_diameter_mm": f"{tool_dia:.6f}",
        "centering_lateral_stepover_pct": f"{stepover_pct:.3f}",
        "centering_lateral_stepover_mm": f"{stepover_mm:.6f}",
        "centering_extra_depth_mm": f"{extra_depth:.6f}",
        "centering_hole_count": str(len(centers)),
        "centering_bore_hole_count": str(bore_hole_count),
        "centering_bore_pass_count": str(bore_pass_count),
    }
    return Layer(
        name=name,
        path=Path(str(source_layer.path)),
        kind="geometry",
        source=_DerivedSource(units="mm", primitives=primitives, bounds=bounds),
        color="#79C0FF",
        opacity=1.0,
        role="drills",
        bbox=(bounds[0][0], bounds[1][0], bounds[0][1], bounds[1][1]),
        metadata=meta,
    )


def centering_hole_mirror_axis(layer: Layer) -> tuple[str, float] | None:
    """Return the reflection axis implied by a centering-holes toolpath layer.

    The axis name matches the layer transform flag to toggle:
    - ``"mirror_y"`` reflects across the horizontal line through left/right holes.
    - ``"mirror_x"`` reflects across the vertical line through top/bottom holes.
    """
    kind = str((getattr(layer, "metadata", {}) or {}).get("kind", "")).strip().lower()
    if kind != "centering_holes_toolpath":
        return None

    centers: list[tuple[float, float]] = []
    for primitive in list(getattr(getattr(layer, "source", None), "primitives", []) or []):
        if hasattr(primitive, "center"):
            continue
        start = getattr(primitive, "start", None)
        end = getattr(primitive, "end", None)
        if not (_is_xy_pair(start) and _is_xy_pair(end)):
            continue
        sx, sy = float(start[0]), float(start[1])
        ex, ey = float(end[0]), float(end[1])
        if math.hypot(ex - sx, ey - sy) > 0.01:
            continue
        centers.append((sx, sy))
        if len(centers) >= 2:
            break

    if len(centers) < 2:
        return None

    (x0, y0), (x1, y1) = centers[:2]
    if abs(x1 - x0) >= abs(y1 - y0):
        return "mirror_y", (y0 + y1) * 0.5
    return "mirror_x", (x0 + x1) * 0.5


def _centering_hole_centers(
    *,
    min_x: float,
    max_x: float,
    min_y: float,
    max_y: float,
    orientation: str,
    distance_mm: float,
) -> list[tuple[float, float]]:
    cx = 0.5 * (min_x + max_x)
    cy = 0.5 * (min_y + max_y)
    dist = max(0.0, float(distance_mm))
    if _normalize_orientation(orientation) == "vertical":
        return [(cx, min_y - dist), (cx, max_y + dist)]
    return [(min_x - dist, cy), (max_x + dist, cy)]


def _normalize_orientation(value: str) -> str:
    out = (value or "").strip().lower()
    if out not in {"horizontal", "vertical"}:
        return "horizontal"
    return out


def _is_xy_pair(value: Any) -> bool:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return False
    try:
        float(value[0])
        float(value[1])
    except Exception:
        return False
    return True


def _log(log: callable | None, msg: str) -> None:
    if log is None:
        return
    try:
        log(msg)
    except Exception:
        pass
