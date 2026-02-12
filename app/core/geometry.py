from __future__ import annotations

"""Geometry conversion pipeline for viewport rendering.

This module converts pcb-tools primitives into normalized draw shapes used by
the Qt scene item painter. Keeping this logic outside UI classes makes it
testable and safer to evolve without breaking interaction code.
"""

from dataclasses import dataclass, field
import math
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPainterPath

from app.core.project import Layer, ManualEdit


SEGMENT_CHORD_MM = 0.05
MIN_ARC_SEGMENTS = 12
MAX_ARC_SEGMENTS = 720


@dataclass(slots=True)
class DrawShape:
    kind: str
    path: QPainterPath
    line_width: float
    clear: bool
    info: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class GeometryBuildResult:
    shapes: list[DrawShape]
    bounds: QRectF
    primitive_count: int
    shape_count: int
    unsupported: dict[str, int]
    unsupported_samples: list[str]


def build_layer_geometry(layer: Layer) -> GeometryBuildResult:
    shapes: list[DrawShape] = []
    primitives = getattr(layer.source, "primitives", [])
    unsupported: dict[str, int] = {}
    unsupported_samples: list[str] = []

    for primitive_index, primitive in enumerate(primitives):
        override = layer.primitive_overrides.get(primitive_index, {})
        if not _append_primitive_shapes(
            primitive,
            shapes,
            unsupported,
            primitive_index=primitive_index,
            override=override,
            layer_kind=layer.kind,
        ):
            name = primitive.__class__.__name__
            unsupported[name] = unsupported.get(name, 0) + 1
            if len(unsupported_samples) < 20:
                unsupported_samples.append(_describe_primitive(primitive))

    _append_manual_edit_shapes(layer, shapes)

    layer.metadata["primitive_count"] = str(len(primitives))
    layer.metadata["manual_edit_count"] = str(len(layer.manual_edits))
    layer.metadata["shape_count"] = str(len(shapes))
    if unsupported:
        layer.metadata["unsupported_primitives"] = ", ".join(
            f"{name}:{count}" for name, count in sorted(unsupported.items())
        )
        layer.metadata["unsupported_samples"] = " | ".join(unsupported_samples)
    else:
        layer.metadata.pop("unsupported_primitives", None)
        layer.metadata.pop("unsupported_samples", None)

    return GeometryBuildResult(
        shapes=shapes,
        bounds=_shapes_rect(shapes, layer),
        primitive_count=len(primitives),
        shape_count=len(shapes),
        unsupported=unsupported,
        unsupported_samples=unsupported_samples,
    )


def _append_manual_edit_shapes(layer: Layer, shapes: list[DrawShape]) -> None:
    for idx, edit in enumerate(layer.manual_edits):
        info = {
            "primitive_type": "ManualEdit",
            "edit_kind": edit.kind,
            "edit_index": str(idx),
            "shape": edit.shape,
        }
        if edit.kind == "pad":
            path = manual_pad_path(edit)
            if path is not None:
                shapes.append(DrawShape(kind="fill", path=path, line_width=0.0, clear=False, info=info))
                if edit.hole_diameter_mm > 0.0:
                    hole = _circle_poly_path(edit.x_mm, edit.y_mm, edit.hole_diameter_mm * 0.5)
                    shapes.append(DrawShape(kind="fill", path=hole, line_width=0.0, clear=True, info=info))
            continue
        if edit.kind == "hole":
            if edit.diameter_mm > 0.0:
                path = _circle_poly_path(edit.x_mm, edit.y_mm, edit.diameter_mm * 0.5)
                shapes.append(DrawShape(kind="fill", path=path, line_width=0.0, clear=True, info=info))
            continue
        if edit.kind == "track":
            path = QPainterPath(QPointF(edit.x_mm, -edit.y_mm))
            path.lineTo(edit.x2_mm, -edit.y2_mm)
            shapes.append(
                DrawShape(
                    kind="line",
                    path=path,
                    line_width=max(edit.width_mm, 0.02),
                    clear=False,
                    info=info,
                )
            )


