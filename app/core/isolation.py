from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import math

from shapely import affinity
from shapely.ops import linemerge, unary_union

from app.core.cnc_params import calculate_coppercam_params
from app.core.io import Line
from app.core.project import Layer, Project


@dataclass(slots=True)
class IsolationParams:
    tool_diameter_mm: float
    passes: int = 1
    overlap: float = 0.0
    iso_type: int = 2  # 0=exterior, 1=interior, 2=both
    extra_pad_contours: int = 0
    # Optional dynamic V-bit geometry inputs.
    tool_profile: str = "cylindrical/flute"
    tool_tip_diameter_mm: float = 0.0
    tool_angle_deg: float = 0.0
    cutting_depth_mm: float = 0.0
    # Extra offset added beyond the effective tool radius for the first pass.
    trace_margin_mm: float = 0.0
    # Optional explicit hatching step-over (mm). If <= 0, derived automatically.
    hatching_margin_mm: float = 0.0
    # Shapely buffer join style: 1=round, 2=mitre, 3=bevel.
    buffer_join_style: int = 1
    # When true, passes wider than minimum measured copper gap are skipped.
    skip_tight_clearance_paths: bool = False


@dataclass(slots=True)
class _DerivedSource:
    units: str
    primitives: list[Any]
    bounds: tuple[tuple[float, float], tuple[float, float]] | None = None


