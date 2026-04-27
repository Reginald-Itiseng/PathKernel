"""Qt GraphicsView canvas and overlay widgets used by the fallback renderer."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import math

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QKeyEvent, QMouseEvent, QPainter, QPainterPath, QPainterPathStroker, QPen, QPixmap, QWheelEvent
from PySide6.QtWidgets import QGraphicsItem, QGraphicsScene, QGraphicsView, QPushButton, QWidget

from app.core.geometry import DrawShape, build_layer_geometry
from app.core.project import Layer
from app.ui.icons import icon_for


class GraphicsCanvas(QGraphicsView):
    """Simple pan/zoom canvas for CAM layer rendering."""

    cursor_moved = Signal(float, float)
    zoom_changed = Signal(float)
    scene_clicked = Signal(float, float)
    layers_moved = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self._free_pan_extent_mm = 1_000_000.0
        self.scene().setSceneRect(
            -self._free_pan_extent_mm,
            -self._free_pan_extent_mm,
            2.0 * self._free_pan_extent_mm,
            2.0 * self._free_pan_extent_mm,
        )
        self.setRenderHints(QPainter.Antialiasing)
        # Avoid edge artifacts while panning/zooming with custom overlays.
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.setCacheMode(QGraphicsView.CacheNone)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        self.setMouseTracking(True)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setBackgroundBrush(QColor("#1b1f24"))

        self._panning = False
        self._last_pan_point = QPoint()
        self._pan_button: Qt.MouseButton | None = None
        self._space_held = False
        self._zoom = 1.0
        self._min_zoom = 0.02
        self._max_zoom = 800.0
        self._show_grid = True
        self._grid_minor_mm = 1.0
        self._grid_major_factor = 5
        self._ruler_size_px = 22
        self._marquee_active = False
        self._marquee_origin = QPoint()
        self._marquee_rect = QRect()
        self._moving_layers = False
        self._move_start_scene = QPointF()
        self._move_anchor_scene = QPointF()
        self._move_grid_mm = 1.0
        self._move_layer_items: list[CAMLayerItem] = []
        self._move_original_offsets: dict[int, tuple[float, float]] = {}
        self._task_overlay_lines: list[str] = []
        self._task_overlay_max_lines = 12
        self._task_overlay_active = False
        self._task_overlay_dismissed = False
        self._task_overlay_pulse_idx = 0
        self._task_overlay_pulse_timer = QTimer(self)
        self._task_overlay_pulse_timer.setInterval(300)
        self._task_overlay_pulse_timer.timeout.connect(self._task_overlay_pulse)
        self._task_overlay_widget = _TaskTextOverlay(self)
        self._task_overlay_widget.setGeometry(self.viewport().rect())
        self._task_overlay_widget.show()
        self._task_overlay_widget.raise_()
        self._task_overlay_close_button = QPushButton("x", self.viewport())
        self._task_overlay_close_button.setFixedSize(16, 16)
        self._task_overlay_close_button.setCursor(Qt.PointingHandCursor)
        self._task_overlay_close_button.setToolTip("Hide task log")
        self._task_overlay_close_button.setFocusPolicy(Qt.NoFocus)
        overlay_close_icon = icon_for("overlay_close", size=14, color="#d8f5dd")
        if not overlay_close_icon.isNull():
            self._task_overlay_close_button.setIcon(overlay_close_icon)
            self._task_overlay_close_button.setIconSize(QSize(12, 12))
            self._task_overlay_close_button.setText("")
        self._task_overlay_close_button.setStyleSheet(
            "QPushButton {"
            " background-color: rgba(15, 18, 22, 190);"
            " color: #d8f5dd;"
            " border: 1px solid rgba(130, 220, 140, 180);"
            " border-radius: 8px;"
            " font: 8pt Consolas;"
            " padding: 0px;"
            "}"
            "QPushButton:hover {"
            " background-color: rgba(34, 44, 52, 220);"
            " border: 1px solid rgba(190, 250, 190, 220);"
            "}"
        )
        self._task_overlay_close_button.clicked.connect(self._dismiss_task_overlay)
        self._task_overlay_close_button.hide()
        self._sync_task_overlay_controls()
        self._fast_nav_enabled = True
        self._fast_nav_active = False
        self._fast_nav_snapshot: QPixmap | None = None
        self._fast_nav_scene_tl = QPointF()
        self._fast_nav_sx = 1.0
        self._fast_nav_sy = 1.0
        self._fast_nav_idle_timer = QTimer(self)
        self._fast_nav_idle_timer.setSingleShot(True)
        self._fast_nav_idle_timer.setInterval(120)
        self._fast_nav_idle_timer.timeout.connect(self._finish_fast_navigation)
        self._isolation_view_mode = "width"  # "width" | "centerline"
        self._hatching_view_mode = "width"  # "width" | "centerline"
        self._cutout_view_mode = "width"  # "width" | "centerline"
        self._drill_view_mode = "width"  # "width" | "centerline"
        self._crosshair_enabled = True
        self._crosshair_pos: QPoint | None = None

    def set_grid_visible(self, visible: bool) -> None:
        self._show_grid = bool(visible)
        self.viewport().update()

    def grid_visible(self) -> bool:
        return self._show_grid

    def set_grid_minor_mm(self, spacing_mm: float) -> None:
        self._grid_minor_mm = max(1e-6, float(spacing_mm))
        self.viewport().update()

    def zoom_in(self, factor: float = 1.2) -> None:
        self._apply_zoom(factor)

    def zoom_out(self, factor: float = 1.2) -> None:
        self._apply_zoom(1.0 / factor)

    def reset_view(self) -> None:
        self.resetTransform()
        self._zoom = self.transform().m11()
        self.zoom_changed.emit(self._zoom)
        self.viewport().update()

    def set_isolation_view_mode(self, mode: str) -> None:
        normalized = (mode or "").strip().lower()
        if normalized not in {"width", "centerline"}:
            normalized = "width"
        self._isolation_view_mode = normalized
        for item in self.scene().items():
            if isinstance(item, CAMLayerItem):
                item.set_isolation_view_mode(normalized)
        self.viewport().update()

    def isolation_view_mode(self) -> str:
        return self._isolation_view_mode

    def set_hatching_view_mode(self, mode: str) -> None:
        normalized = (mode or "").strip().lower()
        if normalized not in {"width", "centerline"}:
            normalized = "width"
        self._hatching_view_mode = normalized
        for item in self.scene().items():
            if isinstance(item, CAMLayerItem):
                item.set_hatching_view_mode(normalized)
        self.viewport().update()

    def hatching_view_mode(self) -> str:
        return self._hatching_view_mode

    def set_cutout_view_mode(self, mode: str) -> None:
        normalized = (mode or "").strip().lower()
        if normalized not in {"width", "centerline"}:
            normalized = "width"
        self._cutout_view_mode = normalized
        for item in self.scene().items():
            if isinstance(item, CAMLayerItem):
                item.set_cutout_view_mode(normalized)
        self.viewport().update()

    def cutout_view_mode(self) -> str:
        return self._cutout_view_mode

    def set_drill_view_mode(self, mode: str) -> None:
        normalized = (mode or "").strip().lower()
        if normalized not in {"width", "centerline"}:
            normalized = "width"
        self._drill_view_mode = normalized
        for item in self.scene().items():
            if isinstance(item, CAMLayerItem):
                item.set_drill_view_mode(normalized)
        self.viewport().update()

    def drill_view_mode(self) -> str:
        return self._drill_view_mode

    def clear_scene(self) -> None:
        self._finish_fast_navigation()
        self.scene().clear()
        self.scene().setSceneRect(
            -self._free_pan_extent_mm,
            -self._free_pan_extent_mm,
            2.0 * self._free_pan_extent_mm,
            2.0 * self._free_pan_extent_mm,
        )

    def add_layer(self, layer: Layer, layer_index: int):
        item = CAMLayerItem(
            layer=layer,
            layer_index=layer_index,
            isolation_view_mode=self._isolation_view_mode,
            hatching_view_mode=self._hatching_view_mode,
            cutout_view_mode=self._cutout_view_mode,
            drill_view_mode=self._drill_view_mode,
        )
        item.setOpacity(1.0)
        self.scene().addItem(item)
        return item

    def remove_layer(self, handle) -> None:  # noqa: ANN001
        if handle is None:
            return
        try:
            self.scene().removeItem(handle)
        except Exception:
            return

    def apply_layer_transform_updates(self, layer_indices: list[int]) -> None:
        target = {int(i) for i in list(layer_indices or [])}
        if not target:
            return
        for item in self.scene().items():
            if not isinstance(item, CAMLayerItem):
                continue
            if int(item.layer_index) not in target:
                continue
            try:
                item.apply_layer_transform()
            except Exception:
                continue
        self.viewport().update()

    def fit_scene(self) -> None:
        self._finish_fast_navigation()
        rect = self.scene().itemsBoundingRect()
        if rect.isNull():
            return
        self.fitInView(rect, Qt.KeepAspectRatio)
        self._zoom = self.transform().m11()
        self.zoom_changed.emit(self._zoom)
        self.viewport().update()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        self._start_fast_navigation()
        step = 1.15 if event.angleDelta().y() > 0 else (1.0 / 1.15)
        self._apply_zoom(step)
        self._schedule_fast_navigation_finish()
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._wants_pan(event):
            self._start_fast_navigation()
            self._panning = True
            self._last_pan_point = event.pos()
            self._pan_button = event.button()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            cam_item = self._cam_item_at(event.pos())
            selected = self._selected_cam_items()
            if cam_item is not None and cam_item in selected:
                self._start_layer_move(event.pos(), selected)
                event.accept()
                return
            if cam_item is None and not (event.modifiers() & Qt.ControlModifier):
                self._cancel_layer_move(restore=False)
                self.scene().clearSelection()
            self._marquee_active = True
            self._marquee_origin = event.pos()
            self._marquee_rect = QRect(event.pos(), event.pos())
            if not (event.modifiers() & Qt.ControlModifier):
                self.scene().clearSelection()
            self.viewport().update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._panning and event.button() == self._pan_button:
            self._panning = False
            self._pan_button = None
            self.setCursor(Qt.OpenHandCursor if self._space_held else Qt.ArrowCursor)
            self._schedule_fast_navigation_finish()
            event.accept()
            return
        if self._moving_layers and event.button() == Qt.LeftButton:
            moved_indices = [int(item.layer_index) for item in self._move_layer_items]
            end_scene = self.mapToScene(event.pos())
            moved_mag = math.hypot(
                float(end_scene.x()) - float(self._move_start_scene.x()),
                float(end_scene.y()) - float(self._move_start_scene.y()),
            )
            self._moving_layers = False
            self._move_layer_items = []
            self._move_original_offsets = {}
            self.viewport().update()
            if moved_indices and moved_mag > 1e-6:
                try:
                    self.layers_moved.emit(moved_indices)
                except Exception:
                    pass
            event.accept()
            return
        if self._marquee_active and event.button() == Qt.LeftButton:
            rect = self._marquee_rect.normalized()
            moved = rect.width() >= 4 or rect.height() >= 4
            if moved:
                self._select_layers_in_view_rect(rect, add=(event.modifiers() & Qt.ControlModifier) != 0)
            else:
                self.scene().clearSelection()
                cam_item = self._cam_item_at(event.pos())
                if cam_item is not None:
                    cam_item.setSelected(True)
                    scene_pos = self.mapToScene(event.pos())
                    self.scene_clicked.emit(scene_pos.x(), scene_pos.y())
            self._marquee_active = False
            self._marquee_rect = QRect()
            self.viewport().update()
            event.accept()
            return
        if event.button() == Qt.LeftButton and not self._panning:
            scene_pos = self.mapToScene(event.pos())
            self.scene_clicked.emit(scene_pos.x(), scene_pos.y())
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._crosshair_pos = event.pos()
        if self._panning:
            delta = event.pos() - self._last_pan_point
            self._last_pan_point = event.pos()
            center = self.mapToScene(self.viewport().rect().center())
            sx = max(abs(self.transform().m11()), 1e-9)
            sy = max(abs(self.transform().m22()), 1e-9)
            self.centerOn(center.x() - (delta.x() / sx), center.y() - (delta.y() / sy))
            self._schedule_fast_navigation_finish()
            self.viewport().update()
            event.accept()
            return
        if self._moving_layers:
            self._update_layer_move(self.mapToScene(event.pos()))
            event.accept()
            return
        if self._marquee_active:
            self._marquee_rect = QRect(self._marquee_origin, event.pos()).normalized()
            self.viewport().update()
            event.accept()
            return

        scene_pos = self.mapToScene(event.pos())
        self.cursor_moved.emit(scene_pos.x(), scene_pos.y())
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802, ANN001
        self._crosshair_pos = None
        self.viewport().update()
        super().leaveEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key_Space:
            self._space_held = True
            if not self._panning:
                self.setCursor(Qt.OpenHandCursor)
            event.accept()
            return
        if event.key() in (Qt.Key_Plus, Qt.Key_Equal):
            self.zoom_in()
            event.accept()
            return
        if event.key() == Qt.Key_Minus:
            self.zoom_out()
            event.accept()
            return
        if event.key() == Qt.Key_0:
            self.fit_scene()
            event.accept()
            return
        if event.key() == Qt.Key_1:
            self.reset_view()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key_Space:
            self._space_held = False
            if not self._panning:
                self.setCursor(Qt.ArrowCursor)
            event.accept()
            return
        super().keyReleaseEvent(event)

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:  # noqa: N802
        super().drawBackground(painter, rect)
        if not self._show_grid:
            return

        spacing = self._adaptive_grid_spacing_mm()
        major = spacing * self._grid_major_factor
        self._draw_grid_lines(painter, rect, spacing, QColor("#2c3541"), 0)
        self._draw_grid_lines(painter, rect, major, QColor("#3a4655"), 0)
        self._draw_axis(painter, rect)

    def paintEvent(self, event) -> None:  # noqa: N802, ANN001
        if self._fast_nav_active and self._fast_nav_snapshot is not None and not self._fast_nav_snapshot.isNull():
            painter = QPainter(self.viewport())
            try:
                self._draw_fast_navigation_frame(painter)
                if self._show_grid:
                    self._draw_rulers_overlay(painter)
            finally:
                painter.end()
            self._sync_task_overlay_controls()
            if not self._task_overlay_dismissed:
                self._task_overlay_widget.update()
            return

        super().paintEvent(event)
        self._draw_selection_overlay()
        if self._marquee_active and not self._marquee_rect.isNull():
            painter = QPainter(self.viewport())
            try:
                outline = QPen(QColor("#8db7ff"))
                outline.setCosmetic(True)
                painter.setPen(outline)
                painter.fillRect(self._marquee_rect, QColor(76, 139, 245, 40))
                painter.drawRect(self._marquee_rect)
            finally:
                painter.end()
        if self._show_grid:
            painter = QPainter(self.viewport())
            try:
                self._draw_rulers_overlay(painter)
            finally:
                painter.end()
        if self._crosshair_enabled and self._crosshair_pos is not None:
            painter = QPainter(self.viewport())
            try:
                self._draw_crosshair_overlay(painter, self._crosshair_pos)
            finally:
                painter.end()
        self._sync_task_overlay_controls()
        if not self._task_overlay_dismissed:
            self._task_overlay_widget.update()

    def resizeEvent(self, event) -> None:  # noqa: N802, ANN001
        super().resizeEvent(event)
        self._finish_fast_navigation()
        self._sync_task_overlay_controls()

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

    def debug_dump_at(
        self,
        x_mm: float,
        y_mm: float,
        preferred_layer_index: int | None = None,
    ) -> str:
        scene_pos = QPointF(float(x_mm), float(y_mm))
        lines = [
            f"[{self._stamp()}] geometry-debug renderer=qt",
            f"click_mm=({float(x_mm):.6f},{float(y_mm):.6f})",
        ]
        hits: list[tuple[CAMLayerItem, dict[str, str]]] = []
        for item in self.scene().items(scene_pos):
            if isinstance(item, CAMLayerItem):
                info = item.inspect_at(scene_pos)
                if info is not None:
                    hits.append((item, info))
        if not hits:
            lines.append("hit=none")
            return "\n".join(lines)

        hit_item, hit_info = hits[0]
        if preferred_layer_index is not None:
            for item, info in hits:
                if item.layer_index == preferred_layer_index:
                    hit_item, hit_info = item, info
                    break

        gid = str(hit_info.get("group_id", "")).strip()
        primitive_index = str(hit_info.get("primitive_index", "")).strip()
        scoped: list[DrawShape] = []
        for shape in hit_item._shapes:
            sinfo = shape.info or {}
            if gid and str(sinfo.get("group_id", "")).strip() == gid:
                scoped.append(shape)
            elif not gid and primitive_index and str(sinfo.get("primitive_index", "")).strip() == primitive_index:
                scoped.append(shape)
        if not scoped:
            scoped = list(hit_item._shapes)

        clear_count = sum(1 for s in scoped if bool(s.clear))
        dark_count = len(scoped) - clear_count
        kind_ctr = Counter(str(s.kind) for s in scoped)
        prim_ctr = Counter(str((s.info or {}).get("primitive_type", "")) for s in scoped)

        min_x = float("inf")
        min_y = float("inf")
        max_x = float("-inf")
        max_y = float("-inf")
        for s in scoped:
            rect = s.path.boundingRect()
            min_x = min(min_x, float(rect.left()))
            min_y = min(min_y, float(rect.top()))
            max_x = max(max_x, float(rect.right()))
            max_y = max(max_y, float(rect.bottom()))

        lines.extend(
            [
                f"hit_layer_index={hit_item.layer_index}",
                f"group_id={gid or '(none)'}",
                f"primitive_index={primitive_index or '(none)'}",
                f"scoped_shape_count={len(scoped)}",
                f"scoped_dark_shapes={dark_count}",
                f"scoped_clear_shapes={clear_count}",
                f"scoped_kind_counts={dict(kind_ctr)}",
                f"scoped_primitive_type_counts={dict(prim_ctr)}",
            ]
        )
        if min_x != float("inf"):
            lines.append(f"scoped_bounds_mm=({min_x:.6f},{min_y:.6f})-({max_x:.6f},{max_y:.6f})")
        lines.append(f"hit_info={{{', '.join(f'{k}={v}' for k, v in sorted(hit_info.items()))}}}")
        return "\n".join(lines)

    def _wants_pan(self, event: QMouseEvent) -> bool:
        if event.button() in (Qt.MiddleButton, Qt.RightButton):
            return True
        return event.button() == Qt.LeftButton and self._space_held

    def _apply_zoom(self, factor: float) -> None:
        if factor <= 0.0:
            return
        current = self.transform().m11()
        target = current * factor
        if target < self._min_zoom:
            factor = self._min_zoom / max(current, 1e-12)
        elif target > self._max_zoom:
            factor = self._max_zoom / max(current, 1e-12)

        self.scale(factor, factor)
        self._zoom = self.transform().m11()
        self.zoom_changed.emit(self._zoom)
        self.viewport().update()

    def _adaptive_grid_spacing_mm(self) -> float:
        # Keep a readable on-screen spacing by adapting to current zoom.
        target_px = 24.0
        px_per_mm = max(abs(self.transform().m11()), 1e-9)
        base = target_px / px_per_mm
        steps = [
            0.001,
            0.002,
            0.005,
            0.01,
            0.02,
            0.05,
            0.1,
            0.2,
            0.5,
            1.0,
            2.0,
            5.0,
            10.0,
            20.0,
            50.0,
        ]
        min_step = max(self._grid_minor_mm, 1e-6)
        for step in steps:
            if step < min_step:
                continue
            if step >= base:
                return step
        return max(min_step, base)

    def _draw_grid_lines(self, painter: QPainter, rect: QRectF, spacing: float, color: QColor, width: int) -> None:
        if spacing <= 0.0:
            return
        pen = QPen(color)
        pen.setCosmetic(True)
        pen.setWidth(width)
        painter.setPen(pen)

        left = rect.left()
        right = rect.right()
        top = rect.top()
        bottom = rect.bottom()

        x = int(left / spacing) * spacing
        while x <= right:
            painter.drawLine(QPointF(x, top), QPointF(x, bottom))
            x += spacing

        y = int(top / spacing) * spacing
        while y <= bottom:
            painter.drawLine(QPointF(left, y), QPointF(right, y))
            y += spacing

    def _draw_axis(self, painter: QPainter, rect: QRectF) -> None:
        axis_pen = QPen(QColor("#b8c2cf"))
        axis_pen.setCosmetic(True)
        axis_pen.setWidth(2)
        painter.setPen(axis_pen)
        if rect.left() <= 0.0 <= rect.right():
            painter.drawLine(QPointF(0.0, rect.top()), QPointF(0.0, rect.bottom()))
        if rect.top() <= 0.0 <= rect.bottom():
            painter.drawLine(QPointF(rect.left(), 0.0), QPointF(rect.right(), 0.0))

    def _draw_rulers_overlay(self, painter: QPainter) -> None:
        view_rect = self.viewport().rect()
        if view_rect.width() <= 2 or view_rect.height() <= 2:
            return

        ruler = max(14, int(self._ruler_size_px))
        top_rect = QRect(0, 0, view_rect.width(), ruler)
        left_rect = QRect(0, 0, ruler, view_rect.height())

        painter.fillRect(top_rect, QColor(24, 29, 35, 220))
        painter.fillRect(left_rect, QColor(24, 29, 35, 220))
        painter.fillRect(QRect(0, 0, ruler, ruler), QColor(32, 38, 46, 230))

        border_pen = QPen(QColor("#596676"))
        border_pen.setCosmetic(True)
        painter.setPen(border_pen)
        painter.drawLine(ruler, ruler, view_rect.width(), ruler)
        painter.drawLine(ruler, ruler, ruler, view_rect.height())

        scene_tl = self.mapToScene(QPoint(0, 0))
        scene_br = self.mapToScene(QPoint(view_rect.width(), view_rect.height()))
        min_x = min(scene_tl.x(), scene_br.x())
        max_x = max(scene_tl.x(), scene_br.x())
        min_y = min(scene_tl.y(), scene_br.y())
        max_y = max(scene_tl.y(), scene_br.y())
        major = self._adaptive_grid_spacing_mm() * self._grid_major_factor
        if major <= 0.0:
            return

        tick_pen = QPen(QColor("#9aa7b7"))
        tick_pen.setCosmetic(True)
        painter.setPen(tick_pen)
        text_color = QColor("#d9e1ea")
        painter.setPen(text_color)

        label_gap_px = 52
        last_x_label = -10_000
        x = math.floor(min_x / major) * major
        while x <= max_x + (major * 0.5):
            px = self.mapFromScene(QPointF(float(x), 0.0)).x()
            if px >= ruler and px <= view_rect.width():
                tick_len = 10 if abs(x) < 1e-9 else 7
                painter.drawLine(px, ruler, px, ruler - tick_len)
                if (px - last_x_label) >= label_gap_px:
                    painter.drawText(px + 2, ruler - 8, self._format_mm_label(x))
                    last_x_label = px
            x += major

        label_gap_y_px = 34
        last_y_label = -10_000
        y = math.floor(min_y / major) * major
        while y <= max_y + (major * 0.5):
            py = self.mapFromScene(QPointF(0.0, float(y))).y()
            if py >= ruler and py <= view_rect.height():
                tick_len = 10 if abs(y) < 1e-9 else 7
                painter.drawLine(ruler, py, ruler - tick_len, py)
                if (py - last_y_label) >= label_gap_y_px:
                    # Display Y labels in Cartesian convention (positive upward).
                    painter.drawText(2, py - 2, self._format_mm_label(-y))
                    last_y_label = py
            y += major

        painter.setPen(QColor("#f3f6fb"))
        painter.drawText(5, 15, "mm")

    def _draw_crosshair_overlay(self, painter: QPainter, view_pos: QPoint) -> None:
        view_rect = self.viewport().rect()
        if not view_rect.contains(view_pos):
            return
        pen = QPen(QColor(220, 232, 246, 165))
        pen.setCosmetic(True)
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawLine(view_rect.left(), view_pos.y(), view_rect.right(), view_pos.y())
        painter.drawLine(view_pos.x(), view_rect.top(), view_pos.x(), view_rect.bottom())

    def _format_mm_label(self, value_mm: float) -> str:
        rounded = float(value_mm)
        if abs(rounded) < 1e-9:
            return "0"
        if abs(rounded - round(rounded)) < 1e-9:
            return str(int(round(rounded)))
        return f"{rounded:.2f}".rstrip("0").rstrip(".")

    def _cam_item_at(self, view_pos: QPoint) -> "CAMLayerItem | None":
        item = self.itemAt(view_pos)
        while item is not None:
            if isinstance(item, CAMLayerItem):
                return item
            item = item.parentItem()
        return None

    def _selected_cam_items(self) -> list["CAMLayerItem"]:
        out: list[CAMLayerItem] = []
        for item in self.scene().selectedItems():
            if isinstance(item, CAMLayerItem) and item.layer_index >= 0:
                out.append(item)
        return out

    def _select_layers_in_view_rect(self, rect: QRect, *, add: bool) -> None:
        if not add:
            self.scene().clearSelection()
        if rect.width() < 2 and rect.height() < 2:
            return
        poly = self.mapToScene(rect)
        path = QPainterPath()
        path.addPolygon(poly)
        self.scene().setSelectionArea(
            path,
            Qt.AddToSelection if add else Qt.ReplaceSelection,
            Qt.IntersectsItemShape,
        )
        for item in self.scene().selectedItems():
            if not isinstance(item, CAMLayerItem):
                item.setSelected(False)

    def _start_layer_move(self, start_view_pos: QPoint, items: list["CAMLayerItem"]) -> None:
        if not items:
            return
        union_rect: QRectF | None = None
        self._move_original_offsets.clear()
        for item in items:
            rect = item.sceneBoundingRect()
            union_rect = rect if union_rect is None else union_rect.united(rect)
            self._move_original_offsets[item.layer_index] = (item.layer.offset_x_mm, item.layer.offset_y_mm)
        if union_rect is None:
            return
        self._move_layer_items = items
        self._moving_layers = True
        self._move_start_scene = self.mapToScene(start_view_pos)
        # Scene-space bottom-left anchor of combined selected bounds.
        self._move_anchor_scene = QPointF(union_rect.left(), union_rect.bottom())
        self._move_grid_mm = max(self._adaptive_grid_spacing_mm(), 1e-6)

    def _cancel_layer_move(self, *, restore: bool) -> None:
        if not self._moving_layers:
            return
        if restore:
            for item in self._move_layer_items:
                original = self._move_original_offsets.get(item.layer_index)
                if original is None:
                    continue
                ox, oy = original
                item.layer.offset_x_mm = ox
                item.layer.offset_y_mm = oy
                item.apply_layer_transform()
        self._moving_layers = False
        self._move_layer_items = []
        self._move_original_offsets = {}
        self.viewport().update()

    def _update_layer_move(self, current_scene: QPointF) -> None:
        if not self._moving_layers:
            return
        delta_x = current_scene.x() - self._move_start_scene.x()
        delta_y = current_scene.y() - self._move_start_scene.y()
        target_anchor_x = self._move_anchor_scene.x() + delta_x
        target_anchor_y = self._move_anchor_scene.y() + delta_y
        snapped_anchor_x = round(target_anchor_x / self._move_grid_mm) * self._move_grid_mm
        snapped_anchor_y = round(target_anchor_y / self._move_grid_mm) * self._move_grid_mm
        snapped_dx = snapped_anchor_x - self._move_anchor_scene.x()
        snapped_dy = snapped_anchor_y - self._move_anchor_scene.y()

        for item in self._move_layer_items:
            original = self._move_original_offsets.get(item.layer_index)
            if original is None:
                continue
            ox, oy = original
            item.layer.offset_x_mm = ox + snapped_dx
            # Scene Y is down-positive; project Y is up-positive.
            item.layer.offset_y_mm = oy - snapped_dy
            item.apply_layer_transform()
        self.viewport().update()

    def _draw_selection_overlay(self) -> None:
        selected = self._selected_cam_items()
        if not selected:
            return
        bounds: QRectF | None = None
        for item in selected:
            rect = item.sceneBoundingRect()
            bounds = rect if bounds is None else bounds.united(rect)
        if bounds is None or bounds.isNull():
            return

        tl = self.mapFromScene(bounds.topLeft())
        br = self.mapFromScene(bounds.bottomRight())
        view_rect = QRect(tl, br).normalized()
        if view_rect.width() < 2 or view_rect.height() < 2:
            return

        painter = QPainter(self.viewport())
        try:
            fill = QColor(120, 190, 255, 22)
            border = QPen(QColor("#7ec8ff"))
            border.setCosmetic(True)
            border.setWidth(2)
            border.setStyle(Qt.DashLine)
            painter.setPen(border)
            painter.setBrush(fill)
            painter.drawRoundedRect(view_rect, 4, 4)
        finally:
            painter.end()

    def start_task_overlay(self, title: str) -> None:
        self._task_overlay_lines.clear()
        self._task_overlay_active = True
        self._task_overlay_dismissed = False
        self._task_overlay_pulse_idx = 0
        self._task_overlay_pulse_timer.start()
        self.append_task_overlay_line(f"[{self._stamp()}] task start: {title}")

    def append_task_overlay_line(self, text: str) -> None:
        text = str(text).strip()
        if not text:
            return
        self._task_overlay_lines.append(text)
        if len(self._task_overlay_lines) > self._task_overlay_max_lines:
            self._task_overlay_lines = self._task_overlay_lines[-self._task_overlay_max_lines :]
        self._sync_task_overlay_controls()
        if not self._task_overlay_dismissed:
            self._task_overlay_widget.update()

    def finish_task_overlay(self, *, success: bool, detail: str = "") -> None:
        self._task_overlay_pulse_timer.stop()
        self._task_overlay_active = False
        tail = f" ({detail})" if detail else ""
        status = "done" if success else "failed"
        self.append_task_overlay_line(f"[{self._stamp()}] task {status}{tail}")

    def _task_overlay_pulse(self) -> None:
        if not self._task_overlay_active or self._task_overlay_dismissed:
            return
        frames = ("-", "\\", "|", "/")
        msgs = (
            "checking geometry...",
            "building paths...",
            "resolving outlines...",
            "optimizing segments...",
        )
        frame = frames[self._task_overlay_pulse_idx % len(frames)]
        msg = msgs[self._task_overlay_pulse_idx % len(msgs)]
        self._task_overlay_pulse_idx += 1
        self.append_task_overlay_line(f"[{self._stamp()}] {frame} {msg}")

    def _draw_task_overlay(self, painter: QPainter, overlay_rect: QRect) -> None:
        if self._task_overlay_dismissed or not self._task_overlay_lines:
            return
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        font = QFont("Consolas")
        font.setPointSize(9)
        painter.setFont(font)
        line_h = 14
        pad_x = 10
        pad_y = 10
        max_width = 0
        for line in self._task_overlay_lines:
            max_width = max(max_width, painter.fontMetrics().horizontalAdvance(line))
        panel_w = min(max_width + 8, max(220, int(overlay_rect.width() * 0.58)))
        line_count = len(self._task_overlay_lines)
        panel_h = (line_h * line_count) + 4
        panel_x = pad_x
        panel_y = max(pad_y, overlay_rect.height() - panel_h - pad_y)

        y = panel_y + panel_h - 3
        for rev_idx, line in enumerate(reversed(self._task_overlay_lines)):
            alpha = max(50, 255 - (rev_idx * 22))
            painter.setPen(QColor(185, 247, 192, alpha))
            painter.drawText(panel_x + 2, y, line)
            y -= line_h

    def _sync_task_overlay_controls(self) -> None:
        self._task_overlay_widget.setGeometry(self.viewport().rect())
        if self._task_overlay_dismissed or not self._task_overlay_lines:
            self._task_overlay_widget.hide()
            self._task_overlay_close_button.hide()
            return
        self._task_overlay_widget.show()
        self._task_overlay_widget.raise_()
        line_h = 14
        pad_x = 10
        pad_y = 10
        panel_h = (line_h * max(1, len(self._task_overlay_lines))) + 4
        panel_y = max(pad_y, self.viewport().height() - panel_h - pad_y)
        btn_x = pad_x
        btn_y = max(2, panel_y - self._task_overlay_close_button.height() - 2)
        self._task_overlay_close_button.move(btn_x, btn_y)
        self._task_overlay_close_button.show()
        self._task_overlay_close_button.raise_()

    def _dismiss_task_overlay(self) -> None:
        self._task_overlay_dismissed = True
        self._sync_task_overlay_controls()

    @staticmethod
    def _stamp() -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _start_fast_navigation(self) -> None:
        if not self._fast_nav_enabled:
            return
        if self._fast_nav_active:
            return
        snap = self._capture_scene_snapshot()
        if snap.isNull():
            return
        self._fast_nav_snapshot = snap
        self._fast_nav_scene_tl = self.mapToScene(QPoint(0, 0))
        self._fast_nav_sx = max(abs(self.transform().m11()), 1e-9)
        self._fast_nav_sy = max(abs(self.transform().m22()), 1e-9)
        self._fast_nav_active = True

    def _schedule_fast_navigation_finish(self) -> None:
        if not self._fast_nav_active:
            return
        self._fast_nav_idle_timer.start()

    def _finish_fast_navigation(self) -> None:
        self._fast_nav_idle_timer.stop()
        self._fast_nav_active = False
        self._fast_nav_snapshot = None
        self.viewport().update()

    def _draw_fast_navigation_frame(self, painter: QPainter) -> None:
        if self._fast_nav_snapshot is None:
            return
        cur_tl = self.mapToScene(QPoint(0, 0))
        cur_sx = max(abs(self.transform().m11()), 1e-9)
        cur_sy = max(abs(self.transform().m22()), 1e-9)
        scale_x = cur_sx / self._fast_nav_sx
        scale_y = cur_sy / self._fast_nav_sy
        dx = (self._fast_nav_scene_tl.x() - cur_tl.x()) * cur_sx
        dy = (self._fast_nav_scene_tl.y() - cur_tl.y()) * cur_sy
        painter.fillRect(self.viewport().rect(), QColor("#1b1f24"))
        painter.save()
        painter.translate(dx, dy)
        painter.scale(scale_x, scale_y)
        painter.drawPixmap(0, 0, self._fast_nav_snapshot)
        painter.restore()

    def _capture_scene_snapshot(self) -> QPixmap:
        vp = self.viewport().rect()
        if vp.width() <= 1 or vp.height() <= 1:
            return QPixmap()
        pix = QPixmap(vp.size())
        pix.fill(QColor("#1b1f24"))
        painter = QPainter(pix)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            tl = self.mapToScene(QPoint(0, 0))
            br = self.mapToScene(QPoint(vp.width(), vp.height()))
            source = QRectF(
                min(tl.x(), br.x()),
                min(tl.y(), br.y()),
                abs(br.x() - tl.x()),
                abs(br.y() - tl.y()),
            )
            target = QRectF(0.0, 0.0, float(vp.width()), float(vp.height()))
            self.scene().render(painter, target, source, Qt.IgnoreAspectRatio)
        finally:
            painter.end()
        return pix


class _TaskTextOverlay(QWidget):
    """Transparent overlay that renders task-progress text on top of the canvas."""

    def __init__(self, canvas: GraphicsCanvas) -> None:
        super().__init__(canvas.viewport())
        self._canvas = canvas
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def paintEvent(self, event) -> None:  # noqa: N802, ANN001
        painter = QPainter(self)
        try:
            self._canvas._draw_task_overlay(painter, self.rect())
        finally:
            painter.end()


class CAMLayerItem(QGraphicsItem):
    """Scene item that paints precomputed geometry from core.geometry."""

    def __init__(
        self,
        layer: Layer,
        layer_index: int,
        isolation_view_mode: str = "width",
        hatching_view_mode: str = "width",
        cutout_view_mode: str = "width",
        drill_view_mode: str = "width",
    ) -> None:
        super().__init__()
        self.layer = layer
        self.layer_index = layer_index
        self._isolation_view_mode = isolation_view_mode
        self._hatching_view_mode = hatching_view_mode
        self._cutout_view_mode = cutout_view_mode
        self._drill_view_mode = drill_view_mode
        geometry = build_layer_geometry(layer)
        self._shapes: list[DrawShape] = geometry.shapes
        self._rect = geometry.bounds
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.apply_layer_transform()

    def apply_layer_transform(self) -> None:
        transform = self.transform()
        transform.reset()
        sx = -1.0 if self.layer.mirror_x else 1.0
        sy = -1.0 if self.layer.mirror_y else 1.0
        transform.scale(sx, sy)
        if abs(self.layer.rotation_deg) > 1e-9:
            transform.rotate(float(self.layer.rotation_deg))
        self.setTransform(transform)
        self.setPos(float(self.layer.offset_x_mm), -float(self.layer.offset_y_mm))

    def set_isolation_view_mode(self, mode: str) -> None:
        self._isolation_view_mode = mode
        self.update()

    def set_hatching_view_mode(self, mode: str) -> None:
        self._hatching_view_mode = mode
        self.update()

    def set_cutout_view_mode(self, mode: str) -> None:
        self._cutout_view_mode = mode
        self.update()

    def set_drill_view_mode(self, mode: str) -> None:
        self._drill_view_mode = mode
        self.update()

    def boundingRect(self) -> QRectF:  # noqa: N802
        return self._rect

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: ANN001
        if not self._shapes:
            return

        color = QColor(self.layer.color)
        kind = self.layer.metadata.get("kind", "").strip().lower()
        is_isolation = kind == "isolation"
        is_hatching = kind == "hatching_toolpath"
        is_cutout = kind == "cutout_toolpath"
        is_drill = kind == "drill_toolpath"
        is_centering = kind == "centering_holes_toolpath"
        is_surfacing = kind == "surfacing_toolpath"
        is_toolpath = is_isolation or is_hatching or is_cutout or is_drill or is_centering or is_surfacing
        if is_isolation:
            toolpath_mode = self._isolation_view_mode
        elif is_hatching:
            toolpath_mode = self._hatching_view_mode
        elif is_cutout or is_surfacing:
            toolpath_mode = self._cutout_view_mode
        else:
            toolpath_mode = self._drill_view_mode
        for shape in self._shapes:
            if shape.kind == "line":
                if shape.clear:
                    pen = QPen(color)
                    pen.setWidthF(max(shape.line_width, 0.02))
                    pen.setJoinStyle(Qt.RoundJoin)
                    pen.setCapStyle(Qt.RoundCap)
                    painter.setPen(pen)
                    painter.save()
                    painter.setCompositionMode(QPainter.CompositionMode_Clear)
                    painter.drawPath(shape.path)
                    painter.restore()
                else:
                    if is_toolpath:
                        if toolpath_mode == "centerline":
                            center_pen = QPen(color)
                            center_pen.setWidthF(max(0.02, shape.line_width * 0.22))
                            center_pen.setJoinStyle(Qt.RoundJoin)
                            center_pen.setCapStyle(Qt.RoundCap)
                            painter.setPen(center_pen)
                            painter.drawPath(shape.path)
                            self._draw_direction_arrows(
                                painter,
                                shape.path,
                                color=color,
                                line_width=max(0.02, shape.line_width * 0.22),
                            )
                        else:
                            cut_pen = QPen(color)
                            cut_pen.setWidthF(max(shape.line_width, 0.02))
                            cut_pen.setJoinStyle(Qt.RoundJoin)
                            cut_pen.setCapStyle(Qt.RoundCap)
                            painter.setPen(cut_pen)
                            painter.drawPath(shape.path)
                    else:
                        pen = QPen(color)
                        pen.setWidthF(max(shape.line_width, 0.02))
                        pen.setJoinStyle(Qt.RoundJoin)
                        pen.setCapStyle(Qt.RoundCap)
                        painter.setPen(pen)
                        painter.drawPath(shape.path)
                continue

            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            if is_toolpath and toolpath_mode == "centerline":
                continue
            if shape.clear:
                painter.save()
                painter.setCompositionMode(QPainter.CompositionMode_Clear)
                painter.drawPath(shape.path)
                painter.restore()
            else:
                painter.drawPath(shape.path)

    def inspect_at(self, scene_pos: QPointF, tolerance: float = 0.12) -> dict[str, str] | None:
        local_pos = self.mapFromScene(scene_pos)
        for idx in range(len(self._shapes) - 1, -1, -1):
            shape = self._shapes[idx]
            rect = shape.path.boundingRect()
            if shape.kind == "fill":
                if shape.path.contains(local_pos):
                    info = dict(shape.info)
                    info["shape_kind"] = shape.kind
                    info["shape_index"] = str(idx)
                    # Bounds fallback is used by metadata panel for pad-dimension summary.
                    if "bounds_width_mm" not in info:
                        info["bounds_width_mm"] = f"{max(0.0, float(rect.width())):.6f}"
                    if "bounds_height_mm" not in info:
                        info["bounds_height_mm"] = f"{max(0.0, float(rect.height())):.6f}"
                    return info
            else:
                stroker = QPainterPathStroker()
                stroker.setWidth(max(shape.line_width, tolerance))
                hit_path = stroker.createStroke(shape.path)
                if hit_path.contains(local_pos):
                    info = dict(shape.info)
                    info["shape_kind"] = shape.kind
                    info["shape_index"] = str(idx)
                    info["line_width"] = f"{shape.line_width:.6f}"
                    if "bounds_width_mm" not in info:
                        info["bounds_width_mm"] = f"{max(0.0, float(rect.width())):.6f}"
                    if "bounds_height_mm" not in info:
                        info["bounds_height_mm"] = f"{max(0.0, float(rect.height())):.6f}"
                    return info
        return None

    def _draw_direction_arrows(self, painter: QPainter, path, *, color: QColor, line_width: float) -> None:
        points = self._path_points(path)
        if len(points) < 2:
            return

        px_per_mm = max(abs(painter.worldTransform().m11()), 1e-9)
        mm_per_px = 1.0 / px_per_mm
        arrow_len = max(0.08, 8.0 * mm_per_px)
        arrow_half = arrow_len * 0.45
        spacing = max(arrow_len * 5.0, 55.0 * mm_per_px)

        pen = QPen(color)
        pen.setWidthF(max(line_width * 0.9, 0.02))
        pen.setJoinStyle(Qt.RoundJoin)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)

        for p0, p1 in zip(points, points[1:]):
            dx = p1.x() - p0.x()
            dy = p1.y() - p0.y()
            seg_len = math.hypot(dx, dy)
            if seg_len < arrow_len * 1.2:
                continue
            ux = dx / seg_len
            uy = dy / seg_len
            nx = -uy
            ny = ux

            arrow_count = max(1, int(seg_len / spacing))
            for idx in range(arrow_count):
                t = (idx + 1) / (arrow_count + 1)
                hx = p0.x() + (dx * t)
                hy = p0.y() + (dy * t)
                bx = hx - (ux * arrow_len)
                by = hy - (uy * arrow_len)
                lx = bx + (nx * arrow_half)
                ly = by + (ny * arrow_half)
                rx = bx - (nx * arrow_half)
                ry = by - (ny * arrow_half)
                painter.drawLine(QPointF(hx, hy), QPointF(lx, ly))
                painter.drawLine(QPointF(hx, hy), QPointF(rx, ry))

    def _path_points(self, path) -> list[QPointF]:
        points: list[QPointF] = []
        count = int(path.elementCount())
        for i in range(count):
            element = path.elementAt(i)
            pt = QPointF(float(element.x), float(element.y))
            if not points:
                points.append(pt)
                continue
            last = points[-1]
            if abs(last.x() - pt.x()) > 1e-9 or abs(last.y() - pt.y()) > 1e-9:
                points.append(pt)
        return points