def manual_pad_path(edit: ManualEdit) -> QPainterPath | None:
    shape = edit.shape.lower().strip()
    if shape == "circle":
        if edit.diameter_mm <= 0.0:
            return None
        return _circle_poly_path(edit.x_mm, edit.y_mm, edit.diameter_mm * 0.5)
    if shape == "rect":
        if edit.width_mm <= 0.0 or edit.height_mm <= 0.0:
            return None
        return _rounded_rect_poly_path(edit.x_mm, edit.y_mm, edit.width_mm, edit.height_mm, 0.0)
    if shape == "obround":
        if edit.width_mm <= 0.0 or edit.height_mm <= 0.0:
            return None
        return _rounded_rect_poly_path(
            edit.x_mm,
            edit.y_mm,
            edit.width_mm,
            edit.height_mm,
            min(edit.width_mm, edit.height_mm) * 0.5,
        )
    if shape == "roundrect":
        if edit.width_mm <= 0.0 or edit.height_mm <= 0.0:
            return None
        radius = max(0.0, min(edit.corner_radius_mm, edit.width_mm * 0.5, edit.height_mm * 0.5))
        return _rounded_rect_poly_path(edit.x_mm, edit.y_mm, edit.width_mm, edit.height_mm, radius)
    return None


def _layer_rect(layer: Layer) -> QRectF:
    if layer.bbox:
        min_x, min_y, max_x, max_y = layer.bbox
        return QRectF(min_x, -max_y, max(max_x - min_x, 1e-6), max(max_y - min_y, 1e-6))
    return QRectF(0.0, 0.0, 1.0, 1.0)


def _shapes_rect(shapes: list[DrawShape], layer: Layer) -> QRectF:
    if not shapes:
        return _layer_rect(layer)
    min_x = float("inf")
    min_y = float("inf")
    max_x = float("-inf")
    max_y = float("-inf")
    for shape in shapes:
        rect = shape.path.boundingRect()
        pad = max(shape.line_width * 0.6, 0.05) if shape.kind == "line" else 0.02
        min_x = min(min_x, rect.left() - pad)
        min_y = min(min_y, rect.top() - pad)
        max_x = max(max_x, rect.right() + pad)
        max_y = max(max_y, rect.bottom() + pad)
    if min_x == float("inf"):
        return _layer_rect(layer)
    return QRectF(min_x, min_y, max(max_x - min_x, 1e-6), max(max_y - min_y, 1e-6))


def line_width(primitive: Any) -> float:
    if hasattr(primitive, "diameter"):
        try:
            return max(float(getattr(primitive, "diameter")), 0.02)
        except Exception:
            pass
    aperture = getattr(primitive, "aperture", None)
    for attr in ("diameter", "width"):
        if aperture is not None and hasattr(aperture, attr):
            try:
                return max(float(getattr(aperture, attr)), 0.02)
            except Exception:
                continue
    return 0.06


