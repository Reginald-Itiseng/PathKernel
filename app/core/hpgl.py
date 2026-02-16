from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable

from app.core.project import Layer


@dataclass(slots=True)
class HPGLExportOptions:
    pen_number: int = 1
    units_per_mm: float = 40.0  # HPGL default: 40 plotter units per mm
    normalize_to_origin: bool = True
    arc_chord_mm: float = 0.05
    stitch_tolerance_mm: float = 1e-6
    # Safety cap for packed PD coordinate pairs per command to avoid parser buffer limits.
    pd_batch_pairs: int = 12


@dataclass(slots=True)
class HPGLExportResult:
    hpgl_text: str
    polyline_count: int
    segment_count: int
    point_count: int
    offset_x_mm: float
    offset_y_mm: float


_EPS = 1e-9


def is_toolpath_layer(layer: Layer) -> bool:
    meta = getattr(layer, "metadata", {}) or {}
    kind = str(meta.get("kind", "")).strip().lower()
    if kind in {"isolation", "cutout_toolpath", "drill_toolpath", "toolpath"}:
        return True
    if str(meta.get("derived_from", "")).strip() and layer.kind == "geometry":
        return True
    return False


def export_layers_to_hpgl(
    layers: Iterable[Layer],
    options: HPGLExportOptions | None = None,
) -> HPGLExportResult:
    opts = options or HPGLExportOptions()
    units_per_mm = max(1e-9, float(opts.units_per_mm))
    chord_mm = max(1e-4, float(opts.arc_chord_mm))
    stitch_tol = max(1e-9, float(opts.stitch_tolerance_mm))

    toolpaths: list[dict[str, Any]] = []
    all_polylines: list[list[tuple[float, float]]] = []
    for layer in layers:
        # Convert each layer to centerline polylines first. Width visualization stays in the UI;
        # exporter always emits centerline motion so machine compensation is deterministic.
        polylines = _layer_polylines(layer, chord_mm=chord_mm, stitch_tol=stitch_tol)
        if not polylines:
            continue
        all_polylines.extend(polylines)
        toolpaths.append(
            {
                "tool_number": _layer_tool_number(layer, default=opts.pen_number),
                "speed_mm_min": _layer_speed_mm_min(layer),
                "rounded_speed_mm_min": _layer_rounded_speed_mm_min(layer),
                "strokes": [{"type": "polyline", "points": poly} for poly in polylines],
            }
        )

    if not toolpaths:
        raise ValueError("No exportable toolpath geometry found.")

    min_x = float("inf")
    min_y = float("inf")
    for poly in all_polylines:
        for x, y in poly:
            min_x = min(min_x, x)
            min_y = min(min_y, y)

    shift_x = -min_x if opts.normalize_to_origin and math.isfinite(min_x) else 0.0
    shift_y = -min_y if opts.normalize_to_origin and math.isfinite(min_y) else 0.0

    if abs(shift_x) > _EPS or abs(shift_y) > _EPS:
        # Shift at stroke level so metadata/tool grouping remain unchanged.
        shifted_toolpaths: list[dict[str, Any]] = []
        for tp in toolpaths:
            shifted_strokes: list[dict[str, Any]] = []
            for stroke in list(tp.get("strokes", []) or []):
                shifted_strokes.append(_shift_stroke(stroke, shift_x=shift_x, shift_y=shift_y))
            shifted = dict(tp)
            shifted["strokes"] = shifted_strokes
            shifted_toolpaths.append(shifted)
        toolpaths = shifted_toolpaths

    point_count = 0
    segment_count = 0
    for poly in all_polylines:
        shifted = [(x + shift_x, y + shift_y) for x, y in poly]
        ints = [(_mm_to_hpgl_units(x, units_per_mm), _mm_to_hpgl_units(y, units_per_mm)) for x, y in shifted]
        if len(ints) < 2:
            continue
        point_count += len(ints)
        segment_count += len(ints) - 1

    text = export_to_bungard(toolpaths, units_per_mm=units_per_mm, pd_batch_pairs=opts.pd_batch_pairs)
    return HPGLExportResult(
        hpgl_text=text,
        polyline_count=len(all_polylines),
        segment_count=segment_count,
        point_count=point_count,
        offset_x_mm=shift_x,
        offset_y_mm=shift_y,
    )


