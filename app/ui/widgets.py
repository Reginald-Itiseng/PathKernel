from __future__ import annotations

"""Qt widgets for viewport interaction and direct Gerber primitive rendering.

This module intentionally keeps rendering logic close to the scene item because
performance depends on avoiding intermediate artifact files and expensive reloads.
"""

from dataclasses import dataclass
import math
from typing import Callable

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath, QPen, QWheelEvent
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.project import Layer

SEGMENT_CHORD_MM = 0.05
MIN_ARC_SEGMENTS = 12
MAX_ARC_SEGMENTS = 720


@dataclass(slots=True)
class _DrawShape:
    """Preprocessed draw command ready for paint() loop."""

    kind: str
    path: QPainterPath
    line_width: float
    clear: bool


class GraphicsCanvas(QGraphicsView):
    """Interactive CAD-style canvas with pan/zoom and adaptive grid."""

    cursor_moved = Signal(float, float)
    zoom_changed = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        self.setMouseTracking(True)
        self.setBackgroundBrush(QColor("#1b1f24"))
        self.setCacheMode(QGraphicsView.CacheBackground)
        self.setViewportUpdateMode(QGraphicsView.BoundingRectViewportUpdate)
        self.setOptimizationFlags(
            QGraphicsView.DontSavePainterState | QGraphicsView.DontAdjustForAntialiasing
        )
        self._panning = False
        self._last_pan_point = QPoint()
        self._zoom = 1.0
        self._base_grid_mm = 1.0
        self._major_grid_every = 10

    def clear_scene(self) -> None:
        self.scene().clear()

    def add_layer(self, layer: Layer):
        item = CAMLayerItem(layer)
        self.scene().addItem(item)
        return item

    def fit_scene(self) -> None:
        rect = self.scene().itemsBoundingRect()
        if rect.isNull():
            return
        self.fitInView(rect, Qt.KeepAspectRatio)
        self._zoom = self.transform().m11()
        self.zoom_changed.emit(self._zoom)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        step = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(step, step)
        self._zoom *= step
        self.zoom_changed.emit(self._zoom)
        self.viewport().update()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MiddleButton:
            self._panning = True
            self._last_pan_point = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MiddleButton:
            self._panning = False
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._panning:
            delta = event.pos() - self._last_pan_point
            self._last_pan_point = event.pos()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return

        scene_pos = self.mapToScene(event.pos())
        self.cursor_moved.emit(scene_pos.x(), scene_pos.y())
        super().mouseMoveEvent(event)

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:  # noqa: N802
        super().drawBackground(painter, rect)

        scale = max(self.transform().m11(), 1e-6)
        # Grow grid step with zoom-out so the viewport never draws a dense "moire" grid.
        step = self._base_grid_mm
        while step * scale < 12.0:
            step *= 2.0
        major_step = step * self._major_grid_every

        minor_pen = QPen(QColor("#2a323b"), 0)
        major_pen = QPen(QColor("#3a4652"), 0)
        axis_pen = QPen(QColor("#8fb5ff"), 0)

        left = int(rect.left() // step) - 1
        right = int(rect.right() // step) + 1
        top = int(rect.top() // step) - 1
        bottom = int(rect.bottom() // step) + 1

        for ix in range(left, right + 1):
            x = ix * step
            is_major = abs((x / major_step) - round(x / major_step)) < 1e-9
            painter.setPen(major_pen if is_major else minor_pen)
            painter.drawLine(x, rect.top(), x, rect.bottom())

        for iy in range(top, bottom + 1):
            y = iy * step
            is_major = abs((y / major_step) - round(y / major_step)) < 1e-9
            painter.setPen(major_pen if is_major else minor_pen)
            painter.drawLine(rect.left(), y, rect.right(), y)

        painter.setPen(axis_pen)
        painter.drawLine(0.0, rect.top(), 0.0, rect.bottom())
        painter.drawLine(rect.left(), 0.0, rect.right(), 0.0)


class LayerPanel(QWidget):
    visibility_changed = Signal(int, bool)
    selection_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.list_widget = QListWidget(self)
        layout = QVBoxLayout(self)
        layout.addWidget(self.list_widget)
        self.list_widget.itemChanged.connect(self._on_item_changed)
        self.list_widget.currentRowChanged.connect(self.selection_changed.emit)

    def set_layers(self, layers: list[Layer]) -> None:
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for i, layer in enumerate(layers):
            item = QListWidgetItem(layer.name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            item.setCheckState(Qt.Checked if layer.visible else Qt.Unchecked)
            item.setBackground(QColor(layer.color))
            item.setToolTip(str(layer.path))
            item.setData(Qt.UserRole, i)
            self.list_widget.addItem(item)
        self.list_widget.blockSignals(False)

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        idx = item.data(Qt.UserRole)
        if idx is None:
            return
        self.visibility_changed.emit(int(idx), item.checkState() == Qt.Checked)


class FitToolbar(QWidget):
    def __init__(self, on_fit: Callable[[], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        btn = QPushButton("Fit", self)
        btn.clicked.connect(on_fit)
        row = QHBoxLayout(self)
        row.addWidget(btn)
        row.addStretch(1)


class CAMLayerItem(QGraphicsItem):
    """Single PCB layer rendered from parsed primitives."""

    def __init__(self, layer: Layer) -> None:
        super().__init__()
        self.layer = layer
        self._shapes = _extract_shapes(layer)
        self._rect = _shapes_rect(self._shapes, layer)
        self.setCacheMode(QGraphicsItem.DeviceCoordinateCache)

    def boundingRect(self) -> QRectF:  # noqa: N802
        return self._rect

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: ANN001
        if not self._shapes:
            return

        color = QColor(self.layer.color)

        for shape in self._shapes:
            if shape.kind == "line":
                pen = QPen(color)
                pen.setWidthF(max(shape.line_width, 0.02))
                pen.setJoinStyle(Qt.RoundJoin)
                pen.setCapStyle(Qt.RoundCap)
                painter.setPen(pen)
                if shape.clear:
                    painter.save()
                    painter.setCompositionMode(QPainter.CompositionMode_Clear)
                    painter.drawPath(shape.path)
                    painter.restore()
                else:
                    painter.drawPath(shape.path)
                continue

            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            if shape.clear:
                painter.save()
                painter.setCompositionMode(QPainter.CompositionMode_Clear)
                painter.drawPath(shape.path)
                painter.restore()
            else:
                painter.drawPath(shape.path)


def _layer_rect(layer: Layer) -> QRectF:
    if layer.bbox:
        min_x, min_y, max_x, max_y = layer.bbox
        return QRectF(min_x, -max_y, max(max_x - min_x, 1e-6), max(max_y - min_y, 1e-6))
    return QRectF(0.0, 0.0, 1.0, 1.0)


def _shapes_rect(shapes: list[_DrawShape], layer: Layer) -> QRectF:
    """Use drawn geometry bounds instead of parser bounds to avoid clipping."""
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


def _extract_shapes(layer: Layer) -> list[_DrawShape]:
    """Convert pcb-tools primitives into normalized draw shapes.

    Also records unsupported primitive diagnostics into layer.metadata.
    """
    shapes: list[_DrawShape] = []
    primitives = getattr(layer.source, "primitives", [])
    unsupported: dict[str, int] = {}
    unsupported_samples: list[str] = []
    for primitive in primitives:
        if not _append_primitive_shapes(primitive, shapes, unsupported):
            name = primitive.__class__.__name__
            unsupported[name] = unsupported.get(name, 0) + 1
            if len(unsupported_samples) < 20:
                unsupported_samples.append(_describe_primitive(primitive))
    layer.metadata["primitive_count"] = str(len(primitives))
    layer.metadata["shape_count"] = str(len(shapes))
    if unsupported:
        layer.metadata["unsupported_primitives"] = ", ".join(
            f"{name}:{count}" for name, count in sorted(unsupported.items())
        )
        layer.metadata["unsupported_samples"] = " | ".join(unsupported_samples)
    else:
        layer.metadata.pop("unsupported_primitives", None)
        layer.metadata.pop("unsupported_samples", None)
    return shapes


def _line_width(primitive: object) -> float:
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
    primitive: object,
    shapes: list[_DrawShape],
    unsupported: dict[str, int],
) -> bool:
    """Append one primitive (or primitive group) into draw shapes.

    Returns True when the primitive was handled.
    """
    cls_name = primitive.__class__.__name__.lower()
    clear = str(getattr(primitive, "level_polarity", "dark")).lower() == "clear"

    if cls_name == "region":
        sub_primitives = getattr(primitive, "primitives", [])
        path = _path_from_outline_primitives(sub_primitives)
        if path is not None:
            shapes.append(_DrawShape(kind="fill", path=path, line_width=0.0, clear=clear))
            return True
        return False

    if cls_name == "amgroup":
        # Aperture macros often contain helper primitives (e.g. corner circles).
        # Prefer outline-derived geometry for stable shape fidelity.
        sub_primitives = getattr(primitive, "primitives", [])
        outline_primitives = [sub for sub in sub_primitives if sub.__class__.__name__.lower() == "outline"]
        if outline_primitives:
            # Keep all explicit outlines (these define macro body), skip helper circles by default.
            # Preserve clear-polarity shapes (holes/cutouts) from the full group.
            keep_clear = [
                sub
                for sub in sub_primitives
                if str(getattr(sub, "level_polarity", "dark")).lower() == "clear"
            ]
            sub_primitives = outline_primitives + keep_clear
        appended = False
        tmp_shapes: list[_DrawShape] = []
        for sub in sub_primitives:
            if _append_primitive_shapes(sub, tmp_shapes, unsupported):
                appended = True
            else:
                name = sub.__class__.__name__
                unsupported[name] = unsupported.get(name, 0) + 1

        if not appended:
            return False

        # AM macro pads may be composed from multiple overlapping fills (outline + corner circles).
        # Union dark fills into one path to avoid visible seams while preserving true extents.
        dark_fill = QPainterPath()
        clear_fill = QPainterPath()
        line_shapes: list[_DrawShape] = []
        for shape in tmp_shapes:
            if shape.kind == "fill":
                if shape.clear:
                    clear_fill = clear_fill.united(shape.path)
                else:
                    dark_fill = dark_fill.united(shape.path)
            else:
                line_shapes.append(shape)

        if not dark_fill.isEmpty():
            shapes.append(_DrawShape(kind="fill", path=dark_fill, line_width=0.0, clear=clear))
        if not clear_fill.isEmpty():
            shapes.append(_DrawShape(kind="fill", path=clear_fill, line_width=0.0, clear=True))
        # If we already have a filled macro body, helper line/arc strokes usually create artifacts.
        if dark_fill.isEmpty() and clear_fill.isEmpty():
            shapes.extend(line_shapes)
        return appended

    if cls_name == "outline":
        outline_primitives = getattr(primitive, "primitives", [])
        path = _path_from_outline_primitives(outline_primitives)
        if path is not None:
            shapes.append(_DrawShape(kind="fill", path=path, line_width=0.0, clear=clear))
            return True

    if cls_name == "obround":
        pos = getattr(primitive, "position", None)
        width = getattr(primitive, "width", None)
        height = getattr(primitive, "height", None)
        if pos is not None and width and height:
            path = _obround_path(float(pos[0]), float(pos[1]), float(width), float(height))
            if path is not None:
                shapes.append(_DrawShape(kind="fill", path=path, line_width=0.0, clear=clear))
                return True

    if cls_name == "roundrectangle":
        pos = getattr(primitive, "position", None)
        width = getattr(primitive, "width", None)
        height = getattr(primitive, "height", None)
        radius = getattr(primitive, "radius", None)
        if pos is not None and width and height and radius is not None:
            path = _round_rect_path(
                float(pos[0]),
                float(pos[1]),
                float(width),
                float(height),
                float(radius),
            )
            if path is not None:
                shapes.append(_DrawShape(kind="fill", path=path, line_width=0.0, clear=clear))
                return True

    try:
        # Arc must be handled before generic vertices, otherwise it may degrade to a straight segment.
        if cls_name == "arc":
            points = _arc_points(primitive)
            if len(points) >= 2:
                path = QPainterPath(QPointF(points[0][0], points[0][1]))
                for px, py in points[1:]:
                    path.lineTo(px, py)
                shapes.append(_DrawShape(kind="line", path=path, line_width=_line_width(primitive), clear=clear))
                return True

        vertices = getattr(primitive, "vertices", None)
        flashed = bool(getattr(primitive, "flashed", False))
        if vertices and len(vertices) >= 2:
            path = QPainterPath(QPointF(float(vertices[0][0]), -float(vertices[0][1])))
            for vertex in vertices[1:]:
                path.lineTo(float(vertex[0]), -float(vertex[1]))
            if flashed and len(vertices) >= 3:
                path.closeSubpath()
                shapes.append(_DrawShape(kind="fill", path=path, line_width=0.0, clear=clear))
            elif cls_name == "region" and len(vertices) >= 3:
                path.closeSubpath()
                shapes.append(_DrawShape(kind="fill", path=path, line_width=0.0, clear=clear))
            else:
                shapes.append(_DrawShape(kind="line", path=path, line_width=_line_width(primitive), clear=clear))
            return True

        if cls_name in {"line", "slot"}:
            start = getattr(primitive, "start", None)
            end = getattr(primitive, "end", None)
            if start and end:
                path = QPainterPath(QPointF(float(start[0]), -float(start[1])))
                path.lineTo(float(end[0]), -float(end[1]))
                shapes.append(_DrawShape(kind="line", path=path, line_width=_line_width(primitive), clear=clear))
                return True

        if cls_name in {"circle", "drill"}:
            pos = getattr(primitive, "position", None)
            diameter = float(getattr(primitive, "diameter", 0.0))
            if pos and diameter > 0.0:
                r = diameter * 0.5
                path = _circle_poly_path(float(pos[0]), float(pos[1]), r)
                shapes.append(_DrawShape(kind="fill", path=path, line_width=0.0, clear=clear))
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


def _path_from_outline_primitives(primitives: list[object]) -> QPainterPath | None:
    """Build a closed polygon path from outline child primitives (line/arc/vertex)."""
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


def _arc_points(primitive: object) -> list[tuple[float, float]]:
    """Tessellate a Gerber arc primitive into viewport-space points.

    The implementation considers full-circle encoding and picks the sweep
    candidate that best matches the primitive-reported bounding box.
    """
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

    # Full-circle arcs can be encoded with identical start/end.
    if abs(sx - ex) < 1e-9 and abs(sy - ey) < 1e-9:
        sweep = two_pi if direction == "counterclockwise" else -two_pi
    else:
        candidates = [ccw, cw]
        # Prefer direction sign first, then refine using primitive's own bbox if available.
        if direction == "clockwise":
            candidates = [cw, ccw]

        bbox = getattr(primitive, "bounding_box", None)
        if callable(bbox):
            bbox = bbox()
        if bbox:
            # Some files encode direction ambiguously; bbox matching gives a robust tie-breaker.
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
    """Approximate circles with line segments (FlatCAM-style behavior)."""
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
    """Build a rounded-rectangle from four tessellated quarter-arcs."""
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


def _describe_primitive(primitive: object) -> str:
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


def _primitive_bbox_area(primitive: object) -> float:
    bbox = getattr(primitive, "bounding_box", None)
    if callable(bbox):
        bbox = bbox()
    if not bbox:
        return 0.0
    try:
        (min_x, max_x), (min_y, max_y) = bbox
        return max(0.0, float(max_x - min_x)) * max(0.0, float(max_y - min_y))
    except Exception:
        return 0.0


def _arc_bbox_error(
    cx: float,
    cy: float,
    radius: float,
    start_angle: float,
    sweep: float,
    bbox: tuple[tuple[float, float], tuple[float, float]],
) -> float:
    """Lower error means the sampled arc better matches parser-reported bbox."""
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
