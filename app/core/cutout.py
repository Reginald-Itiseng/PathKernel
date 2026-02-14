from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import math

from shapely import affinity
from shapely.geometry import LineString, Polygon
from shapely.ops import linemerge, polygonize, unary_union

from app.core.io import Line
from app.core.project import Layer, Project


@dataclass(slots=True)
class CutoutParams:
    tool_diameter_mm: float
    compensation: str = "outside"  # "outside" | "inside" | "onpath"
    loop_compensations: list[str] | None = None


@dataclass(slots=True)
class _DerivedSource:
    units: str
    primitives: list[Any]
    bounds: tuple[tuple[float, float], tuple[float, float]] | None = None


def build_cutout_toolpath_layer(
    source_layer: Layer,
    project: Project,
    params: CutoutParams,
    log: callable | None = None,
) -> Layer:
    if source_layer.role != "cutout":
        raise ValueError("Cutout toolpath can only be generated from a layer with role 'cutout'.")
    if params.tool_diameter_mm <= 0.0:
        raise ValueError("Tool diameter must be greater than zero.")

    _log(log, "cutout: extracting closed loops")
    loops = extract_cutout_loops(source_layer, log=log)
    if not loops:
        raise ValueError("Could not derive a closed board outline from this layer.")

    default_comp = _normalize_compensation(params.compensation)
    loop_compensations = list(params.loop_compensations or [])
    if len(loop_compensations) < len(loops):
        loop_compensations.extend([default_comp] * (len(loops) - len(loop_compensations)))
    loop_compensations = [_normalize_compensation(c) for c in loop_compensations[: len(loops)]]

    radius = params.tool_diameter_mm * 0.5
    path_geoms = []
    resolved_comp: list[str] = []
    for loop, comp in zip(loops, loop_compensations):
        _log(log, f"cutout: applying compensation '{comp}'")
        path_geom = _compensated_path(loop, radius=radius, compensation=comp)
        if path_geom is None or path_geom.is_empty:
            raise ValueError(f"Compensation '{comp}' produced no valid toolpath for one closed loop.")
        path_geoms.append(path_geom)
        resolved_comp.append(comp)

    _log(log, "cutout: converting paths to primitives")
    primitives = _line_primitives_from_paths(path_geoms, params.tool_diameter_mm)
    if not primitives:
        raise ValueError("Generated cutout toolpath is empty.")

    merged = unary_union(path_geoms)
    min_x, min_y, max_x, max_y = merged.bounds
    bounds = ((float(min_x), float(max_x)), (float(min_y), float(max_y)))
    meta = {
        "name": f"{source_layer.name}_cutout_tp",
        "kind": "cutout_toolpath",
        "path": str(source_layer.path),
        "units": "mm",
        "derived_from": source_layer.name,
        "source_kind": source_layer.kind,
        "tool_diameter_mm": f"{params.tool_diameter_mm:.6f}",
        "compensation": ",".join(resolved_comp),
        "loop_count": str(len(loops)),
    }
    return Layer(
        name=f"{source_layer.name}_cutout_tp",
        path=Path(str(source_layer.path)),
        kind="geometry",
        source=_DerivedSource(units="mm", primitives=primitives, bounds=bounds),
        color="#8DFF57",
        opacity=1.0,
        role="cutout",
        bbox=(bounds[0][0], bounds[1][0], bounds[0][1], bounds[1][1]),
        metadata=meta,
    )


def extract_cutout_loops(layer: Layer, log: callable | None = None) -> list[Polygon]:
    """Return closed outline loops sorted by area descending."""
    _log(log, "cutout: collecting outline centerline segments")
    segments = _collect_outline_centerline_segments(layer)
    if not segments:
        return []

    _log(log, "cutout: snapping near endpoints")
    snap_tol = _estimate_snap_tolerance(segments)
    snapped_segments = _snap_segment_endpoints(segments, tol=snap_tol)
    _log(log, "cutout: merging and polygonizing segments")
    merged = linemerge(unary_union(snapped_segments))
    loops: list[Polygon] = []
    loops.extend(_polygons_from_near_closed_lines(merged))

    loops.extend([poly for poly in polygonize(merged)])

    # Fallback for almost-closed loops with tiny endpoint gaps.
    try:
        _log(log, "cutout: running healed-loop fallback")
        healed = unary_union(segments).buffer(0.03).buffer(-0.03)
        if healed.geom_type == "Polygon":
            loops.append(healed)
        healed_polys = [g for g in getattr(healed, "geoms", []) if g.geom_type == "Polygon"]
        if healed_polys:
            loops.extend(healed_polys)
    except Exception:
        pass

    # Fallback: some files may expose explicit filled region outlines as polygon primitives.
    candidates: list[Polygon] = []
    _log(log, "cutout: checking primitive polygon fallback")
    for primitive in getattr(layer.source, "primitives", []) or []:
        if hasattr(primitive, "vertices"):
            verts = _vertices(getattr(primitive, "vertices", None))
            if len(verts) >= 3:
                poly = _safe_polygon(verts)
                if poly is not None and not poly.is_empty:
                    poly = _apply_layer_transform(poly, layer)
                    loops.append(poly)

    if not loops:
        return []

    loops = _dedupe_polygons(loops)
    normalized = _normalize_loops_preserve_nesting(loops)

    # Drop tiny numeric artifacts while preserving real nested holes.
    filtered = _filter_meaningful_loops(normalized)
    filtered.sort(key=lambda p: p.area, reverse=True)
    _log(log, f"cutout: found {len(filtered)} closed loop(s)")
    return filtered