def build_isolation_layer(
    source_layer: Layer,
    project: Project,
    params: IsolationParams,
    log: callable | None = None,
) -> Layer:
    _log(log, "isolation: deriving copper geometry")
    copper = _build_copper_geometry(source_layer)
    if copper is None or copper.is_empty:
        raise ValueError("Could not derive copper geometry from this layer.")
    join_style = _normalized_join_style(getattr(params, "buffer_join_style", 1))
    tool = _derive_isolation_tool_geometry(params)
    effective_radius = float(tool["effective_radius_mm"])
    effective_diameter = float(tool["effective_diameter_mm"])
    step = float(tool["hatching_margin_mm"])
    offset_start = float(tool["trace_compensation_mm"])
    passes = max(1, int(params.passes))
    _log(
        log,
        (
            "isolation: tool "
            f"profile={tool['profile']} "
            f"nominal={tool['nominal_diameter_mm']:.4f}mm "
            f"effective={effective_diameter:.4f}mm "
            f"radius={effective_radius:.4f}mm "
            f"trace_margin={tool['trace_margin_mm']:.4f}mm "
            f"hatch_step={step:.4f}mm"
        ),
    )

    min_gap = _minimum_copper_gap(copper)
    clearance_warning = ""
    if min_gap is not None and effective_diameter > (min_gap + 1e-9):
        clearance_warning = (
            "effective tool width exceeds minimum copper gap: "
            f"D_eff={effective_diameter:.4f}mm > gap={min_gap:.4f}mm"
        )
        _log(log, f"isolation: warning: {clearance_warning}")

    iso_shapes = []
    for i in range(passes):
        offset = offset_start + (i * step)
        if (
            bool(getattr(params, "skip_tight_clearance_paths", False))
            and min_gap is not None
            and (2.0 * offset) > (min_gap + 1e-9)
        ):
            _log(
                log,
                (
                    "isolation: skipping pass due to tight clearance "
                    f"(offset={offset:.4f}mm, min_gap={min_gap:.4f}mm)"
                ),
            )
            continue
        _log(log, f"isolation: pass {i + 1}/{passes} at offset {offset:.4f} mm")
        geo = _isolation_geometry(copper, offset=offset, iso_type=params.iso_type, join_style=join_style)
        if geo is not None and not geo.is_empty:
            iso_shapes.append(_clean_linework(geo))

    extra_pad_contours = max(0, int(params.extra_pad_contours))
    if extra_pad_contours > 0:
        _log(log, f"isolation: computing {extra_pad_contours} extra pad contour(s)")
        pad_shapes = _collect_pad_shapes(source_layer)
        non_pad_shapes = _collect_non_pad_shapes(source_layer)
        if pad_shapes:
            pads_union = unary_union(pad_shapes)
            non_pad_union = unary_union(non_pad_shapes) if non_pad_shapes else None
            tool_radius = max(0.001, effective_radius)
            existing_paths = unary_union([g for g in iso_shapes if g is not None and not g.is_empty]) if iso_shapes else None
            existing_swept = (
                _linework_swept_area(existing_paths, tool_radius, join_style=join_style)
                if existing_paths is not None
                else None
            )
            start = offset_start + (passes * step)
            for i in range(extra_pad_contours):
                offset = start + (i * step)
                _log(log, f"isolation: extra pad contour {i + 1}/{extra_pad_contours}")
                geo = _isolation_geometry(pads_union, offset=offset, iso_type=0, join_style=join_style)
                if geo is not None and not geo.is_empty:
                    candidate = _clean_linework(geo)
                    # Buffered keepout from previous toolpaths; prevents recutting into prior contours.
                    if existing_swept is not None and not existing_swept.is_empty:
                        keepout = existing_swept
                        # Prevent boundary-touch clipping (dotted/missing sections) when contour spacing
                        # is near the keepout radius due to floating-point tolerance.
                        shrink = max(0.0001, tool_radius * 0.02)
                        try:
                            shrunk = existing_swept.buffer(-shrink, join_style=join_style)
                            if shrunk is not None and not shrunk.is_empty:
                                keepout = shrunk
                        except Exception:
                            pass
                        candidate = candidate.difference(keepout)
                    # Keep extra pad contours clear of other copper features.
                    if non_pad_union is not None and not non_pad_union.is_empty:
                        candidate = candidate.difference(non_pad_union.buffer(max(0.001, tool_radius * 0.22)))
                    candidate = _clean_linework(candidate)
                    if candidate is not None and not candidate.is_empty:
                        iso_shapes.append(candidate)
                        existing_paths = candidate if existing_paths is None else unary_union([existing_paths, candidate])
                        new_swept = _linework_swept_area(candidate, tool_radius, join_style=join_style)
                        if new_swept is not None and not new_swept.is_empty:
                            existing_swept = (
                                new_swept
                                if existing_swept is None or existing_swept.is_empty
                                else unary_union([existing_swept, new_swept])
                            )

    if not iso_shapes:
        raise ValueError("Isolation produced no geometry with current parameters.")

    merged = unary_union(iso_shapes)
    _log(log, "isolation: converting linework to toolpath primitives")
    primitives = _as_line_primitives(merged, effective_diameter)
    bounds = _bounds_tuple(merged.bounds if hasattr(merged, "bounds") else None)
    meta = {
        "name": f"{source_layer.name}_iso",
        "kind": "isolation",
        "path": str(source_layer.path),
        "units": "mm",
        "derived_from": source_layer.name,
        "source_kind": source_layer.kind,
        "isolation_tool_diameter_mm": f"{effective_diameter:.6f}",
        "isolation_nominal_tool_diameter_mm": f"{tool['nominal_diameter_mm']:.6f}",
        "isolation_effective_radius_mm": f"{effective_radius:.6f}",
        "isolation_hatching_margin_mm": f"{step:.6f}",
        "isolation_trace_compensation_mm": f"{offset_start:.6f}",
        "isolation_tool_profile": str(tool["profile"]),
        "isolation_tool_tip_diameter_mm": f"{tool['tip_diameter_mm']:.6f}",
        "isolation_tool_angle_deg": f"{tool['angle_deg']:.6f}",
        "isolation_cutting_depth_mm": f"{tool['cutting_depth_mm']:.6f}",
        "isolation_trace_margin_mm": f"{tool['trace_margin_mm']:.6f}",
        "isolation_buffer_join_style": str(join_style),
        "isolation_passes": str(passes),
        "isolation_overlap": f"{params.overlap:.3f}",
        "isolation_type": _iso_type_label(params.iso_type),
        "extra_pad_contours": str(extra_pad_contours),
    }
    if min_gap is not None:
        meta["minimum_copper_gap_mm"] = f"{min_gap:.6f}"
    if clearance_warning:
        meta["tool_clearance_warning"] = clearance_warning
    derived = _DerivedSource(units="mm", primitives=primitives, bounds=bounds)
    return Layer(
        name=f"{source_layer.name}_iso",
        path=Path(str(source_layer.path)),
        kind="geometry",
        source=derived,
        color="#FFFFFF",
        opacity=1.0,
        role="unassigned",
        bbox=_bbox_from_bounds(bounds),
        metadata=meta,
    )


