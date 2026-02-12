from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPainterPathStroker, QPen, QWheelEvent
from PySide6.QtWidgets import QGraphicsItem, QGraphicsScene, QGraphicsView, QWidget

from app.core.geometry import DrawShape, build_layer_geometry
from app.core.project import Layer


class GraphicsCanvas(QGraphicsView):
    """Simple pan/zoom canvas for CAM layer rendering."""

    cursor_moved = Signal(float, float)
    zoom_changed = Signal(float)
    scene_clicked = Signal(float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        self.setMouseTracking(True)
        self.setBackgroundBrush(QColor("#1b1f24"))

        self._panning = False
        self._last_pan_point = QPoint()
        self._zoom = 1.0

    def clear_scene(self) -> None:
        self.scene().clear()

    def add_layer(self, layer: Layer, layer_index: int):
        item = CAMLayerItem(layer=layer, layer_index=layer_index)
        item.setOpacity(layer.opacity)
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
        step = 1.15 if event.angleDelta().y() > 0 else (1.0 / 1.15)
        self.scale(step, step)
        self._zoom *= step
        self.zoom_changed.emit(self._zoom)

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
        if event.button() == Qt.LeftButton:
            scene_pos = self.mapToScene(event.pos())
            self.scene_clicked.emit(scene_pos.x(), scene_pos.y())
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

    def inspect_at(
        self,
        x_mm: float,
        y_mm: float,
        preferred_layer_index: int | None = None,
    ) -> tuple[int | None, dict[str, str] | None]:
        scene_pos = QPointF(x_mm, y_mm)
        hits: list[tuple[int, dict[str, str]]] = []
        for item in self.scene().items(scene_pos):
            if isinstance(item, CAMLayerItem):
                info = item.inspect_at(scene_pos)
                if info is not None:
                    hits.append((item.layer_index, info))
        if not hits:
            return None, None
        if preferred_layer_index is not None:
            for layer_index, info in hits:
                if layer_index == preferred_layer_index:
                    return layer_index, info
        return hits[0]


class CAMLayerItem(QGraphicsItem):
    """Scene item that paints precomputed geometry from core.geometry."""

    def __init__(self, layer: Layer, layer_index: int) -> None:
        super().__init__()
        self.layer = layer
        self.layer_index = layer_index
        geometry = build_layer_geometry(layer)
        self._shapes: list[DrawShape] = geometry.shapes
        self._rect = geometry.bounds

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

    def inspect_at(self, scene_pos: QPointF, tolerance: float = 0.12) -> dict[str, str] | None:
        for idx in range(len(self._shapes) - 1, -1, -1):
            shape = self._shapes[idx]
            if shape.kind == "fill":
                if shape.path.contains(scene_pos):
                    info = dict(shape.info)
                    info["shape_kind"] = shape.kind
                    info["shape_index"] = str(idx)
                    return info
            else:
                stroker = QPainterPathStroker()
                stroker.setWidth(max(shape.line_width, tolerance))
                hit_path = stroker.createStroke(shape.path)
                if hit_path.contains(scene_pos):
                    info = dict(shape.info)
                    info["shape_kind"] = shape.kind
                    info["shape_index"] = str(idx)
                    info["line_width"] = f"{shape.line_width:.6f}"
                    return info
        return None