def _normalize_loops_preserve_nesting(polygons: list[Polygon]) -> list[Polygon]:
    out: list[Polygon] = []
    for poly in polygons:
        if poly is None or poly.is_empty:
            continue
        gt = getattr(poly, "geom_type", "")
        if gt == "Polygon":
            out.extend(_ring_polygons(poly))
            continue
        if gt == "MultiPolygon":
            for sub in getattr(poly, "geoms", []):
                if sub is None or sub.is_empty:
                    continue
                out.extend(_ring_polygons(sub))
    return _dedupe_polygons(out)


def _ring_polygons(poly: Polygon) -> list[Polygon]:
    out: list[Polygon] = []
    try:
        ext = Polygon(poly.exterior)
        if not ext.is_empty and ext.area > 1e-8:
            out.append(ext)
    except Exception:
        pass
    for ring in getattr(poly, "interiors", []):
        try:
            rp = Polygon(ring)
        except Exception:
            continue
        if not rp.is_empty and rp.area > 1e-8:
            out.append(rp)
    return out


def _estimate_snap_tolerance(segments: list[LineString]) -> float:
    lengths = [float(s.length) for s in segments if s is not None and not s.is_empty and s.length > 0.0]
    if not lengths:
        return 1e-3
    ref = min(lengths)
    # Dynamic tolerance to close endpoint gaps without distorting geometry.
    return max(1e-4, min(0.05, ref * 0.002))


def _snap_segment_endpoints(segments: list[LineString], *, tol: float) -> list[LineString]:
    endpoint_pts: list[tuple[float, float]] = []
    seg_endpoint_idx: list[tuple[int, int, list[tuple[float, float]]]] = []

    for seg in segments:
        coords = list(getattr(seg, "coords", []) or [])
        if len(coords) < 2:
            continue
        s_idx = len(endpoint_pts)
        endpoint_pts.append((float(coords[0][0]), float(coords[0][1])))
        e_idx = len(endpoint_pts)
        endpoint_pts.append((float(coords[-1][0]), float(coords[-1][1])))
        mid = [(float(x), float(y)) for x, y in coords[1:-1]]
        seg_endpoint_idx.append((s_idx, e_idx, mid))

    n = len(endpoint_pts)
    if n == 0:
        return []

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra = find(a)
        rb = find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        xi, yi = endpoint_pts[i]
        for j in range(i + 1, n):
            xj, yj = endpoint_pts[j]
            if math.hypot(xi - xj, yi - yj) <= tol:
                union(i, j)

    clusters: dict[int, list[tuple[float, float]]] = {}
    for idx, pt in enumerate(endpoint_pts):
        root = find(idx)
        clusters.setdefault(root, []).append(pt)

    rep: dict[int, tuple[float, float]] = {}
    for root, pts in clusters.items():
        sx = sum(p[0] for p in pts)
        sy = sum(p[1] for p in pts)
        rep[root] = (sx / len(pts), sy / len(pts))

    out: list[LineString] = []
    for s_idx, e_idx, mid in seg_endpoint_idx:
        s = rep[find(s_idx)]
        e = rep[find(e_idx)]
        if mid:
            out.append(LineString([s, *mid, e]))
        else:
            out.append(LineString([s, e]))
    return out


def _dedupe_polygons(polygons: list[Polygon], area_tol: float = 1e-5, pt_tol: float = 1e-3) -> list[Polygon]:
    out: list[Polygon] = []
    sigs: list[tuple[float, float, float]] = []
    for poly in polygons:
        if poly is None or poly.is_empty:
            continue
        rp = poly.representative_point()
        sig = (round(poly.area / area_tol) * area_tol, rp.x, rp.y)
        duplicate = False
        for area_sig, x_sig, y_sig in sigs:
            if abs(sig[0] - area_sig) <= area_tol and math.hypot(sig[1] - x_sig, sig[2] - y_sig) <= pt_tol:
                duplicate = True
                break
        if duplicate:
            continue
        sigs.append(sig)
        out.append(poly)
    return out


def _filter_meaningful_loops(polygons: list[Polygon]) -> list[Polygon]:
    if not polygons:
        return []
    max_area = max((float(p.area) for p in polygons if p is not None and not p.is_empty), default=0.0)
    max_perimeter = max((float(p.exterior.length) for p in polygons if p is not None and not p.is_empty), default=0.0)

    # Relative thresholds keep nested small loops but remove sliver artifacts.
    min_area = max(1e-6, max_area * 1e-5)
    min_perimeter = max(1e-3, max_perimeter * 1e-4)

    out: list[Polygon] = []
    for poly in polygons:
        if poly is None or poly.is_empty:
            continue
        if float(poly.area) < min_area:
            continue
        if float(poly.exterior.length) < min_perimeter:
            continue
        out.append(poly)
    return out