def _log(log: callable | None, msg: str) -> None:
    if log is None:
        return
    try:
        log(msg)
    except Exception:
        pass


def _derive_isolation_tool_geometry(params: IsolationParams) -> dict[str, float | str]:
    nominal_diameter = max(0.001, float(getattr(params, "tool_diameter_mm", 0.0) or 0.0))
    profile = str(getattr(params, "tool_profile", "cylindrical/flute") or "").strip().lower()
    tip_dia = float(getattr(params, "tool_tip_diameter_mm", 0.0) or 0.0)
    angle_deg = max(0.0, float(getattr(params, "tool_angle_deg", 0.0) or 0.0))
    cut_depth = max(0.0, float(getattr(params, "cutting_depth_mm", 0.0) or 0.0))
    trace_margin = max(0.0, float(getattr(params, "trace_margin_mm", 0.0) or 0.0))
    overlap = max(0.0, min(0.999, float(getattr(params, "overlap", 0.0) or 0.0)))
    explicit_margin = max(0.0, float(getattr(params, "hatching_margin_mm", 0.0) or 0.0))

    if profile == "conical" or (angle_deg > 0.0 and cut_depth > 0.0):
        tip = tip_dia if tip_dia > 0.0 else nominal_diameter
        calc = calculate_coppercam_params(T_dia=tip, T_angle=angle_deg, D_cut=cut_depth)
        effective_radius = max(0.001, float(calc["effective_radius"]))
        effective_diameter = max(0.002, float(calc["total_path_width"]))
        auto_margin = max(0.001, float(calc["hatching_margin"]))
        margin = explicit_margin if explicit_margin > 0.0 else auto_margin
        return {
            "profile": "conical",
            "nominal_diameter_mm": nominal_diameter,
            "tip_diameter_mm": tip,
            "angle_deg": angle_deg,
            "cutting_depth_mm": cut_depth,
            "trace_margin_mm": trace_margin,
            "effective_radius_mm": effective_radius,
            "effective_diameter_mm": effective_diameter,
            "hatching_margin_mm": max(0.001, margin),
            "trace_compensation_mm": max(0.001, effective_radius + trace_margin),
        }

    effective_diameter = nominal_diameter
    effective_radius = max(0.001, effective_diameter * 0.5)
    auto_margin = effective_diameter * max(0.01, 1.0 - overlap)
    margin = explicit_margin if explicit_margin > 0.0 else auto_margin
    return {
        "profile": "cylindrical/flute",
        "nominal_diameter_mm": nominal_diameter,
        "tip_diameter_mm": nominal_diameter,
        "angle_deg": 0.0,
        "cutting_depth_mm": 0.0,
        "trace_margin_mm": trace_margin,
        "effective_radius_mm": effective_radius,
        "effective_diameter_mm": max(0.002, effective_diameter),
        "hatching_margin_mm": max(0.001, margin),
        "trace_compensation_mm": effective_radius + trace_margin,
    }


def _normalized_join_style(value: Any) -> int:
    try:
        out = int(value)
    except Exception:
        return 1
    return out if out in {1, 2, 3} else 1


def _isolation_geometry(copper, *, offset: float, iso_type: int, join_style: int = 1):
    if offset <= 0.0:
        return None

    ext = copper.buffer(offset, join_style=join_style).boundary if iso_type in (0, 2) else None
    inn = copper.buffer(-offset, join_style=join_style).boundary if iso_type in (1, 2) else None

    if ext is not None and inn is not None:
        from shapely.ops import unary_union

        return unary_union([ext, inn])
    return ext if ext is not None else inn