def _mm_to_hpgl_units(value_mm: float, units_per_mm: float) -> int:
    return int(round(float(value_mm) * units_per_mm))


def export_to_bungard(
    toolpaths: Iterable[Any],
    *,
    units_per_mm: float = 40.0,
    default_tool: int = 1,
    pd_batch_pairs: int = 12,
) -> str:
    """
    Convert toolpath commands to Bungard-compatible HPGL.

    Rules implemented:
    - 40x coordinate scaling to integer plotter units.
    - Header IN; and absolute mode PA;.
    - Modal pen motion with PU/PD and explicit plunge command PD;.
    - Tool changes with SP<T>;.
    - Speed commands VS<V>; where V is cm/s from mm/min input.
    - Arc support with AA<CX>,<CY>,<A12>; absolute arc.
    - Packed PD coordinate batching for smoother continuous motion.
    - Footer PU0,0;SP0;.
    """
    scale = max(1e-9, float(units_per_mm))
    batch_pairs = max(1, int(pd_batch_pairs))
    normalized = _normalize_toolpaths(toolpaths, default_tool=default_tool)
    if not normalized:
        raise ValueError("No toolpaths to export.")

    out: list[str] = ["IN;", "PA;"]
    current_tool: int | None = None
    current_vs: float | None = None
    current_pos: tuple[int, int] | None = None
    pen_down = False

    for tp in normalized:
        tool_number = max(1, int(tp.get("tool_number", default_tool)))
        if current_tool != tool_number:
            if pen_down:
                out.append("PU;")
                pen_down = False
            out.append(f"SP{tool_number};")
            current_tool = tool_number

        speed_mm_min = tp.get("speed_mm_min")
        rounded_speed_mm_min = tp.get("rounded_speed_mm_min")
        base_vs: float | None = None
        round_vs: float | None = None
        if speed_mm_min is not None:
            base_vs = _mm_min_to_cm_s(float(speed_mm_min))
            current_vs = _emit_vs_if_changed(out, current_vs, base_vs)
        if rounded_speed_mm_min is not None:
            round_vs = _mm_min_to_cm_s(float(rounded_speed_mm_min))
            if base_vs is not None and round_vs <= base_vs:
                round_vs = None

        for stroke in tp.get("strokes", []):
            stype = str(stroke.get("type", "")).strip().lower()
            if stype == "polyline":
                points = [p for p in (_point_tuple(v) for v in list(stroke.get("points", []) or [])) if p is not None]
                if len(points) < 2:
                    continue
                start_i = (_mm_to_hpgl_units(points[0][0], scale), _mm_to_hpgl_units(points[0][1], scale))
                if pen_down or current_pos != start_i:
                    out.append(f"PU{start_i[0]},{start_i[1]};")
                    pen_down = False
                if not pen_down:
                    out.append("PD;")
                    pen_down = True
                curved_flags = _polyline_curved_segment_flags(points)
                pd_batch: list[tuple[int, int]] = []
                pd_batch_vs: float | None = None
                for seg_idx, (px, py) in enumerate(points[1:]):
                    is_curved = bool(seg_idx < len(curved_flags) and curved_flags[seg_idx])
                    target_vs = round_vs if is_curved and round_vs is not None else base_vs
                    speed_changed = (
                        ((target_vs is None) != (pd_batch_vs is None))
                        or (
                            target_vs is not None
                            and pd_batch_vs is not None
                            and abs(float(target_vs) - float(pd_batch_vs)) > _EPS
                        )
                    )
                    if pd_batch and speed_changed:
                        _append_pd_batch(out, pd_batch)
                        pd_batch = []
                        pd_batch_vs = None
                    if target_vs is not None:
                        current_vs = _emit_vs_if_changed(out, current_vs, target_vs)
                    if not pd_batch:
                        pd_batch_vs = target_vs
                    xi = _mm_to_hpgl_units(px, scale)
                    yi = _mm_to_hpgl_units(py, scale)
                    pd_batch.append((xi, yi))
                    if len(pd_batch) >= batch_pairs:
                        _append_pd_batch(out, pd_batch)
                        pd_batch = []
                        pd_batch_vs = None
                    current_pos = (xi, yi)
                if pd_batch:
                    _append_pd_batch(out, pd_batch)
                continue

            if stype == "line":
                start = _point_tuple(stroke.get("start"))
                end = _point_tuple(stroke.get("end"))
                if start is None or end is None:
                    continue
                start_i = (_mm_to_hpgl_units(start[0], scale), _mm_to_hpgl_units(start[1], scale))
                if pen_down or current_pos != start_i:
                    out.append(f"PU{start_i[0]},{start_i[1]};")
                    pen_down = False
                if not pen_down:
                    out.append("PD;")
                    pen_down = True
                if base_vs is not None:
                    current_vs = _emit_vs_if_changed(out, current_vs, base_vs)
                end_i = (_mm_to_hpgl_units(end[0], scale), _mm_to_hpgl_units(end[1], scale))
                _append_pd_batch(out, [end_i])
                current_pos = end_i
                continue

            if stype == "arc":
                start = _point_tuple(stroke.get("start"))
                end = _point_tuple(stroke.get("end"))
                center = _point_tuple(stroke.get("center"))
                if center is None:
                    continue
                sweep_deg = _stroke_arc_sweep_deg(stroke, start=start, end=end, center=center)
                if sweep_deg is None:
                    continue

                if start is not None:
                    start_i = (_mm_to_hpgl_units(start[0], scale), _mm_to_hpgl_units(start[1], scale))
                    if pen_down or current_pos != start_i:
                        out.append(f"PU{start_i[0]},{start_i[1]};")
                        pen_down = False
                if not pen_down:
                    out.append("PD;")
                    pen_down = True
                target_vs = round_vs if round_vs is not None else base_vs
                if target_vs is not None:
                    current_vs = _emit_vs_if_changed(out, current_vs, target_vs)
                cx_i = _mm_to_hpgl_units(center[0], scale)
                cy_i = _mm_to_hpgl_units(center[1], scale)
                out.append(f"AA{cx_i},{cy_i},{_format_hpgl_float(sweep_deg)};")
                if end is not None:
                    current_pos = (_mm_to_hpgl_units(end[0], scale), _mm_to_hpgl_units(end[1], scale))
                else:
                    current_pos = None
                continue

    if pen_down:
        out.append("PU;")
    out.append("PU0,0;SP0;")
    return "\n".join(out) + "\n"