def _polygons_from_near_closed_lines(geom, tol: float = 1e-3) -> list[Polygon]:
    candidates: list[Polygon] = []
    lines = []
    if geom is None:
        return None
    if geom.geom_type == "LineString":
        lines = [geom]
    elif geom.geom_type == "LinearRing":
        lines = [LineString(list(geom.coords))]
    elif geom.geom_type == "MultiLineString":
        lines = list(getattr(geom, "geoms", []))

    for ln in lines:
        coords = list(getattr(ln, "coords", []) or [])
        if len(coords) < 4:
            continue
        sx, sy = coords[0]
        ex, ey = coords[-1]
        if math.hypot(ex - sx, ey - sy) > tol:
            continue
        ring = [*coords]
        ring[-1] = ring[0]
        poly = _safe_polygon([(float(x), float(y)) for x, y in ring])
        if poly is not None and not poly.is_empty:
            candidates.append(poly)

    return candidates


def _collect_outline_centerline_segments(layer: Layer) -> list[LineString]:
    segments: list[LineString] = []
    for primitive in getattr(layer.source, "primitives", []) or []:
        cls = primitive.__class__.__name__.lower()

        if cls in {"line", "slot"}:
            start = _point_tuple(getattr(primitive, "start", None))
            end = _point_tuple(getattr(primitive, "end", None))
            if start and end:
                segments.append(LineString([start, end]))
            continue

        if cls == "arc":
            pts = _arc_xy_points(primitive)
            if len(pts) >= 2:
                segments.append(LineString(pts))
            continue

        if cls == "polygon":
            verts = _vertices(getattr(primitive, "vertices", None))
            if len(verts) >= 3:
                segments.append(LineString(_ensure_closed(verts)))
            continue

        if cls == "region":
            verts = _vertices(getattr(primitive, "vertices", None))
            if len(verts) >= 3:
                segments.append(LineString(_ensure_closed(verts)))
                continue
            # region primitives are sometimes outlined as segments/arcs
            for sub in getattr(primitive, "primitives", []) or []:
                sub_cls = sub.__class__.__name__.lower()
                if sub_cls in {"line", "slot"}:
                    start = _point_tuple(getattr(sub, "start", None))
                    end = _point_tuple(getattr(sub, "end", None))
                    if start and end:
                        segments.append(LineString([start, end]))
                elif sub_cls == "arc":
                    pts = _arc_xy_points(sub)
                    if len(pts) >= 2:
                        segments.append(LineString(pts))

    return [_apply_layer_transform(seg, layer) for seg in segments if seg is not None and not seg.is_empty]


def _line_primitives_from_paths(path_geoms: list[Any], tool_dia: float) -> list[Any]:
    out: list[Any] = []
    for path_geom in path_geoms:
        out.extend(_line_primitives_from_path(path_geom, tool_dia))
    return out


def _line_primitives_from_path(path_geom, tool_dia: float) -> list[Any]:
    coords = list(getattr(path_geom, "coords", []) or [])
    if len(coords) < 2:
        return []
    out: list[Any] = []
    for i in range(len(coords) - 1):
        a = coords[i]
        b = coords[i + 1]
        out.append(
            Line(
                start=(float(a[0]), float(a[1])),
                end=(float(b[0]), float(b[1])),
                diameter=max(tool_dia, 0.02),
                level_polarity="dark",
            )
        )
    return out


def _normalize_compensation(value: str) -> str:
    comp = (value or "").strip().lower()
    if comp not in {"outside", "inside", "onpath"}:
        return "outside"
    return comp


def _compensated_path(loop: Polygon, *, radius: float, compensation: str):
    comp = _normalize_compensation(compensation)
    if comp == "outside":
        return loop.buffer(radius).exterior
    if comp == "inside":
        inside = loop.buffer(-radius)
        if inside.is_empty:
            return None
        if inside.geom_type == "Polygon":
            return inside.exterior
        polys = [g for g in getattr(inside, "geoms", []) if g.geom_type == "Polygon"]
        if not polys:
            return None
        return max(polys, key=lambda p: p.area).exterior
    return loop.exterior


def _ensure_closed(verts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not verts:
        return verts
    if abs(verts[0][0] - verts[-1][0]) <= 1e-9 and abs(verts[0][1] - verts[-1][1]) <= 1e-9:
        return verts
    return [*verts, verts[0]]


def _safe_polygon(verts: list[tuple[float, float]]) -> Polygon | None:
    try:
        poly = Polygon(verts)
    except Exception:
        return None
    if poly.is_empty:
        return None
    if poly.is_valid:
        return poly
    try:
        repaired = poly.buffer(0)
        return repaired if not repaired.is_empty else None
    except Exception:
        return None


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


def _log(log: callable | None, msg: str) -> None:
    if log is None:
        return
    try:
        log(msg)
    except Exception:
        pass