def _build_copper_geometry(layer: Layer):
    from shapely.ops import unary_union

    dark = []
    clear = []
    for primitive in getattr(layer.source, "primitives", []) or []:
        shape = _primitive_to_shape(primitive)
        if shape is None or shape.is_empty:
            continue
        shape = _apply_layer_transform(shape, layer)
        polarity = str(getattr(primitive, "level_polarity", "dark")).lower()
        if "clear" in polarity:
            clear.append(shape)
        else:
            dark.append(shape)

    if not dark:
        return None

    copper = unary_union(dark)
    if clear:
        copper = copper.difference(unary_union(clear))
    return copper


def _collect_pad_shapes(layer: Layer):
    # Include amgroup so aperture-macro pads (Altium/PADS/OrCAD) are handled.
    pad_classes = {"circle", "obround", "roundrectangle", "amgroup"}
    shapes = []
    for primitive in getattr(layer.source, "primitives", []) or []:
        cls = primitive.__class__.__name__.lower()
        if cls not in pad_classes:
            continue
        if not bool(getattr(primitive, "flashed", False)):
            continue
        polarity = str(getattr(primitive, "level_polarity", "dark")).lower()
        if "clear" in polarity:
            continue
        shape = _primitive_to_shape(primitive)
        if shape is not None and not shape.is_empty:
            shape = _apply_layer_transform(shape, layer)
            shapes.append(shape)
    return shapes


def _collect_non_pad_shapes(layer: Layer):
    pad_classes = {"circle", "obround", "roundrectangle"}
    shapes = []
    for primitive in getattr(layer.source, "primitives", []) or []:
        cls = primitive.__class__.__name__.lower()
        if cls in pad_classes:
            continue
        polarity = str(getattr(primitive, "level_polarity", "dark")).lower()
        if "clear" in polarity:
            continue
        shape = _primitive_to_shape(primitive)
        if shape is not None and not shape.is_empty:
            shape = _apply_layer_transform(shape, layer)
            shapes.append(shape)
    return shapes


def _clean_linework(geom):
    if geom is None or geom.is_empty:
        return geom
    gt = geom.geom_type
    if gt in {"LineString", "LinearRing"}:
        return geom
    if gt in {"Polygon", "MultiPolygon"}:
        return geom.boundary
    if gt == "MultiLineString":
        try:
            return linemerge(unary_union(geom))
        except Exception:
            return geom
    if gt == "GeometryCollection":
        lines = []
        for g in getattr(geom, "geoms", []):
            cg = _clean_linework(g)
            if cg is None or cg.is_empty:
                continue
            if cg.geom_type in {"LineString", "LinearRing", "MultiLineString"}:
                lines.append(cg)
        if not lines:
            return geom
        try:
            return linemerge(unary_union(lines))
        except Exception:
            return unary_union(lines)
    return geom


def _linework_swept_area(geom, tool_radius: float, join_style: int = 1):
    if geom is None or geom.is_empty:
        return None
    try:
        swept = geom.buffer(tool_radius, join_style=join_style)
        return swept if swept is not None and not swept.is_empty else None
    except Exception:
        return None