def _append_primitive_shapes(
    primitive: Any,
    shapes: list[DrawShape],
    unsupported: dict[str, int],
    primitive_index: int = -1,
    override: dict[str, float | str] | None = None,
    layer_kind: str = "gerber",
) -> bool:
    override = override or {}
    cls_name = primitive.__class__.__name__.lower()
    clear = str(getattr(primitive, "level_polarity", "dark")).lower() == "clear"
    info = {
        "primitive_type": primitive.__class__.__name__,
        "primitive_index": str(primitive_index),
        "level_polarity": str(getattr(primitive, "level_polarity", "dark")),
        "flashed": str(bool(getattr(primitive, "flashed", False))),
    }

    if cls_name == "region":
        sub_primitives = getattr(primitive, "primitives", [])
        path = _path_from_outline_primitives(sub_primitives)
        if path is not None:
            shapes.append(DrawShape(kind="fill", path=path, line_width=0.0, clear=clear, info=info))
            return True
        return False

    if cls_name == "amgroup":
        sub_primitives = getattr(primitive, "primitives", [])
        outline_primitives = [sub for sub in sub_primitives if sub.__class__.__name__.lower() == "outline"]
        if outline_primitives:
            keep_clear = [
                sub
                for sub in sub_primitives
                if str(getattr(sub, "level_polarity", "dark")).lower() == "clear"
            ]
            sub_primitives = outline_primitives + keep_clear
        appended = False
        tmp_shapes: list[DrawShape] = []
        for sub in sub_primitives:
            if _append_primitive_shapes(
                sub,
                tmp_shapes,
                unsupported,
                primitive_index=primitive_index,
                layer_kind=layer_kind,
            ):
                appended = True
            else:
                name = sub.__class__.__name__
                unsupported[name] = unsupported.get(name, 0) + 1

        if not appended:
            return False

        dark_fill = QPainterPath()
        clear_fill = QPainterPath()
        line_shapes: list[DrawShape] = []
        for shape in tmp_shapes:
            if shape.kind == "fill":
                if shape.clear:
                    clear_fill = clear_fill.united(shape.path)
                else:
                    dark_fill = dark_fill.united(shape.path)
            else:
                line_shapes.append(shape)

        if not dark_fill.isEmpty():
            shapes.append(DrawShape(kind="fill", path=dark_fill, line_width=0.0, clear=clear, info=info))
        if not clear_fill.isEmpty():
            shapes.append(DrawShape(kind="fill", path=clear_fill, line_width=0.0, clear=True, info=info))
        if dark_fill.isEmpty() and clear_fill.isEmpty():
            shapes.extend(line_shapes)
        return appended

    if cls_name == "outline":
        outline_primitives = getattr(primitive, "primitives", [])
        path = _path_from_outline_primitives(outline_primitives)
        if path is not None:
            shapes.append(DrawShape(kind="fill", path=path, line_width=0.0, clear=clear, info=info))
            return True

    if cls_name == "obround":
        pos = getattr(primitive, "position", None)
        width = override.get("width_mm", getattr(primitive, "width", None))
        height = override.get("height_mm", getattr(primitive, "height", None))
        if pos is not None and width and height:
            path = _obround_path(float(pos[0]), float(pos[1]), float(width), float(height))
            if path is not None:
                shapes.append(DrawShape(kind="fill", path=path, line_width=0.0, clear=clear, info=info))
                hole = float(override.get("hole_diameter_mm", 0.0))
                if hole > 0.0:
                    hpath = _circle_poly_path(float(pos[0]), float(pos[1]), hole * 0.5)
                    shapes.append(DrawShape(kind="fill", path=hpath, line_width=0.0, clear=True, info=info))
                return True

    if cls_name == "roundrectangle":
        pos = getattr(primitive, "position", None)
        width = override.get("width_mm", getattr(primitive, "width", None))
        height = override.get("height_mm", getattr(primitive, "height", None))
        radius = override.get("corner_radius_mm", getattr(primitive, "radius", None))
        if pos is not None and width and height and radius is not None:
            path = _round_rect_path(
                float(pos[0]),
                float(pos[1]),
                float(width),
                float(height),
                float(radius),
            )
            if path is not None:
                shapes.append(DrawShape(kind="fill", path=path, line_width=0.0, clear=clear, info=info))
                hole = float(override.get("hole_diameter_mm", 0.0))
                if hole > 0.0:
                    hpath = _circle_poly_path(float(pos[0]), float(pos[1]), hole * 0.5)
                    shapes.append(DrawShape(kind="fill", path=hpath, line_width=0.0, clear=True, info=info))
                return True

    try:
        if cls_name == "arc":
            points = _arc_points(primitive)
            if len(points) >= 2:
                path = QPainterPath(QPointF(points[0][0], points[0][1]))
                for px, py in points[1:]:
                    path.lineTo(px, py)
                width = float(override.get("line_width_mm", line_width(primitive)))
                shapes.append(DrawShape(kind="line", path=path, line_width=width, clear=clear, info=info))
                return True

        vertices = getattr(primitive, "vertices", None)
        flashed = bool(getattr(primitive, "flashed", False))
        if vertices and len(vertices) >= 2:
            path = QPainterPath(QPointF(float(vertices[0][0]), -float(vertices[0][1])))
            for vertex in vertices[1:]:
                path.lineTo(float(vertex[0]), -float(vertex[1]))
            if flashed and len(vertices) >= 3:
                path.closeSubpath()
                shapes.append(DrawShape(kind="fill", path=path, line_width=0.0, clear=clear, info=info))
            else:
                width = float(override.get("line_width_mm", line_width(primitive)))
                shapes.append(DrawShape(kind="line", path=path, line_width=width, clear=clear, info=info))
            return True

        if cls_name in {"line", "slot"}:
            start = getattr(primitive, "start", None)
            end = getattr(primitive, "end", None)
            if start and end:
                path = QPainterPath(QPointF(float(start[0]), -float(start[1])))
                path.lineTo(float(end[0]), -float(end[1]))
                width = float(override.get("line_width_mm", line_width(primitive)))
                shapes.append(DrawShape(kind="line", path=path, line_width=width, clear=clear, info=info))
                return True

        if cls_name in {"circle", "drill"}:
            pos = getattr(primitive, "position", None)
            diameter = float(override.get("diameter_mm", getattr(primitive, "diameter", 0.0)))
            if pos and diameter > 0.0:
                r = diameter * 0.5
                path = _circle_poly_path(float(pos[0]), float(pos[1]), r)
                # Excellon drill layers are display layers, so draw drills as visible geometry.
                # For Gerber we keep drill primitives as clear "holes" in copper.
                if cls_name == "drill" and layer_kind != "excellon":
                    shapes.append(DrawShape(kind="fill", path=path, line_width=0.0, clear=True, info=info))
                else:
                    shapes.append(DrawShape(kind="fill", path=path, line_width=0.0, clear=clear, info=info))
                    hole = float(override.get("hole_diameter_mm", 0.0))
                    if hole > 0.0:
                        hpath = _circle_poly_path(float(pos[0]), float(pos[1]), hole * 0.5)
                        shapes.append(DrawShape(kind="fill", path=hpath, line_width=0.0, clear=True, info=info))
                return True
    except Exception:
        return False

    return False