def _layer_polylines(layer: Layer, *, chord_mm: float, stitch_tol: float) -> list[list[tuple[float, float]]]:
    primitives = list(getattr(getattr(layer, "source", None), "primitives", []) or [])
    if not primitives:
        return []

    polylines: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []

    for primitive in primitives:
        # `primitive_segments` returns one or more local segments. We transform to project space
        # immediately so downstream stitching/export uses the same coordinates as the viewport.
        segs = _primitive_segments(primitive, chord_mm=chord_mm)
        for seg in segs:
            if len(seg) < 2:
                continue
            transformed = [_transform_point(layer, x, y) for x, y in seg]
            transformed = _dedupe_points(transformed)
            if len(transformed) < 2:
                continue

            if not current:
                current = list(transformed)
                continue

            # Stitch only when endpoints match within tolerance to preserve discontinuities
            # between independent toolpaths.
            if _points_close(current[-1], transformed[0], stitch_tol):
                current.extend(transformed[1:])
                continue
            if _points_close(current[-1], transformed[-1], stitch_tol):
                rev = list(reversed(transformed))
                current.extend(rev[1:])
                continue

            if len(current) >= 2:
                polylines.append(current)
            current = list(transformed)

    if len(current) >= 2:
        polylines.append(current)
    return polylines


