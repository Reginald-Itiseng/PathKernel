from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPixmapItem,
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

try:
    from PySide6.QtSvgWidgets import QGraphicsSvgItem
except Exception:  # pragma: no cover
    QGraphicsSvgItem = None


class GraphicsCanvas(QGraphicsView):
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

    def add_artifact(self, artifact_path: Path):
        suffix = artifact_path.suffix.lower()
        if suffix == ".svg" and QGraphicsSvgItem is not None:
            item = QGraphicsSvgItem(str(artifact_path))
            item.setCacheMode(QGraphicsItem.DeviceCoordinateCache)
            self.scene().addItem(item)
            return item

        pixmap = QPixmap(str(artifact_path))
        item = QGraphicsPixmapItem(pixmap)
        item.setTransformationMode(Qt.FastTransformation)
        item.setCacheMode(QGraphicsItem.DeviceCoordinateCache)
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