def _obround_path(cx: float, cy: float, width: float, height: float) -> QPainterPath | None:
    if width <= 0.0 or height <= 0.0:
        return None
    return _rounded_rect_poly_path(cx, cy, width, height, min(width, height) * 0.5)


def _round_rect_path(cx: float, cy: float, width: float, height: float, radius: float) -> QPainterPath | None:
    if width <= 0.0 or height <= 0.0:
        return None
    r = max(0.0, min(radius, width * 0.5, height * 0.5))
    return _rounded_rect_poly_path(cx, cy, width, height, r)


def _path_from_outline_primitives(primitives: list[Any]) -> QPainterPath | None:
    points: list[tuple[float, float]] = []
    for primitive in primitives:
        cls_name = primitive.__class__.__name__.lower()
        if cls_name == "arc":
            for px, py in _arc_points(primitive):
                if not points or abs(points[-1][0] - px) > 1e-9 or abs(points[-1][1] - py) > 1e-9:
                    points.append((px, py))
            continue

        vertices = getattr(primitive, "vertices", None)
        if vertices and len(vertices) >= 2:
            for i, vertex in enumerate(vertices):
                px = float(vertex[0])
                py = -float(vertex[1])
                if not points or i > 0:
                    if not points or abs(points[-1][0] - px) > 1e-9 or abs(points[-1][1] - py) > 1e-9:
                        points.append((px, py))
            continue

        start = getattr(primitive, "start", None)
        end = getattr(primitive, "end", None)
        if start is not None:
            sx = float(start[0])
            sy = -float(start[1])
            if not points or abs(points[-1][0] - sx) > 1e-9 or abs(points[-1][1] - sy) > 1e-9:
                points.append((sx, sy))
        if end is not None:
            ex = float(end[0])
            ey = -float(end[1])
            if not points or abs(points[-1][0] - ex) > 1e-9 or abs(points[-1][1] - ey) > 1e-9:
                points.append((ex, ey))

    if len(points) < 3:
        return None

    path = QPainterPath(QPointF(points[0][0], points[0][1]))
    for point in points[1:]:
        path.lineTo(point[0], point[1])
    path.closeSubpath()
    return path


def _arc_points(primitive: Any) -> list[tuple[float, float]]:
    start = getattr(primitive, "start", None)
    end = getattr(primitive, "end", None)
    center = getattr(primitive, "center", None)
    if not start or not end or not center:
        return []

    try:
        sx = float(start[0])
        sy = float(start[1])
        ex = float(end[0])
        ey = float(end[1])
        cx = float(center[0])
        cy = float(center[1])
    except Exception:
        return []

    radius = math.hypot(sx - cx, sy - cy)
    if radius <= 0.0:
        return [(sx, -sy), (ex, -ey)]

    a0 = math.atan2(sy - cy, sx - cx)
    a1 = math.atan2(ey - cy, ex - cx)
    direction = str(getattr(primitive, "direction", "counterclockwise")).lower()
    two_pi = 2.0 * math.pi

    ccw = (a1 - a0) % two_pi
    cw = ccw - two_pi

    if abs(sx - ex) < 1e-9 and abs(sy - ey) < 1e-9:
        sweep = two_pi if direction == "counterclockwise" else -two_pi
    else:
        candidates = [ccw, cw]
        if direction == "clockwise":
            candidates = [cw, ccw]

        bbox = getattr(primitive, "bounding_box", None)
        if callable(bbox):
            bbox = bbox()
        if bbox:
            candidates = sorted(candidates, key=lambda s: _arc_bbox_error(cx, cy, radius, a0, s, bbox))
        sweep = candidates[0]

    arc_len = abs(sweep) * radius
    steps = max(MIN_ARC_SEGMENTS, min(MAX_ARC_SEGMENTS, int(max(1.0, arc_len / SEGMENT_CHORD_MM))))
    points: list[tuple[float, float]] = []
    for i in range(steps + 1):
        t = i / steps
        ang = a0 + (sweep * t)
        x = cx + (radius * math.cos(ang))
        y = cy + (radius * math.sin(ang))
        points.append((x, -y))
    return points