def _normalize_toolpaths(toolpaths: Iterable[Any], *, default_tool: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in toolpaths:
        if isinstance(entry, Layer):
            strokes = [{"type": "polyline", "points": poly} for poly in _layer_polylines(entry, chord_mm=0.05, stitch_tol=1e-6)]
            if not strokes:
                continue
            out.append(
                {
                    "tool_number": _layer_tool_number(entry, default=default_tool),
                    "speed_mm_min": _layer_speed_mm_min(entry),
                    "rounded_speed_mm_min": _layer_rounded_speed_mm_min(entry),
                    "strokes": strokes,
                }
            )
            continue

        if not isinstance(entry, dict):
            continue
        strokes_in = list(entry.get("strokes", []) or entry.get("segments", []) or entry.get("moves", []) or [])
        strokes: list[dict[str, Any]] = []
        for raw in strokes_in:
            st = _normalize_stroke(raw)
            if st is not None:
                strokes.append(st)
        if not strokes:
            continue

        tool_number = _as_int(entry.get("tool_number"), None)
        if tool_number is None:
            tool_number = _as_int(entry.get("tool"), None)
        if tool_number is None:
            tool_number = _as_int(entry.get("pen"), default_tool)

        speed_mm_min: float | None = None
        if "speed_mm_min" in entry:
            speed_mm_min = _as_float(entry.get("speed_mm_min"), None)
        elif "feed_mm_min" in entry:
            speed_mm_min = _as_float(entry.get("feed_mm_min"), None)
        elif "speed_mm_s" in entry:
            mm_s = _as_float(entry.get("speed_mm_s"), None)
            speed_mm_min = (mm_s * 60.0) if mm_s is not None else None

        rounded_speed_mm_min: float | None = None
        if "rounded_speed_mm_min" in entry:
            rounded_speed_mm_min = _as_float(entry.get("rounded_speed_mm_min"), None)
        elif "rounded_speed_mm_s" in entry:
            rounded_mm_s = _as_float(entry.get("rounded_speed_mm_s"), None)
            rounded_speed_mm_min = (rounded_mm_s * 60.0) if rounded_mm_s is not None else None

        out.append(
            {
                "tool_number": max(1, int(tool_number)),
                "speed_mm_min": speed_mm_min,
                "rounded_speed_mm_min": rounded_speed_mm_min,
                "strokes": strokes,
            }
        )
    return out


def _normalize_stroke(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        stype = str(raw.get("type", "")).strip().lower()
        if stype == "polyline":
            points = [p for p in (_point_tuple(v) for v in list(raw.get("points", []) or [])) if p is not None]
            if len(points) >= 2:
                return {"type": "polyline", "points": points}
            return None
        if stype == "line":
            start = _point_tuple(raw.get("start"))
            end = _point_tuple(raw.get("end"))
            if start is None or end is None:
                return None
            return {"type": "line", "start": start, "end": end}
        if stype == "arc":
            center = _point_tuple(raw.get("center"))
            if center is None:
                return None
            out = {"type": "arc", "center": center}
            start = _point_tuple(raw.get("start"))
            end = _point_tuple(raw.get("end"))
            if start is not None:
                out["start"] = start
            if end is not None:
                out["end"] = end
            if "sweep_deg" in raw:
                sweep = _as_float(raw.get("sweep_deg"), None)
                if sweep is not None:
                    out["sweep_deg"] = sweep
            if "angle" in raw and "sweep_deg" not in out:
                sweep = _as_float(raw.get("angle"), None)
                if sweep is not None:
                    out["sweep_deg"] = sweep
            direction = str(raw.get("direction", "")).strip().lower()
            if direction:
                out["direction"] = direction
            return out
        # If dict has primitive-like coords fallback to line.
        start = _point_tuple(raw.get("start"))
        end = _point_tuple(raw.get("end"))
        if start is not None and end is not None:
            return {"type": "line", "start": start, "end": end}
        return None

    cls = raw.__class__.__name__.lower()
    if cls in {"line", "slot"}:
        start = _point_tuple(getattr(raw, "start", None))
        end = _point_tuple(getattr(raw, "end", None))
        if start is None or end is None:
            return None
        return {"type": "line", "start": start, "end": end}
    if cls == "arc":
        center = _point_tuple(getattr(raw, "center", None))
        start = _point_tuple(getattr(raw, "start", None))
        end = _point_tuple(getattr(raw, "end", None))
        if center is None:
            return None
        out = {"type": "arc", "center": center}
        if start is not None:
            out["start"] = start
        if end is not None:
            out["end"] = end
        direction = str(getattr(raw, "direction", "")).strip().lower()
        if direction:
            out["direction"] = direction
        return out
    if isinstance(raw, (list, tuple)):
        points = [p for p in (_point_tuple(v) for v in raw) if p is not None]
        if len(points) >= 2:
            return {"type": "polyline", "points": points}
    return None


def _stroke_arc_sweep_deg(
    stroke: dict[str, Any],
    *,
    start: tuple[float, float] | None,
    end: tuple[float, float] | None,
    center: tuple[float, float],
) -> float | None:
    explicit = _as_float(stroke.get("sweep_deg"), None)
    if explicit is not None:
        return explicit
    if start is None or end is None:
        return None

    sx, sy = start
    ex, ey = end
    cx, cy = center
    a0 = math.atan2(sy - cy, sx - cx)
    a1 = math.atan2(ey - cy, ex - cx)
    two_pi = 2.0 * math.pi
    ccw = (a1 - a0) % two_pi
    cw = ccw - two_pi
    direction = str(stroke.get("direction", "counterclockwise")).lower()
    if abs(sx - ex) <= _EPS and abs(sy - ey) <= _EPS:
        sweep = two_pi if direction != "clockwise" else -two_pi
    else:
        sweep = cw if direction == "clockwise" else ccw
    return math.degrees(sweep)


def _format_hpgl_float(value: float) -> str:
    text = f"{float(value):.6f}"
    text = text.rstrip("0").rstrip(".")
    return text if text else "0"


def _mm_min_to_cm_s(mm_min: float) -> float:
    # CopperCAM reference: VS uses cm/s, from mm/min via /60 then /10.
    return float(mm_min) / 600.0


def _emit_vs_if_changed(out: list[str], current_vs: float | None, target_vs: float) -> float:
    if current_vs is None or abs(float(target_vs) - float(current_vs)) > _EPS:
        out.append(f"VS{_format_hpgl_float(target_vs)};")
        return float(target_vs)
    return float(current_vs)


def _append_pd_batch(out: list[str], points: list[tuple[int, int]]) -> None:
    if not points:
        return
    coords = ",".join(f"{x},{y}" for x, y in points)
    out.append(f"PD{coords};")


def _layer_tool_number(layer: Layer, *, default: int) -> int:
    meta = getattr(layer, "metadata", {}) or {}
    for key in ("selected_cutting_tool", "selected_engraving_tool", "selected_drill_tool", "tool_number", "tool"):
        value = str(meta.get(key, "")).strip()
        if value.isdigit():
            return max(1, int(value))
    return max(1, int(default))


def _layer_speed_mm_min(layer: Layer) -> float | None:
    meta = getattr(layer, "metadata", {}) or {}
    for key in (
        "selected_cutting_speed_mm_s",
        "selected_engraving_speed_mm_s",
        "selected_hatching_speed_mm_s",
        "selected_drill_boring_speed_mm_s",
        "drill_boring_speed_mm_s",
    ):
        speed_mm_s = _as_float(meta.get(key), None)
        if speed_mm_s is not None and speed_mm_s > 0.0:
            return speed_mm_s * 60.0
    for key in (
        "speed_mm_min",
        "feed_mm_min",
        "selected_cutting_speed_mm_min",
        "selected_engraving_speed_mm_min",
        "selected_drill_boring_speed_mm_min",
        "drill_boring_speed_mm_min",
    ):
        speed_mm_min = _as_float(meta.get(key), None)
        if speed_mm_min is not None and speed_mm_min > 0.0:
            return speed_mm_min
    return None


def _layer_rounded_speed_mm_min(layer: Layer) -> float | None:
    """
    Speed used for curved/tessellated moves.
    Prefer hatching speed for isolation, otherwise optional rounded-speed keys.
    """
    meta = getattr(layer, "metadata", {}) or {}
    for key in ("selected_hatching_speed_mm_s", "rounded_speed_mm_s", "selected_rounded_speed_mm_s"):
        speed_mm_s = _as_float(meta.get(key), None)
        if speed_mm_s is not None and speed_mm_s > 0.0:
            return speed_mm_s * 60.0
    for key in ("selected_hatching_speed_mm_min", "rounded_speed_mm_min", "selected_rounded_speed_mm_min"):
        speed_mm_min = _as_float(meta.get(key), None)
        if speed_mm_min is not None and speed_mm_min > 0.0:
            return speed_mm_min
    return None


def _polyline_curved_segment_flags(points: list[tuple[float, float]]) -> list[bool]:
    """
    Return a per-segment flag list (len(points)-1) where True marks arc-like runs.
    Sharp corners are intentionally excluded.
    """
    seg_count = max(0, len(points) - 1)
    if seg_count <= 1:
        return [False] * seg_count

    flags = [False] * seg_count
    min_turn_deg = 0.25
    max_turn_deg = 35.0
    eps = 1e-12

    for i in range(1, len(points) - 1):
        ax, ay = points[i - 1]
        bx, by = points[i]
        cx, cy = points[i + 1]
        v1x = bx - ax
        v1y = by - ay
        v2x = cx - bx
        v2y = cy - by
        l1 = math.hypot(v1x, v1y)
        l2 = math.hypot(v2x, v2y)
        if l1 <= eps or l2 <= eps:
            continue
        cross = (v1x * v2y) - (v1y * v2x)
        dot = (v1x * v2x) + (v1y * v2y)
        turn = abs(math.degrees(math.atan2(abs(cross), dot)))
        if min_turn_deg <= turn <= max_turn_deg:
            flags[i] = True

    # Keep only continuous runs of at least 2 segments to avoid false positives near corners.
    start = 0
    while start < seg_count:
        if not flags[start]:
            start += 1
            continue
        end = start + 1
        while end < seg_count and flags[end]:
            end += 1
        if (end - start) < 2:
            for j in range(start, end):
                flags[j] = False
        start = end

    return flags


def _shift_stroke(stroke: dict[str, Any], *, shift_x: float, shift_y: float) -> dict[str, Any]:
    out = dict(stroke)
    stype = str(out.get("type", "")).strip().lower()
    if stype == "polyline":
        points = [p for p in (_point_tuple(v) for v in list(out.get("points", []) or [])) if p is not None]
        out["points"] = [(x + shift_x, y + shift_y) for x, y in points]
        return out
    if stype in {"line", "arc"}:
        start = _point_tuple(out.get("start"))
        end = _point_tuple(out.get("end"))
        center = _point_tuple(out.get("center"))
        if start is not None:
            out["start"] = (start[0] + shift_x, start[1] + shift_y)
        if end is not None:
            out["end"] = (end[0] + shift_x, end[1] + shift_y)
        if center is not None:
            out["center"] = (center[0] + shift_x, center[1] + shift_y)
    return out


def _primitive_segments(primitive: Any, *, chord_mm: float) -> list[list[tuple[float, float]]]:
    cls = primitive.__class__.__name__.lower()

    if cls in {"line", "slot"}:
        start = _point_tuple(getattr(primitive, "start", None))
        end = _point_tuple(getattr(primitive, "end", None))
        if start is not None and end is not None:
            return [[start, end]]
        return []

    if cls == "arc":
        pts = _arc_points(primitive, chord_mm=chord_mm)
        return [pts] if len(pts) >= 2 else []

    if cls in {"polygon", "region"}:
        vertices = _vertices(getattr(primitive, "vertices", None))
        if len(vertices) >= 3:
            return [_ensure_closed(vertices)]
        if cls == "region":
            out: list[list[tuple[float, float]]] = []
            for sub in getattr(primitive, "primitives", []) or []:
                out.extend(_primitive_segments(sub, chord_mm=chord_mm))
            return out
        return []

    if cls == "amgroup":
        out: list[list[tuple[float, float]]] = []
        for sub in getattr(primitive, "primitives", []) or []:
            out.extend(_primitive_segments(sub, chord_mm=chord_mm))
        return out

    start = _point_tuple(getattr(primitive, "start", None))
    end = _point_tuple(getattr(primitive, "end", None))
    if start is not None and end is not None:
        return [[start, end]]
    return []


def _arc_points(primitive: Any, *, chord_mm: float) -> list[tuple[float, float]]:
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
    steps = max(8, min(2000, int(math.ceil(max(1e-9, arc_len) / max(chord_mm, 1e-4)))))

    out: list[tuple[float, float]] = []
    for i in range(steps + 1):
        t = i / steps
        a = a0 + (sweep * t)
        out.append((cx + (radius * math.cos(a)), cy + (radius * math.sin(a))))
    return out


def _transform_point(layer: Layer, x: float, y: float) -> tuple[float, float]:
    sx = -1.0 if bool(getattr(layer, "mirror_x", False)) else 1.0
    sy = -1.0 if bool(getattr(layer, "mirror_y", False)) else 1.0
    tx = float(x) * sx
    ty = float(y) * sy

    rot_deg = float(getattr(layer, "rotation_deg", 0.0) or 0.0)
    if abs(rot_deg) > 1e-12:
        ang = math.radians(rot_deg)
        ca = math.cos(ang)
        sa = math.sin(ang)
        rx = (tx * ca) - (ty * sa)
        ry = (tx * sa) + (ty * ca)
        tx, ty = rx, ry

    tx += float(getattr(layer, "offset_x_mm", 0.0) or 0.0)
    ty += float(getattr(layer, "offset_y_mm", 0.0) or 0.0)
    return tx, ty


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


def _as_float(value: Any, default: float | None) -> float | None:
    try:
        return float(value)
    except Exception:
        return default


def _as_int(value: Any, default: int | None) -> int | None:
    try:
        return int(value)
    except Exception:
        return default


def _vertices(value: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    if value is None:
        return out
    for v in value:
        try:
            out.append((float(v[0]), float(v[1])))
        except Exception:
            continue
    return out


def _ensure_closed(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not points:
        return points
    if _points_close(points[0], points[-1], 1e-9):
        return points
    return [*points, points[0]]


def _points_close(a: tuple[float, float], b: tuple[float, float], tol: float) -> bool:
    return math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1])) <= float(tol)


def _dedupe_points(points: list[tuple[float, float]], tol: float = 1e-12) -> list[tuple[float, float]]:
    if not points:
        return points
    out = [points[0]]
    for pt in points[1:]:
        if not _points_close(out[-1], pt, tol):
            out.append(pt)
    return out