def _primitive_to_shape(primitive):
    from shapely.geometry import LineString, Point
    from shapely.ops import unary_union

    cls = primitive.__class__.__name__.lower()

    if hasattr(primitive, "vertices"):
        verts = _vertices(getattr(primitive, "vertices", None))
        if len(verts) >= 3:
            return _polygon_from_vertices(verts)

    if cls in {"circle", "drill"} and hasattr(primitive, "position"):
        pos = _point_tuple(getattr(primitive, "position", None))
        dia = _positive_float(getattr(primitive, "diameter", None))
        if pos and dia:
            return Point(pos).buffer(dia * 0.5)
        return None

    if cls == "obround" and hasattr(primitive, "position"):
        pos = _point_tuple(getattr(primitive, "position", None))
        w = _positive_float(getattr(primitive, "width", None))
        h = _positive_float(getattr(primitive, "height", None))
        if pos and w and h:
            return _rounded_rect_shape(pos[0], pos[1], w, h, min(w, h) * 0.5)
        return None

    if cls == "roundrectangle" and hasattr(primitive, "position"):
        pos = _point_tuple(getattr(primitive, "position", None))
        w = _positive_float(getattr(primitive, "width", None))
        h = _positive_float(getattr(primitive, "height", None))
        r = float(getattr(primitive, "radius", 0.0) or 0.0)
        if pos and w and h:
            return _rounded_rect_shape(pos[0], pos[1], w, h, max(0.0, min(r, w * 0.5, h * 0.5)))
        return None

    if cls in {"line", "slot"}:
        start = _point_tuple(getattr(primitive, "start", None))
        end = _point_tuple(getattr(primitive, "end", None))
        width = _positive_float(getattr(primitive, "diameter", None)) or 0.0
        if start and end:
            seg = LineString([start, end])
            if width > 0.0:
                return seg.buffer(width * 0.5)
            return seg
        return None

    if cls == "arc":
        pts = _arc_xy_points(primitive)
        if len(pts) < 2:
            return None
        width = _positive_float(getattr(primitive, "diameter", None)) or 0.0
        arc = LineString(pts)
        if width > 0.0:
            return arc.buffer(width * 0.5)
        return arc

    if cls == "region":
        outline_poly = _region_outline_polygon(primitive)
        if outline_poly is not None and not outline_poly.is_empty:
            return outline_poly
        subs = getattr(primitive, "primitives", []) or []
        pieces = [_primitive_to_shape(sub) for sub in subs]
        pieces = [p for p in pieces if p is not None and not p.is_empty]
        if not pieces:
            return None
        return unary_union(pieces)

    # AMGroup: aperture-macro flash pads (common in Altium/PADS/OrCAD exports).
    # Recursively union all sub-primitive shapes so the pad is not silently dropped.
    if cls == "amgroup":
        subs = getattr(primitive, "primitives", []) or []
        pieces = [_primitive_to_shape(sub) for sub in subs]
        pieces = [p for p in pieces if p is not None and not p.is_empty]
        if not pieces:
            return None
        merged = unary_union(pieces)
        return merged if not merged.is_empty else None

    return None


def _rounded_rect_shape(cx: float, cy: float, w: float, h: float, r: float):
    from shapely.geometry import box

    x0 = cx - (w * 0.5)
    x1 = cx + (w * 0.5)
    y0 = cy - (h * 0.5)
    y1 = cy + (h * 0.5)

    if r <= 1e-9:
        return box(x0, y0, x1, y1)

    inner = box(x0 + r, y0 + r, x1 - r, y1 - r)
    return inner.buffer(r)


def _polygon_from_vertices(vertices: list[tuple[float, float]]):
    from shapely.geometry import Polygon

    try:
        poly = Polygon(vertices)
    except Exception:
        return None
    return _repair_polygon(poly)


def _repair_polygon(poly):
    from shapely.ops import unary_union

    if poly.is_empty:
        return None
    if poly.is_valid:
        return poly

    fixed = None
    try:
        from shapely.validation import make_valid

        fixed = make_valid(poly)
    except Exception:
        fixed = None

    if fixed is None:
        try:
            fixed = poly.buffer(0)
        except Exception:
            return None

    if fixed.is_empty:
        return None

    gt = fixed.geom_type
    if gt in {"Polygon", "MultiPolygon"}:
        return fixed

    polys = [g for g in getattr(fixed, "geoms", []) if getattr(g, "geom_type", "") in {"Polygon", "MultiPolygon"}]
    if not polys:
        return None
    return unary_union(polys)