def _circle_poly_path(cx: float, cy: float, radius: float) -> QPainterPath:
    circumference = 2.0 * math.pi * radius
    segments = max(
        MIN_ARC_SEGMENTS,
        min(MAX_ARC_SEGMENTS, int(max(1.0, circumference / SEGMENT_CHORD_MM))),
    )
    points: list[tuple[float, float]] = []
    for i in range(segments):
        ang = (2.0 * math.pi * i) / segments
        points.append((cx + (radius * math.cos(ang)), -(cy + (radius * math.sin(ang)))))
    path = QPainterPath(QPointF(points[0][0], points[0][1]))
    for px, py in points[1:]:
        path.lineTo(px, py)
    path.closeSubpath()
    return path


def _rounded_rect_poly_path(cx: float, cy: float, width: float, height: float, radius: float) -> QPainterPath:
    if radius <= 1e-9:
        x0 = cx - (width * 0.5)
        x1 = cx + (width * 0.5)
        y0 = cy - (height * 0.5)
        y1 = cy + (height * 0.5)
        path = QPainterPath(QPointF(x0, -y0))
        path.lineTo(x1, -y0)
        path.lineTo(x1, -y1)
        path.lineTo(x0, -y1)
        path.closeSubpath()
        return path

    x0 = cx - (width * 0.5)
    x1 = cx + (width * 0.5)
    y0 = cy - (height * 0.5)
    y1 = cy + (height * 0.5)
    r = radius

    points: list[tuple[float, float]] = []
    points.extend(_arc_segment_points(x1 - r, y0 + r, r, -math.pi / 2.0, 0.0))
    points.extend(_arc_segment_points(x1 - r, y1 - r, r, 0.0, math.pi / 2.0))
    points.extend(_arc_segment_points(x0 + r, y1 - r, r, math.pi / 2.0, math.pi))
    points.extend(_arc_segment_points(x0 + r, y0 + r, r, math.pi, 3.0 * math.pi / 2.0))

    points = _dedupe_points(points)
    path = QPainterPath(QPointF(points[0][0], -points[0][1]))
    for px, py in points[1:]:
        path.lineTo(px, -py)
    path.closeSubpath()
    return path


def _arc_segment_points(
    cx: float,
    cy: float,
    radius: float,
    start_angle: float,
    end_angle: float,
) -> list[tuple[float, float]]:
    sweep = end_angle - start_angle
    arc_len = abs(sweep) * radius
    steps = max(3, min(MAX_ARC_SEGMENTS, int(max(1.0, arc_len / SEGMENT_CHORD_MM))))
    pts: list[tuple[float, float]] = []
    for i in range(steps + 1):
        t = i / steps
        ang = start_angle + (sweep * t)
        pts.append((cx + (radius * math.cos(ang)), cy + (radius * math.sin(ang))))
    return pts


def _dedupe_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for px, py in points:
        if not out:
            out.append((px, py))
            continue
        lx, ly = out[-1]
        if abs(px - lx) > 1e-9 or abs(py - ly) > 1e-9:
            out.append((px, py))
    return out


def _describe_primitive(primitive: Any) -> str:
    cls = primitive.__class__.__name__
    parts = [cls]
    for attr in (
        "flashed",
        "level_polarity",
        "direction",
        "quadrant_mode",
        "start",
        "end",
        "center",
        "position",
        "diameter",
        "width",
        "height",
        "radius",
    ):
        if hasattr(primitive, attr):
            try:
                parts.append(f"{attr}={getattr(primitive, attr)}")
            except Exception:
                parts.append(f"{attr}=<err>")
    if hasattr(primitive, "vertices"):
        try:
            vertices = getattr(primitive, "vertices")
            count = len(vertices) if vertices is not None else 0
            parts.append(f"vertices={count}")
        except Exception:
            parts.append("vertices=<err>")
    return ",".join(parts)


def _arc_bbox_error(
    cx: float,
    cy: float,
    radius: float,
    start_angle: float,
    sweep: float,
    bbox: tuple[tuple[float, float], tuple[float, float]],
) -> float:
    pts = _sample_arc_xy(cx, cy, radius, start_angle, sweep, samples=120)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    (bx0, bx1), (by0, by1) = bbox
    return (
        (min(xs) - bx0) ** 2
        + (max(xs) - bx1) ** 2
        + (min(ys) - by0) ** 2
        + (max(ys) - by1) ** 2
    )


def _sample_arc_xy(
    cx: float,
    cy: float,
    radius: float,
    start_angle: float,
    sweep: float,
    samples: int,
) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for i in range(samples + 1):
        t = i / samples
        ang = start_angle + (sweep * t)
        pts.append((cx + (radius * math.cos(ang)), cy + (radius * math.sin(ang))))
    return pts
