"""Copper clearing (hatching) toolpath generation constrained by keepouts and board bounds."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shapely import affinity
from shapely.geometry import LineString, box
from shapely.ops import unary_union

from app.core.cutout import extract_cutout_loops
from app.core.io import Line
from app.core.isolation import (
    _apply_layer_transform,
    _bbox_from_bounds,
    _bounds_tuple,
    _build_copper_geometry,
    _derive_isolation_tool_geometry,
    _primitive_to_shape,
)
from app.core.project import Layer, Project


@dataclass(slots=True)
class HatchingParams:
    """Copper clearing settings: tool geometry, stepover and clipping margins."""

    tool_diameter_mm: float
    tool_profile: str = "cylindrical/flute"
    tool_tip_diameter_mm: float = 0.0
    tool_angle_deg: float = 0.0
    cutting_depth_mm: float = 0.0
    overlap: float = 0.5
    hatching_margin_mm: float = 0.0
    hatch_angle_deg: float = 0.0
    copper_keepout_margin_mm: float = 0.0
    boundary_margin_mm: float = 0.0
    buffer_join_style: int = 1


@dataclass(slots=True)
class _DerivedSource:
    units: str
    primitives: list[Any]
    bounds: tuple[tuple[float, float], tuple[float, float]] | None = None


def build_hatching_toolpath_layer(
    source_layer: Layer,
    project: Project,
    params: HatchingParams,
    *,
    keepout_layers: list[Layer] | None = None,
    board_layers: list[Layer] | None = None,
    copper_keepout_layers: list[Layer] | None = None,
    log: callable | None = None,
) -> Layer:
    """Build a copper-clearing hatch toolpath clipped by board domain and keepouts."""
    del project  # Stateless generator signature parity with other builders.
    _log(log, "hatching: deriving copper geometry")
    copper = _build_copper_geometry(source_layer)
    if copper is None or copper.is_empty:
        raise ValueError("Could not derive copper geometry from this layer.")

    join_style = _normalized_join_style(getattr(params, "buffer_join_style", 1))
    tool = _derive_isolation_tool_geometry(params)
    effective_radius = float(tool["effective_radius_mm"])
    effective_diameter = float(tool["effective_diameter_mm"])
    step = max(0.001, float(tool["hatching_margin_mm"]))
    hatch_angle = float(getattr(params, "hatch_angle_deg", 0.0) or 0.0)
    copper_keepout_margin = max(0.0, float(getattr(params, "copper_keepout_margin_mm", 0.0) or 0.0))
    boundary_margin = max(0.0, float(getattr(params, "boundary_margin_mm", 0.0) or 0.0))
    safety_clearance = max(0.005, effective_radius * 0.02)

    _log(
        log,
        (
            "hatching: tool "
            f"profile={tool['profile']} "
            f"effective={effective_diameter:.4f}mm "
            f"step={step:.4f}mm "
            f"angle={hatch_angle:.2f}deg"
        ),
    )

    board_domain = _board_domain_from_layers(board_layers or [], copper=copper, log=log)
    if board_layers and (board_domain is None or board_domain.is_empty):
        raise ValueError("Could not derive a valid board cutout domain from cutout layers.")

    if board_domain is not None and not board_domain.is_empty:
        clear_domain = board_domain
        _log(log, f"hatching: constrained to board cutout domain from {len(list(board_layers or []))} layer(s)")
    else:
        min_x, min_y, max_x, max_y = copper.bounds
        clear_domain = box(
            float(min_x - boundary_margin),
            float(min_y - boundary_margin),
            float(max_x + boundary_margin),
            float(max_y + boundary_margin),
        )

    selected_copper_keepout = _copper_keepout_from_layers(copper_keepout_layers or [])
    copper_for_keepout = copper
    if selected_copper_keepout is not None and not selected_copper_keepout.is_empty:
        copper_for_keepout = unary_union([copper, selected_copper_keepout])

    copper_keepout = copper_for_keepout.buffer(
        effective_radius + copper_keepout_margin + safety_clearance,
        join_style=join_style,
    )
    clear_region = clear_domain.difference(copper_keepout)
    if clear_region is None or clear_region.is_empty:
        raise ValueError("No hatchable region remains after copper keepout.")

    keepout_swept = _toolpath_keepout_swept_area(
        keepout_layers or [],
        keepout_expand=max(0.001, effective_radius * 0.1),
        join_style=join_style,
    )
    if keepout_swept is not None and not keepout_swept.is_empty:
        _log(log, f"hatching: applying keepout from {len(list(keepout_layers or []))} existing toolpath layer(s)")
        clear_region = clear_region.difference(keepout_swept)
    if clear_region is None or clear_region.is_empty:
        raise ValueError("No hatchable region remains after existing toolpath keepout.")

    hatch_lines = _hatch_lines(clear_region, step=step, angle_deg=hatch_angle)
    hatch_lines = _filter_hatch_lines_against_copper(
        hatch_lines,
        copper=copper_for_keepout,
        tool_radius=effective_radius,
        copper_keepout_margin=copper_keepout_margin,
        safety_clearance=safety_clearance,
        join_style=join_style,
    )
    if not hatch_lines:
        raise ValueError("No hatching toolpaths were generated with current parameters.")

    _log(log, f"hatching: generated {len(hatch_lines)} hatch line(s)")
    primitives = _line_primitives_from_lines(hatch_lines, effective_diameter)
    if not primitives:
        raise ValueError("Generated hatching toolpath is empty.")

    merged = unary_union(hatch_lines)
    bounds = _bounds_tuple(merged.bounds if hasattr(merged, "bounds") else None)
    meta = {
        "name": f"{source_layer.name}_hatch_tp",
        "kind": "hatching_toolpath",
        "path": str(source_layer.path),
        "units": "mm",
        "derived_from": source_layer.name,
        "source_role": str(getattr(source_layer, "role", "") or ""),
        "source_kind": source_layer.kind,
        "hatching_tool_diameter_mm": f"{effective_diameter:.6f}",
        "hatching_nominal_tool_diameter_mm": f"{tool['nominal_diameter_mm']:.6f}",
        "hatching_effective_radius_mm": f"{effective_radius:.6f}",
        "hatching_step_mm": f"{step:.6f}",
        "hatching_angle_deg": f"{hatch_angle:.3f}",
        "hatching_copper_keepout_margin_mm": f"{copper_keepout_margin:.6f}",
        "hatching_safety_clearance_mm": f"{safety_clearance:.6f}",
        "hatching_boundary_margin_mm": f"{boundary_margin:.6f}",
        "hatching_board_cutout_awareness": "true",
        "hatching_board_cutout_layer_count": str(len(list(board_layers or []))),
        "hatching_copper_keepout_layer_count": str(len(list(copper_keepout_layers or []))),
    }
    return Layer(
        name=f"{source_layer.name}_hatch_tp",
        path=Path(str(source_layer.path)),
        kind="geometry",
        source=_DerivedSource(units="mm", primitives=primitives, bounds=bounds),
        color="#F0B429",
        opacity=1.0,
        role="unassigned",
        bbox=_bbox_from_bounds(bounds),
        metadata=meta,
    )


def _hatch_lines(region, *, step: float, angle_deg: float) -> list[LineString]:
    if region is None or region.is_empty:
        return []
    spacing = max(0.001, float(step))
    rot = affinity.rotate(region, -angle_deg, origin=(0.0, 0.0), use_radians=False)
    if rot is None or rot.is_empty:
        return []
    min_x, min_y, max_x, max_y = rot.bounds
    reach = max(1.0, (max_x - min_x) + spacing)
    y = float(min_y)
    row = 0
    out: list[LineString] = []
    while y <= (float(max_y) + 1e-9):
        probe = LineString([(float(min_x - reach), y), (float(max_x + reach), y)])
        clipped = rot.intersection(probe)
        row_lines = [ln for ln in _iter_lines(clipped) if ln is not None and not ln.is_empty and ln.length > 1e-6]
        row_lines.sort(key=lambda ln: min(float(ln.coords[0][0]), float(ln.coords[-1][0])))
        if row % 2 == 1:
            row_lines = [_reversed_line(ln) for ln in reversed(row_lines)]
        out.extend(row_lines)
        y += spacing
        row += 1

    if abs(float(angle_deg)) <= 1e-12:
        return out
    return [affinity.rotate(ln, angle_deg, origin=(0.0, 0.0), use_radians=False) for ln in out]


def _reversed_line(line: LineString) -> LineString:
    return LineString(list(line.coords)[::-1])


def _line_primitives_from_lines(lines: list[LineString], tool_dia: float) -> list[Any]:
    out: list[Any] = []
    width = max(0.02, float(tool_dia))
    for line in lines:
        coords = list(getattr(line, "coords", []) or [])
        if len(coords) < 2:
            continue
        for i in range(len(coords) - 1):
            a = coords[i]
            b = coords[i + 1]
            out.append(
                Line(
                    start=(float(a[0]), float(a[1])),
                    end=(float(b[0]), float(b[1])),
                    diameter=width,
                    level_polarity="dark",
                )
            )
    return out


def _filter_hatch_lines_against_copper(
    lines: list[LineString],
    *,
    copper,
    tool_radius: float,
    copper_keepout_margin: float,
    safety_clearance: float,
    join_style: int,
) -> list[LineString]:
    if not lines:
        return []
    if copper is None or copper.is_empty:
        return list(lines)

    forbidden = copper.buffer(
        max(0.0, float(tool_radius) + float(copper_keepout_margin) + float(safety_clearance)),
        join_style=join_style,
    )
    if forbidden is None or forbidden.is_empty:
        return list(lines)

    out: list[LineString] = []
    for line in lines:
        if line is None or line.is_empty or line.length <= 1e-6:
            continue
        try:
            swept = line.buffer(max(0.0, float(tool_radius)), join_style=join_style)
            if swept is not None and not swept.is_empty and not swept.intersects(copper):
                out.append(line)
                continue
        except Exception:
            pass

        try:
            clipped = line.difference(forbidden)
        except Exception:
            continue
        for safe_line in _iter_lines(clipped):
            if safe_line is not None and not safe_line.is_empty and safe_line.length > 1e-6:
                out.append(safe_line)
    return out


def _toolpath_keepout_swept_area(
    layers: list[Layer],
    *,
    keepout_expand: float,
    join_style: int,
):
    swept_parts = []
    for layer in layers:
        for primitive in getattr(getattr(layer, "source", None), "primitives", []) or []:
            shape = _primitive_to_shape(primitive)
            if shape is None or shape.is_empty:
                continue
            shape = _apply_layer_transform(shape, layer)
            polarity = str(getattr(primitive, "level_polarity", "dark")).strip().lower()
            if "clear" in polarity:
                continue
            swept_parts.append(shape)
    if not swept_parts:
        return None
    base = unary_union(swept_parts)
    if base is None or base.is_empty:
        return None
    try:
        expanded = base.buffer(max(0.0, float(keepout_expand)), join_style=join_style)
        return expanded if expanded is not None and not expanded.is_empty else base
    except Exception:
        return base


def _copper_keepout_from_layers(layers: list[Layer]):
    parts = []
    for layer in list(layers or []):
        copper = _build_copper_geometry(layer)
        if copper is not None and not copper.is_empty:
            parts.append(copper)
    if not parts:
        return None
    merged = unary_union(parts)
    return merged if merged is not None and not merged.is_empty else None


def _board_domain_from_layers(layers: list[Layer], *, copper=None, log: callable | None = None):
    loops = []
    for layer in list(layers or []):
        try:
            layer_loops = extract_cutout_loops(layer, log=log)
        except Exception:
            continue
        if layer_loops:
            loops.extend(layer_loops)
    if not loops:
        return None
    domain = _loops_to_domain(loops)
    if copper is None or getattr(copper, "is_empty", True) or domain is None or domain.is_empty:
        return domain
    return _domain_components_touching_copper(domain, copper)


def _domain_components_touching_copper(domain, copper):
    selected = []
    for part in _iter_polygons(domain):
        try:
            if part.intersects(copper) or part.contains(copper.representative_point()):
                selected.append(part)
        except Exception:
            continue
    if not selected:
        return None
    merged = unary_union(selected)
    return merged if merged is not None and not merged.is_empty else None


def _iter_polygons(geom):
    if geom is None or geom.is_empty:
        return
    if geom.geom_type == "Polygon":
        yield geom
        return
    for sub in getattr(geom, "geoms", []) or []:
        yield from _iter_polygons(sub)


def _loops_to_domain(loops: list[Any]):
    valid = [lp for lp in list(loops or []) if lp is not None and not lp.is_empty]
    if not valid:
        return None

    add_polys = []
    sub_polys = []
    for i, loop in enumerate(valid):
        try:
            rp = loop.representative_point()
        except Exception:
            continue
        depth = 0
        for j, other in enumerate(valid):
            if i == j:
                continue
            try:
                if float(getattr(other, "area", 0.0)) <= float(getattr(loop, "area", 0.0)):
                    continue
                if other.contains(rp):
                    depth += 1
            except Exception:
                continue
        if depth % 2 == 0:
            add_polys.append(loop)
        else:
            sub_polys.append(loop)

    if not add_polys:
        return None
    domain = unary_union(add_polys)
    if sub_polys:
        domain = domain.difference(unary_union(sub_polys))
    return domain if domain is not None and not domain.is_empty else None


def _iter_lines(geom):
    if geom is None or geom.is_empty:
        return
    gt = geom.geom_type
    if gt in {"LineString", "LinearRing"}:
        yield LineString(list(geom.coords))
        return
    for sub in getattr(geom, "geoms", []) or []:
        yield from _iter_lines(sub)


def _normalized_join_style(value: Any) -> int:
    try:
        out = int(value)
    except Exception:
        return 1
    return out if out in {1, 2, 3} else 1


def _log(log: callable | None, msg: str) -> None:
    if log is None:
        return
    try:
        log(msg)
    except Exception:
        pass