def _region_outline_polygon(region):
    points = _outline_xy_points(getattr(region, "primitives", []) or [])
    if len(points) < 3:
        return None
    if abs(points[0][0] - points[-1][0]) > 1e-9 or abs(points[0][1] - points[-1][1]) > 1e-9:
        points.append(points[0])
    return _polygon_from_vertices(points)


def _outline_xy_points(primitives: list[Any]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for primitive in primitives:
        cls_name = primitive.__class__.__name__.lower()
        if cls_name == "arc":
            for px, py in _arc_xy_points(primitive):
                if not points or abs(points[-1][0] - px) > 1e-9 or abs(points[-1][1] - py) > 1e-9:
                    points.append((px, py))
            continue

        vertices = getattr(primitive, "vertices", None)
        if vertices and len(vertices) >= 2:
            for vertex in vertices:
                try:
                    px = float(vertex[0])
                    py = float(vertex[1])
                except Exception:
                    continue
                if not points or abs(points[-1][0] - px) > 1e-9 or abs(points[-1][1] - py) > 1e-9:
                    points.append((px, py))
            continue

        start = _point_tuple(getattr(primitive, "start", None))
        end = _point_tuple(getattr(primitive, "end", None))
        if start is not None:
            if not points or abs(points[-1][0] - start[0]) > 1e-9 or abs(points[-1][1] - start[1]) > 1e-9:
                points.append(start)
        if end is not None:
            if not points or abs(points[-1][0] - end[0]) > 1e-9 or abs(points[-1][1] - end[1]) > 1e-9:
                points.append(end)
    return points


def _arc_xy_points(primitive: Any) -> list[tuple[float, float]]:
    start = _point_tuple(getattr(primitive, "start", None))
    end = _point_tuple(getattr(primitive, "end", None))
    center = _point_tuple(getattr(primitive, "center", None))
    if start is None or end is None or center is None:
        return []

    sx, sy = start
    ex, ey = end
    cx, cy = center

    radius = math.hypot(sx - cx, sy - cy)
    if radius <= 0.0:
        return [start, end]

    a0 = math.atan2(sy - cy, sx - cx)
    a1 = math.atan2(ey - cy, ex - cx)
    direction = str(getattr(primitive, "direction", "counterclockwise")).lower()
    two_pi = 2.0 * math.pi

    ccw = (a1 - a0) % two_pi
    cw = ccw - two_pi
    if abs(sx - ex) < 1e-9 and abs(sy - ey) < 1e-9:
        sweep = two_pi if direction == "counterclockwise" else -two_pi
    else:
        sweep = cw if direction == "clockwise" else ccw

    arc_len = abs(sweep) * radius
    steps = max(12, min(720, int(max(1.0, arc_len / 0.05))))
    points = []
    for i in range(steps + 1):
        t = i / steps
        a = a0 + (sweep * t)
        points.append((cx + (radius * math.cos(a)), cy + (radius * math.sin(a))))
    return points


def _as_line_primitives(geom, width: float) -> list[Any]:
    primitives: list[Any] = []
    for line in _iter_lines(geom):
        coords = list(getattr(line, "coords", []) or [])
        if len(coords) < 2:
            continue
        for i in range(len(coords) - 1):
            start = coords[i]
            end = coords[i + 1]
            primitives.append(
                Line(
                    start=(float(start[0]), float(start[1])),
                    end=(float(end[0]), float(end[1])),
                    diameter=max(width, 0.02),
                    level_polarity="dark",
                )
            )
    return primitives


def _iter_lines(geom):
    if geom is None or geom.is_empty:
        return
    gt = geom.geom_type
    if gt in {"LineString", "LinearRing"}:
        yield geom
        return
    if gt == "Polygon":
        yield geom.exterior
        for interior in geom.interiors:
            yield interior
        return
    for sub in getattr(geom, "geoms", []):
        yield from _iter_lines(sub)


def _minimum_copper_gap(copper) -> float | None:
    parts = _polygon_parts(copper)
    if len(parts) < 2:
        return None

    # Keep runtime bounded on extremely fragmented geometry.
    if len(parts) > 1200:
        return None

    min_gap = float("inf")
    checks = 0
    max_checks = 250_000
    bounds = [p.bounds for p in parts]

    for i in range(len(parts) - 1):
        b0 = bounds[i]
        for j in range(i + 1, len(parts)):
            checks += 1
            if checks > max_checks:
                return None if not math.isfinite(min_gap) else min_gap
            b1 = bounds[j]
            lower = _bounds_distance_lower_bound(b0, b1)
            if lower >= min_gap:
                continue
            d = float(parts[i].distance(parts[j]))
            if d < min_gap:
                min_gap = d
                if min_gap <= 0.0:
                    return 0.0

    return None if not math.isfinite(min_gap) else min_gap


def _polygon_parts(geom) -> list[Any]:
    if geom is None or geom.is_empty:
        return []
    gt = geom.geom_type
    if gt == "Polygon":
        return [geom]
    out: list[Any] = []
    for g in getattr(geom, "geoms", []) or []:
        out.extend(_polygon_parts(g))
    return out


def _bounds_distance_lower_bound(
    b0: tuple[float, float, float, float],
    b1: tuple[float, float, float, float],
) -> float:
    a_minx, a_miny, a_maxx, a_maxy = b0
    b_minx, b_miny, b_maxx, b_maxy = b1
    dx = max(0.0, b_minx - a_maxx, a_minx - b_maxx)
    dy = max(0.0, b_miny - a_maxy, a_miny - b_maxy)
    if dx <= 0.0 and dy <= 0.0:
        return 0.0
    return math.hypot(dx, dy)


def _vertices(value: Any) -> list[tuple[float, float]]:
    if value is None:
        return []
    out = []
    for v in value:
        try:
            out.append((float(v[0]), float(v[1])))
        except Exception:
            continue
    return out


def _point_tuple(value: Any) -> tuple[float, float] | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return float(value[0]), float(value[1])
        except Exception:
            return None
    for xn, yn in (("x", "y"), ("real", "imag")):
        if hasattr(value, xn) and hasattr(value, yn):
            try:
                return float(getattr(value, xn)), float(getattr(value, yn))
            except Exception:
                continue
    return None


def _positive_float(value: Any) -> float | None:
    try:
        out = float(value)
        return out if out > 0.0 else None
    except Exception:
        return None


def _bounds_tuple(bounds: tuple[float, float, float, float] | None):
    if not bounds:
        return None
    x0, y0, x1, y1 = bounds
    return ((float(x0), float(x1)), (float(y0), float(y1)))


def _bbox_from_bounds(bounds: tuple[tuple[float, float], tuple[float, float]] | None):
    if not bounds:
        return None
    return (bounds[0][0], bounds[1][0], bounds[0][1], bounds[1][1])


def _iso_type_label(value: int) -> str:
    if value == 0:
        return "exterior"
    if value == 1:
        return "interior"
    return "both"


def _apply_layer_transform(geom, layer: Layer):
    if geom is None or getattr(geom, "is_empty", False):
        return geom
    out = geom
    if bool(getattr(layer, "mirror_x", False)) or bool(getattr(layer, "mirror_y", False)):
        sx = -1.0 if bool(getattr(layer, "mirror_x", False)) else 1.0
        sy = -1.0 if bool(getattr(layer, "mirror_y", False)) else 1.0
        out = affinity.scale(out, xfact=sx, yfact=sy, origin=(0.0, 0.0))
    rot = float(getattr(layer, "rotation_deg", 0.0) or 0.0)
    if abs(rot) > 1e-9:
        out = affinity.rotate(out, rot, origin=(0.0, 0.0), use_radians=False)
    dx = float(getattr(layer, "offset_x_mm", 0.0) or 0.0)
    dy = float(getattr(layer, "offset_y_mm", 0.0) or 0.0)
    if abs(dx) > 1e-12 or abs(dy) > 1e-12:
        out = affinity.translate(out, xoff=dx, yoff=dy)
    return out
