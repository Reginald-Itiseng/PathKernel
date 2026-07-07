"""High-performance PyQtGraph canvas implementation with inspect/drag interaction support."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import math

import numpy as np
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPainterPathStroker, QPen, QTransform
from PySide6.QtWidgets import QGraphicsPathItem, QLabel, QPushButton, QVBoxLayout, QWidget

from app.core.geometry import build_layer_geometry
from app.core.project import Layer
from app.ui.icons import icon_for


class PyQtGraphCanvas(QWidget):
    """PyQtGraph renderer with GraphicsCanvas-compatible interaction API."""

    cursor_moved = Signal(float, float)
    zoom_changed = Signal(float)
    scene_clicked = Signal(float, float)
    layers_moved = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._available = False
        self._pg = None
        self._plot = None
        self._view = None
        self._items: list[object] = []
        self._layer_items: dict[int, list[object]] = {}
        self._layers_by_index: dict[int, Layer] = {}
        self._layer_hit_entries: dict[int, list[dict[str, object]]] = {}
        self._layer_hit_grids: dict[int, dict[tuple[int, int], list[int]]] = {}
        self._layer_hit_cell_mm: dict[int, float] = {}
        self._layer_render_mode: dict[int, str] = {}
        self._toolpath_visual_groups: list[dict[str, object]] = []
        self._grid_minor_mm = 1.0
        self._selected_layer_indices: set[int] = set()
        self._marquee_active = False
        self._marquee_origin = QPoint()
        self._marquee_rect = QRect()
        self._moving_layers = False
        self._move_start_view = QPointF()
        self._move_anchor_view = QPointF()
        self._move_grid_mm = 1.0
        self._move_layer_indices: list[int] = []
        self._move_original_offsets: dict[int, tuple[float, float]] = {}
        self._move_original_entries: dict[int, list[dict[str, object]]] = {}
        self._origin_x_line = None
        self._origin_y_line = None
        self._crosshair_x_line = None
        self._crosshair_y_line = None
        self._isolation_view_mode = "width"
        self._hatching_view_mode = "width"
        self._cutout_view_mode = "width"
        self._drill_view_mode = "width"
        self._grid_visible = True
        self._scene_bg_color = QColor("#1b1f24")
        self._task_overlay_lines: list[str] = []
        self._task_overlay_max_lines = 12
        self._task_overlay_active = False
        self._task_overlay_dismissed = False
        self._task_overlay_pulse_idx = 0
        self._task_overlay_pulse_timer = QTimer(self)
        self._task_overlay_pulse_timer.setInterval(300)
        self._task_overlay_pulse_timer.timeout.connect(self._task_overlay_pulse)
        self._task_overlay_widget: _TaskTextOverlay | None = None
        self._task_overlay_close_button: QPushButton | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._status = QLabel(self)
        self._status.setAlignment(Qt.AlignCenter)
        self._status.setStyleSheet("color: #b6c2cf; background: #1b1f24;")
        layout.addWidget(self._status)

        try:
            import pyqtgraph as pg  # type: ignore

            self._pg = pg
            self._plot = pg.PlotWidget(background="#1b1f24")
            self._view = self._plot.getViewBox()
            self._plot.setMenuEnabled(False)
            self._plot.setClipToView(False)
            self._plot.getPlotItem().showAxis("left")
            self._plot.getPlotItem().showAxis("bottom")
            self._plot.getPlotItem().setContentsMargins(0, 0, 0, 0)
            axis_pen = pg.mkPen("#6d7b8b")
            text_pen = "#d9e1ea"
            self._plot.getAxis("left").setPen(axis_pen)
            self._plot.getAxis("bottom").setPen(axis_pen)
            self._plot.getAxis("left").setTextPen(text_pen)
            self._plot.getAxis("bottom").setTextPen(text_pen)
            self._plot.getAxis("left").setLabel("Y (mm)", color=text_pen)
            self._plot.getAxis("bottom").setLabel("X (mm)", color=text_pen)
            self._plot.showGrid(x=True, y=True, alpha=0.25)
            self._view.setMouseEnabled(x=True, y=True)
            self._view.setAspectLocked(True, ratio=1.0)
            self._view.enableAutoRange(False, False)
            self._view.setDefaultPadding(0.0)
            # Match existing QGraphicsView scene orientation (Y-down).
            self._view.invertY(True)
            origin_pen = pg.mkPen("#b8c2cf", width=1.6)
            self._origin_x_line = pg.InfiniteLine(pos=0.0, angle=0, pen=origin_pen, movable=False)
            self._origin_y_line = pg.InfiniteLine(pos=0.0, angle=90, pen=origin_pen, movable=False)
            self._plot.addItem(self._origin_x_line)
            self._plot.addItem(self._origin_y_line)
            crosshair_pen = pg.mkPen((230, 239, 250, 165), width=1)
            self._crosshair_x_line = pg.InfiniteLine(pos=0.0, angle=0, pen=crosshair_pen, movable=False)
            self._crosshair_y_line = pg.InfiniteLine(pos=0.0, angle=90, pen=crosshair_pen, movable=False)
            self._crosshair_x_line.setZValue(1000)
            self._crosshair_y_line.setZValue(1000)
            self._plot.addItem(self._crosshair_x_line, ignoreBounds=True)
            self._plot.addItem(self._crosshair_y_line, ignoreBounds=True)
            self._crosshair_x_line.hide()
            self._crosshair_y_line.hide()

            layout.removeWidget(self._status)
            self._status.hide()
            layout.addWidget(self._plot)
            self._task_overlay_widget = _TaskTextOverlay(self, self._plot.viewport())
            self._task_overlay_widget.setGeometry(self._plot.viewport().rect())
            self._task_overlay_widget.show()
            self._task_overlay_widget.raise_()
            self._task_overlay_close_button = QPushButton("x", self._plot.viewport())
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
            self._sync_task_overlay_geometry()
            self._plot.viewport().installEventFilter(self)

            self._plot.scene().sigMouseMoved.connect(self._on_mouse_moved)
            self._plot.scene().sigMouseClicked.connect(self._on_mouse_clicked)
            self._view.sigRangeChanged.connect(self._on_range_changed)
            self._available = True
        except Exception as exc:
            self._status.setText(f"PyQtGraph renderer unavailable: {exc}")

    def renderer_name(self) -> str:
        return "pyqtgraph" if self._available else "pyqtgraph_placeholder"

    def clear_scene(self) -> None:
        if not self._available or self._plot is None:
            return
        for item in self._items:
            try:
                self._plot.removeItem(item)
            except Exception:
                pass
        self._items.clear()
        self._layer_items.clear()
        self._layers_by_index.clear()
        self._layer_hit_entries.clear()
        self._layer_hit_grids.clear()
        self._layer_hit_cell_mm.clear()
        self._layer_render_mode.clear()
        self._toolpath_visual_groups.clear()
        self._selected_layer_indices.clear()
        self._marquee_active = False
        self._moving_layers = False
        self._move_layer_indices = []
        self._move_original_offsets = {}
        self._move_original_entries = {}
        self._sync_task_overlay_geometry()

    def add_layer(self, layer: Layer, layer_index: int):  # noqa: ARG002
        if not self._available or self._plot is None or self._pg is None:
            return None
        self._layers_by_index[layer_index] = layer
        geom = build_layer_geometry(layer)
        color = QColor(layer.color)
        color.setAlpha(255)
        toolpath_kind = _toolpath_kind(layer)
        toolpath_mode = self._active_toolpath_mode(toolpath_kind)
        self._layer_hit_entries[layer_index] = _build_layer_hit_entries(layer, layer_index, geom.shapes)
        self._rebuild_layer_hit_grid(layer_index)
        flatten_safe, flatten_reason = _flatten_decision(geom.shapes)
        if _should_flatten_toolpath_layer(layer) and flatten_safe:
            self._layer_render_mode[layer_index] = "flattened"
            pg = self._pg
            # Flatten only generated toolpath layers for speed.
            # We keep one/few PlotCurveItem objects per width bucket and use NaN separators
            # to preserve path discontinuities without creating per-segment graphics items.
            line_buckets: dict[float, list[list[QPointF]]] = {}
            dark_fill_paths: list[QPainterPath] = []
            clear_fill_paths: list[QPainterPath] = []
            line_subpaths: list[list[QPointF]] = []
            for shape in geom.shapes:
                if shape.kind == "line":
                    if shape.clear:
                        continue
                    width = max(float(shape.line_width), 0.02)
                    key = round(width, 6)
                    bucket = line_buckets.setdefault(key, [])
                    for subpath in _path_to_subpaths(shape.path):
                        if len(subpath) < 2:
                            continue
                        bucket.append(subpath)
                else:
                    p = QPainterPath(shape.path)
                    p.setFillRule(Qt.WindingFill)
                    if shape.clear:
                        clear_fill_paths.append(p)
                    else:
                        dark_fill_paths.append(p)

            added: list[object] = []
            center_items: list[object] = []
            center_color = _toolpath_centerline_color(color) if toolpath_kind is not None else color
            max_width = max(line_buckets.keys()) if line_buckets else 0.02
            for width, raw_subpaths in line_buckets.items():
                if toolpath_kind is not None:
                    # Merge contiguous segments from tessellated curves so centerline mode
                    # looks continuous and arrows follow true cutting direction.
                    stitched = _stitch_connected_subpaths(
                        raw_subpaths,
                        tolerance=max(1e-6, float(width) * 1e-4),
                    )
                    draw_subpaths = stitched if stitched else raw_subpaths
                    line_subpaths.extend(draw_subpaths)
                else:
                    draw_subpaths = raw_subpaths

                xs: list[float] = []
                ys: list[float] = []
                for subpath in draw_subpaths:
                    if len(subpath) < 2:
                        continue
                    for pt in subpath:
                        xs.append(float(pt.x()))
                        ys.append(float(pt.y()))
                    xs.append(math.nan)
                    ys.append(math.nan)
                if len(xs) < 2:
                    continue
                display_w = _toolpath_centerline_width(width) if toolpath_kind is not None else width
                curve = pg.PlotCurveItem(
                    x=np.asarray(xs, dtype=np.float64),
                    y=np.asarray(ys, dtype=np.float64),
                    connect="finite",
                    pen=pg.mkPen(center_color, width=display_w),
                    antialias=True,
                )
                if toolpath_kind is not None:
                    curve.setZValue(120.0)
                _apply_layer_item_transform(curve, layer)
                self._plot.addItem(curve)
                self._items.append(curve)
                added.append(curve)
                center_items.append(curve)

            if toolpath_kind is not None and line_subpaths:
                arrow_x, arrow_y = _arrow_segments_from_subpaths(
                    line_subpaths,
                    base_width=max_width,
                )
                if arrow_x:
                    arrows = pg.PlotCurveItem(
                        x=np.asarray(arrow_x, dtype=np.float64),
                        y=np.asarray(arrow_y, dtype=np.float64),
                        connect="finite",
                        pen=pg.mkPen(center_color, width=max(0.05, _toolpath_centerline_width(max_width) * 0.9)),
                        antialias=True,
                    )
                    arrows.setZValue(130.0)
                    _apply_layer_item_transform(arrows, layer)
                    self._plot.addItem(arrows)
                    self._items.append(arrows)
                    added.append(arrows)
                    center_items.append(arrows)

            width_items: list[object] = []
            if toolpath_kind is not None:
                stroked = _stroked_fill_paths_from_lines(geom.shapes)
                if stroked:
                    width_fill = _LayerFillItem(stroked, [], color, self._scene_bg_color)
                    _apply_layer_item_transform(width_fill, layer)
                    width_fill.setVisible(toolpath_mode == "width")
                    self._plot.addItem(width_fill)
                    self._items.append(width_fill)
                    added.append(width_fill)
                    width_items.append(width_fill)
                for item in center_items:
                    item.setVisible(toolpath_mode == "centerline")
                self._toolpath_visual_groups.append(
                    {
                        "layer_index": layer_index,
                        "kind": toolpath_kind,
                        "center_items": center_items,
                        "width_items": width_items,
                    }
                )

            if dark_fill_paths or clear_fill_paths:
                fill_item = _LayerFillItem(dark_fill_paths, clear_fill_paths, color, self._scene_bg_color)
                # For toolpath layers, native fill paths (if any) belong to width-style view.
                if toolpath_kind is not None:
                    fill_item.setVisible(toolpath_mode == "width")
                    group = self._toolpath_visual_groups[-1] if self._toolpath_visual_groups else None
                    if group is not None:
                        group["width_items"] = list(group.get("width_items", [])) + [fill_item]
                _apply_layer_item_transform(fill_item, layer)
                self._plot.addItem(fill_item)
                self._items.append(fill_item)
                added.append(fill_item)
            _apply_layer_z_values(added, layer, layer_index)
            self._layer_items[layer_index] = list(added)
            return added

        # Fallback path: high-fidelity rendering for imported/complex grouped geometry.
        self._layer_render_mode[layer_index] = f"path ({flatten_reason})"
        added: list[object] = []
        path_item_cls = getattr(self._pg, "PathItem", None)

        dark_fill_paths: list[QPainterPath] = []
        clear_fill_paths: list[QPainterPath] = []
        line_buckets: dict[float, QPainterPath] = {}
        line_subpaths: list[list[QPointF]] = []
        for shape in geom.shapes:
            if shape.kind == "line":
                if shape.clear:
                    continue
                key = round(max(shape.line_width, 0.02), 6)
                bucket = line_buckets.get(key)
                if bucket is None:
                    bucket = QPainterPath()
                    line_buckets[key] = bucket
                bucket.addPath(shape.path)
                if toolpath_kind is not None:
                    for sp in _path_to_subpaths(shape.path):
                        if len(sp) >= 2:
                            line_subpaths.append(sp)
            else:
                p = QPainterPath(shape.path)
                p.setFillRule(Qt.WindingFill)
                if shape.clear:
                    clear_fill_paths.append(p)
                else:
                    dark_fill_paths.append(p)

        width_items: list[object] = []
        center_items: list[object] = []
        if dark_fill_paths or clear_fill_paths:
            fill_item = _LayerFillItem(dark_fill_paths, clear_fill_paths, color, self._scene_bg_color)
            if toolpath_kind is not None:
                fill_item.setVisible(toolpath_mode == "width")
                width_items.append(fill_item)
            _apply_layer_item_transform(fill_item, layer)
            self._plot.addItem(fill_item)
            self._items.append(fill_item)
            added.append(fill_item)

        for width, path in line_buckets.items():
            if path.isEmpty():
                continue
            line_item = path_item_cls(path) if path_item_cls is not None else QGraphicsPathItem(path)
            p = QPen(_toolpath_centerline_color(color) if toolpath_kind is not None else color)
            display_w = _toolpath_centerline_width(width) if toolpath_kind is not None else width
            p.setWidthF(max(display_w, 0.02))
            p.setJoinStyle(Qt.RoundJoin)
            p.setCapStyle(Qt.RoundCap)
            p.setCosmetic(toolpath_kind is not None)
            line_item.setPen(p)
            line_item.setBrush(Qt.NoBrush)
            if toolpath_kind is not None:
                line_item.setVisible(toolpath_mode == "centerline")
                line_item.setZValue(120.0)
                center_items.append(line_item)
            _apply_layer_item_transform(line_item, layer)
            self._plot.addItem(line_item)
            self._items.append(line_item)
            added.append(line_item)
        if toolpath_kind is not None and line_subpaths and self._pg is not None:
            max_width = max(line_buckets.keys()) if line_buckets else 0.02
            arrow_x, arrow_y = _arrow_segments_from_subpaths(line_subpaths, base_width=max_width)
            if arrow_x:
                arrows = self._pg.PlotCurveItem(
                    x=np.asarray(arrow_x, dtype=np.float64),
                    y=np.asarray(arrow_y, dtype=np.float64),
                    connect="finite",
                    pen=self._pg.mkPen(_toolpath_centerline_color(color), width=max(0.05, _toolpath_centerline_width(max_width) * 0.9)),
                    antialias=True,
                )
                arrows.setVisible(toolpath_mode == "centerline")
                arrows.setZValue(130.0)
                _apply_layer_item_transform(arrows, layer)
                self._plot.addItem(arrows)
                self._items.append(arrows)
                added.append(arrows)
                center_items.append(arrows)
        if toolpath_kind is not None:
            # Ensure we have true cut-width visual from line stroke area, even in path fallback mode.
            stroked = _stroked_fill_paths_from_lines(geom.shapes)
            if stroked:
                width_fill = _LayerFillItem(stroked, [], color, self._scene_bg_color)
                width_fill.setVisible(toolpath_mode == "width")
                _apply_layer_item_transform(width_fill, layer)
                self._plot.addItem(width_fill)
                self._items.append(width_fill)
                added.append(width_fill)
                width_items.append(width_fill)
            self._toolpath_visual_groups.append(
                {
                    "layer_index": layer_index,
                    "kind": toolpath_kind,
                    "center_items": center_items,
                    "width_items": width_items,
                }
            )
        _apply_layer_z_values(added, layer, layer_index)
        self._layer_items[layer_index] = list(added)
        return added

    def remove_layer(self, handle) -> None:  # noqa: ANN001
        if not self._available or self._plot is None or handle is None:
            return
        items = handle if isinstance(handle, list) else [handle]
        for item in items:
            try:
                self._plot.removeItem(item)
            except Exception:
                pass
            try:
                self._items.remove(item)
            except ValueError:
                pass
        removed_ids = {id(it) for it in items}
        to_drop: list[int] = []
        for layer_index, layer_items in self._layer_items.items():
            kept = [it for it in layer_items if id(it) not in removed_ids]
            if kept:
                self._layer_items[layer_index] = kept
            else:
                to_drop.append(int(layer_index))
        for layer_index in to_drop:
            self._layer_items.pop(layer_index, None)
            self._layers_by_index.pop(layer_index, None)
            self._layer_hit_entries.pop(layer_index, None)
            self._layer_hit_grids.pop(layer_index, None)
            self._layer_hit_cell_mm.pop(layer_index, None)
            self._layer_render_mode.pop(layer_index, None)
            self._selected_layer_indices.discard(layer_index)
        self._toolpath_visual_groups = [
            g
            for g in self._toolpath_visual_groups
            if not removed_ids.intersection(
                {id(it) for it in list(g.get("center_items", [])) + list(g.get("width_items", []))}
            )
        ]
        self._sync_task_overlay_geometry()

    def remove_layer_index(self, layer_index: int) -> None:
        idx = int(layer_index)
        self._layer_items.pop(idx, None)
        self._layers_by_index.pop(idx, None)
        self._layer_hit_entries.pop(idx, None)
        self._layer_hit_grids.pop(idx, None)
        self._layer_hit_cell_mm.pop(idx, None)
        self._layer_render_mode.pop(idx, None)
        self._selected_layer_indices.discard(idx)
        self._toolpath_visual_groups = [g for g in self._toolpath_visual_groups if int(g.get("layer_index", -1)) != idx]
        self._sync_task_overlay_geometry()

    def apply_layer_transform_updates(self, layer_indices: list[int]) -> None:
        targets = {int(i) for i in list(layer_indices or [])}
        if not targets:
            return
        for layer_index in targets:
            layer = self._layers_by_index.get(layer_index)
            if layer is None:
                continue
            for item in self._layer_items.get(layer_index, []):
                try:
                    _apply_layer_item_transform(item, layer)
                except Exception:
                    pass
            try:
                geom = build_layer_geometry(layer)
                self._layer_hit_entries[layer_index] = _build_layer_hit_entries(layer, layer_index, geom.shapes)
                self._rebuild_layer_hit_grid(layer_index)
            except Exception:
                pass
        self._sync_task_overlay_geometry()

    def fit_scene(self) -> None:
        if not self._available or self._view is None:
            return
        self._view.setAspectLocked(True, ratio=1.0)
        visible_items = [
            it
            for it in self._items
            if not hasattr(it, "isVisible") or bool(it.isVisible())
        ]
        if visible_items:
            # Match PyQtGraph's built-in "A" button behavior for one-shot fit.
            try:
                self._view.autoRange(padding=0.0, items=visible_items)
                self._view.enableAutoRange(False, False)
                self.zoom_changed.emit(1.0)
                return
            except Exception:
                pass

        # Fallback for edge cases where autoRange cannot compute bounds.
        bounds = self._compute_scene_bounds()
        if bounds is None:
            return
        x0, y0, x1, y1 = bounds
        if abs(x1 - x0) < 1e-9:
            x0 -= 0.5
            x1 += 0.5
        if abs(y1 - y0) < 1e-9:
            y0 -= 0.5
            y1 += 0.5
        self._view.setLimits(xMin=None, xMax=None, yMin=None, yMax=None)
        self._view.setRange(
            xRange=(x0, x1),
            yRange=(y0, y1),
            padding=0.0,
            disableAutoRange=True,
        )
        self.zoom_changed.emit(1.0)

    def zoom_in(self, factor: float = 1.2) -> None:
        self._scale_view(1.0 / max(factor, 1e-6))

    def zoom_out(self, factor: float = 1.2) -> None:
        self._scale_view(max(factor, 1e-6))

    def reset_view(self) -> None:
        self.fit_scene()

    def set_grid_visible(self, visible: bool) -> None:
        self._grid_visible = bool(visible)
        if self._available and self._plot is not None:
            self._plot.showGrid(x=self._grid_visible, y=self._grid_visible, alpha=0.25)

    def set_grid_minor_mm(self, spacing_mm: float) -> None:
        self._grid_minor_mm = max(1e-6, float(spacing_mm))

    def set_isolation_view_mode(self, mode: str) -> None:
        normalized = (mode or "").strip().lower()
        if normalized not in {"width", "centerline"}:
            normalized = "width"
        self._isolation_view_mode = normalized
        self._apply_toolpath_modes()

    def set_hatching_view_mode(self, mode: str) -> None:
        normalized = (mode or "").strip().lower()
        if normalized not in {"width", "centerline"}:
            normalized = "width"
        self._hatching_view_mode = normalized
        self._apply_toolpath_modes()

    def set_cutout_view_mode(self, mode: str) -> None:
        normalized = (mode or "").strip().lower()
        if normalized not in {"width", "centerline"}:
            normalized = "width"
        self._cutout_view_mode = normalized
        self._apply_toolpath_modes()

    def set_drill_view_mode(self, mode: str) -> None:
        normalized = (mode or "").strip().lower()
        if normalized not in {"width", "centerline"}:
            normalized = "width"
        self._drill_view_mode = normalized
        self._apply_toolpath_modes()

    def inspect_at(
        self,
        x_mm: float,
        y_mm: float,
        preferred_layer_index: int | None = None,
    ) -> tuple[int | None, dict[str, str] | None]:
        pt = QPointF(float(x_mm), float(y_mm))
        tolerance = 0.12
        candidates: list[tuple[int, dict[str, str]]] = []
        search_order = list(self._layer_hit_entries.keys())
        if preferred_layer_index in self._layer_hit_entries:
            search_order.remove(preferred_layer_index)
            search_order.insert(0, preferred_layer_index)
        for layer_index in search_order:
            entries = self._layer_hit_entries.get(layer_index, [])
            candidate_idxs = self._query_hit_grid(layer_index, pt, tolerance)
            if not candidate_idxs:
                candidate_idxs = range(len(entries))
            for idx in candidate_idxs:
                if idx < 0 or idx >= len(entries):
                    continue
                entry = entries[idx]
                rect = entry.get("rect")
                if rect is not None and not rect.adjusted(-tolerance, -tolerance, tolerance, tolerance).contains(pt):
                    continue
                kind = str(entry.get("kind", "line"))
                path = entry.get("path")
                if path is None:
                    continue
                if kind == "fill":
                    if path.contains(pt):
                        info = dict(entry.get("info", {}))
                        candidates.append((layer_index, info))
                else:
                    width = float(entry.get("line_width", 0.02))
                    stroker = self._path_stroker(max(width, tolerance))
                    hit = stroker.createStroke(path)
                    if hit.contains(pt):
                        info = dict(entry.get("info", {}))
                        candidates.append((layer_index, info))
        if not candidates:
            return None, None
        if preferred_layer_index is not None:
            preferred = [(layer_index, info) for layer_index, info in candidates if layer_index == preferred_layer_index]
            if preferred:
                return _best_hit_candidate(preferred)
        return _best_hit_candidate(candidates)

    def debug_dump_at(
        self,
        x_mm: float,
        y_mm: float,
        preferred_layer_index: int | None = None,
    ) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pt = QPointF(float(x_mm), float(y_mm))
        layer_index, info = self.inspect_at(x_mm, y_mm, preferred_layer_index=preferred_layer_index)
        lines = [
            f"[{now}] geometry-debug renderer=pyqtgraph",
            f"click_mm=({float(x_mm):.6f},{float(y_mm):.6f})",
        ]
        if layer_index is None or info is None:
            lines.append("hit=none")
            return "\n".join(lines)

        gid = str(info.get("group_id", "")).strip()
        primitive_index = str(info.get("primitive_index", "")).strip()
        entries = self._layer_hit_entries.get(layer_index, [])
        if gid:
            scoped = [e for e in entries if str((e.get("info") or {}).get("group_id", "")).strip() == gid]
        elif primitive_index:
            scoped = [e for e in entries if str((e.get("info") or {}).get("primitive_index", "")).strip() == primitive_index]
        else:
            scoped = []
        if not scoped:
            scoped = entries

        clear_count = sum(1 for e in scoped if bool(e.get("clear", False)))
        dark_count = len(scoped) - clear_count
        kind_ctr = Counter(str(e.get("kind", "")) for e in scoped)
        prim_ctr = Counter(str((e.get("info") or {}).get("primitive_type", "")) for e in scoped)

        min_x = float("inf")
        min_y = float("inf")
        max_x = float("-inf")
        max_y = float("-inf")
        for e in scoped:
            rect = e.get("rect")
            if rect is None:
                continue
            min_x = min(min_x, float(rect.left()))
            min_y = min(min_y, float(rect.top()))
            max_x = max(max_x, float(rect.right()))
            max_y = max(max_y, float(rect.bottom()))

        lines.extend(
            [
                f"hit_layer_index={layer_index}",
                f"render_mode={self._layer_render_mode.get(layer_index, 'unknown')}",
                f"group_id={gid or '(none)'}",
                f"primitive_index={primitive_index or '(none)'}",
                f"scoped_shape_count={len(scoped)}",
                f"scoped_dark_shapes={dark_count}",
                f"scoped_clear_shapes={clear_count}",
                f"scoped_kind_counts={dict(kind_ctr)}",
                f"scoped_primitive_type_counts={dict(prim_ctr)}",
            ]
        )
        overlap = [e for e in entries if _entry_contains_point(e, pt, tol=0.0)]
        overlap_clear = sum(1 for e in overlap if bool(e.get("clear", False)))
        overlap_dark = len(overlap) - overlap_clear
        lines.append(f"overlap_at_click_dark={overlap_dark}")
        lines.append(f"overlap_at_click_clear={overlap_clear}")
        if min_x != float("inf"):
            lines.append(f"scoped_bounds_mm=({min_x:.6f},{min_y:.6f})-({max_x:.6f},{max_y:.6f})")

        lines.append(f"hit_info={{{', '.join(f'{k}={v}' for k, v in sorted(info.items()))}}}")
        return "\n".join(lines)

    def start_task_overlay(self, title: str) -> None:  # noqa: ARG002
        self._task_overlay_lines.clear()
        self._task_overlay_active = True
        self._task_overlay_dismissed = False
        self._task_overlay_pulse_idx = 0
        self._task_overlay_pulse_timer.start()
        self.append_task_overlay_line(f"[{self._stamp()}] task start: {title}")

    def append_task_overlay_line(self, text: str) -> None:  # noqa: ARG002
        msg = str(text).strip()
        if not msg:
            return
        self._task_overlay_lines.append(msg)
        if len(self._task_overlay_lines) > self._task_overlay_max_lines:
            self._task_overlay_lines = self._task_overlay_lines[-self._task_overlay_max_lines :]
        self._sync_task_overlay_geometry()
        if not self._task_overlay_dismissed and self._task_overlay_widget is not None:
            self._task_overlay_widget.update()

    def finish_task_overlay(self, *, success: bool, detail: str = "") -> None:  # noqa: ARG002
        self._task_overlay_pulse_timer.stop()
        self._task_overlay_active = False
        tail = f" ({detail})" if detail else ""
        status = "done" if success else "failed"
        self.append_task_overlay_line(f"[{self._stamp()}] task {status}{tail}")

    def resizeEvent(self, event) -> None:  # noqa: ANN001, N802
        super().resizeEvent(event)
        self._sync_task_overlay_geometry()

    def eventFilter(self, watched, event):  # noqa: ANN001, N802
        if self._plot is not None and watched is self._plot.viewport():
            et = event.type()
            if et in {QEvent.Resize, QEvent.Show}:
                self._sync_task_overlay_geometry()
            if et == QEvent.MouseButtonPress:
                if self._on_viewport_mouse_press(event):
                    return True
            if et == QEvent.MouseMove:
                if self._on_viewport_mouse_move(event):
                    return True
            if et == QEvent.MouseButtonRelease:
                if self._on_viewport_mouse_release(event):
                    return True
        return super().eventFilter(watched, event)

    def transform(self):  # noqa: ANN201
        # Compatibility with code paths that query transform().m11().
        class _Identity:
            @staticmethod
            def m11() -> float:
                return 1.0

        return _Identity()

    def _scale_view(self, factor: float) -> None:
        if not self._available or self._view is None:
            return
        self._view.enableAutoRange(False, False)
        self._view.scaleBy((factor, factor))
        self.zoom_changed.emit(1.0)

    def _on_viewport_mouse_press(self, event) -> bool:  # noqa: ANN001
        if not self._available or self._view is None or self._plot is None:
            return False
        try:
            if event.button() != Qt.LeftButton:
                return False
            ctrl = bool(event.modifiers() & Qt.ControlModifier)
            view_pos = self._view_pos_from_viewport(event.pos())
            layer_index = self._layer_at_view_pos(view_pos)
            if layer_index is not None and layer_index in self._selected_layer_indices:
                self._start_layer_move(event.pos())
                event.accept()
                return True
            if layer_index is None and not ctrl:
                self._selected_layer_indices.clear()
            self._marquee_active = True
            self._marquee_origin = event.pos()
            self._marquee_rect = QRect(event.pos(), event.pos())
            self._sync_task_overlay_geometry()
            event.accept()
            return True
        except Exception:
            return False

    def _on_viewport_mouse_move(self, event) -> bool:  # noqa: ANN001
        if not self._available or self._view is None or self._plot is None:
            return False
        try:
            if self._moving_layers:
                self._update_layer_move(self._view_pos_from_viewport(event.pos()))
                event.accept()
                return True
            if self._marquee_active:
                self._marquee_rect = QRect(self._marquee_origin, event.pos()).normalized()
                self._sync_task_overlay_geometry()
                event.accept()
                return True
        except Exception:
            return False
        return False

    def _on_viewport_mouse_release(self, event) -> bool:  # noqa: ANN001
        if not self._available or self._view is None or self._plot is None:
            return False
        try:
            if event.button() != Qt.LeftButton:
                return False
            view_pos = self._view_pos_from_viewport(event.pos())
            if self._moving_layers:
                moved_indices = list(self._move_layer_indices)
                end_view = self._view_pos_from_viewport(event.pos())
                moved_mag = math.hypot(
                    float(end_view.x()) - float(self._move_start_view.x()),
                    float(end_view.y()) - float(self._move_start_view.y()),
                )
                self._moving_layers = False
                self._move_layer_indices = []
                self._move_original_offsets = {}
                self._move_original_entries = {}
                self._sync_task_overlay_geometry()
                if moved_indices and moved_mag > 1e-6:
                    try:
                        self.layers_moved.emit(moved_indices)
                    except Exception:
                        pass
                event.accept()
                return True
            if self._marquee_active:
                rect = self._marquee_rect.normalized()
                moved = rect.width() >= 4 or rect.height() >= 4
                ctrl = bool(event.modifiers() & Qt.ControlModifier)
                if moved:
                    self._select_layers_in_view_rect(rect, add=ctrl)
                else:
                    if not ctrl:
                        self._selected_layer_indices.clear()
                    layer_index = self._layer_at_view_pos(view_pos)
                    if layer_index is not None:
                        self._selected_layer_indices.add(layer_index)
                    self.scene_clicked.emit(float(view_pos.x()), float(view_pos.y()))
                self._marquee_active = False
                self._marquee_rect = QRect()
                self._sync_task_overlay_geometry()
                event.accept()
                return True
        except Exception:
            return False
        return False

    def _view_pos_from_viewport(self, viewport_pos: QPoint) -> QPointF:
        if self._plot is None or self._view is None:
            return QPointF()
        try:
            scene_pos = self._plot.mapToScene(viewport_pos)
            return QPointF(self._view.mapSceneToView(scene_pos))
        except Exception:
            return QPointF()

    def _viewport_pos_from_view(self, view_pos: QPointF) -> QPointF:
        if self._plot is None or self._view is None:
            return QPointF()
        try:
            scene_pos = self._view.mapViewToScene(view_pos)
            p = self._plot.mapFromScene(scene_pos)
            return QPointF(float(p.x()), float(p.y()))
        except Exception:
            return QPointF()

    def _layer_at_view_pos(self, view_pos: QPointF) -> int | None:
        layer_index, _ = self.inspect_at(float(view_pos.x()), float(view_pos.y()), preferred_layer_index=None)
        return layer_index

    def _select_layers_in_view_rect(self, rect: QRect, *, add: bool) -> None:
        view_rect = self._view_rect_from_viewport_rect(rect)
        if not add:
            self._selected_layer_indices.clear()
        for layer_index in self._layer_hit_entries.keys():
            layer_bounds = self._layer_bounds_in_view(layer_index)
            if layer_bounds is None:
                continue
            if layer_bounds.intersects(view_rect):
                self._selected_layer_indices.add(int(layer_index))
        self._sync_task_overlay_geometry()

    def _view_rect_from_viewport_rect(self, rect: QRect) -> QRectF:
        top_left = self._view_pos_from_viewport(rect.topLeft())
        bottom_right = self._view_pos_from_viewport(rect.bottomRight())
        left = min(float(top_left.x()), float(bottom_right.x()))
        right = max(float(top_left.x()), float(bottom_right.x()))
        top = min(float(top_left.y()), float(bottom_right.y()))
        bottom = max(float(top_left.y()), float(bottom_right.y()))
        return QRectF(left, top, max(0.0, right - left), max(0.0, bottom - top))

    def _layer_bounds_in_view(self, layer_index: int) -> QRectF | None:
        entries = self._layer_hit_entries.get(int(layer_index), [])
        if not entries:
            return None
        min_x = float("inf")
        min_y = float("inf")
        max_x = float("-inf")
        max_y = float("-inf")
        for entry in entries:
            rect = entry.get("rect")
            if rect is None:
                continue
            min_x = min(min_x, float(rect.left()))
            min_y = min(min_y, float(rect.top()))
            max_x = max(max_x, float(rect.right()))
            max_y = max(max_y, float(rect.bottom()))
        if min_x == float("inf"):
            return None
        return QRectF(min_x, min_y, max(0.0, max_x - min_x), max(0.0, max_y - min_y))

    def _start_layer_move(self, start_view_pos: QPoint) -> None:
        if self._view is None:
            return
        layer_indices = [int(i) for i in self._selected_layer_indices if int(i) in self._layers_by_index]
        if not layer_indices:
            return
        union_rect: QRectF | None = None
        self._move_original_offsets = {}
        self._move_original_entries = {}
        for layer_index in layer_indices:
            layer = self._layers_by_index.get(layer_index)
            if layer is None:
                continue
            bounds = self._layer_bounds_in_view(layer_index)
            if bounds is None:
                continue
            union_rect = bounds if union_rect is None else union_rect.united(bounds)
            self._move_original_offsets[layer_index] = (
                float(getattr(layer, "offset_x_mm", 0.0) or 0.0),
                float(getattr(layer, "offset_y_mm", 0.0) or 0.0),
            )
            original_entries: list[dict[str, object]] = []
            for entry in self._layer_hit_entries.get(layer_index, []):
                path = entry.get("path")
                rect = entry.get("rect")
                copied = dict(entry)
                if path is not None:
                    copied["path"] = QPainterPath(path)
                if rect is not None:
                    copied["rect"] = QRectF(rect)
                original_entries.append(copied)
            self._move_original_entries[layer_index] = original_entries
        if union_rect is None:
            return
        self._move_layer_indices = [idx for idx in layer_indices if idx in self._move_original_offsets]
        if not self._move_layer_indices:
            return
        self._moving_layers = True
        self._move_start_view = self._view_pos_from_viewport(start_view_pos)
        self._move_anchor_view = QPointF(union_rect.left(), union_rect.bottom())
        self._move_grid_mm = max(self._adaptive_grid_spacing_mm(), 1e-6)

    def _update_layer_move(self, current_view: QPointF) -> None:
        if not self._moving_layers:
            return
        delta_x = float(current_view.x()) - float(self._move_start_view.x())
        delta_y = float(current_view.y()) - float(self._move_start_view.y())
        target_anchor_x = float(self._move_anchor_view.x()) + delta_x
        target_anchor_y = float(self._move_anchor_view.y()) + delta_y
        snapped_anchor_x = round(target_anchor_x / self._move_grid_mm) * self._move_grid_mm
        snapped_anchor_y = round(target_anchor_y / self._move_grid_mm) * self._move_grid_mm
        snapped_dx = snapped_anchor_x - float(self._move_anchor_view.x())
        snapped_dy = snapped_anchor_y - float(self._move_anchor_view.y())

        for layer_index in self._move_layer_indices:
            layer = self._layers_by_index.get(layer_index)
            original = self._move_original_offsets.get(layer_index)
            if layer is None or original is None:
                continue
            ox, oy = original
            layer.offset_x_mm = ox + snapped_dx
            # View Y is down-positive; project Y is up-positive.
            layer.offset_y_mm = oy - snapped_dy
            for item in self._layer_items.get(layer_index, []):
                try:
                    _apply_layer_item_transform(item, layer)
                except Exception:
                    pass

            translated_entries: list[dict[str, object]] = []
            for original_entry in self._move_original_entries.get(layer_index, []):
                moved_entry = dict(original_entry)
                path = original_entry.get("path")
                rect = original_entry.get("rect")
                if path is not None:
                    p = QPainterPath(path)
                    p.translate(float(snapped_dx), float(snapped_dy))
                    moved_entry["path"] = p
                if rect is not None:
                    moved_entry["rect"] = QRectF(rect).translated(float(snapped_dx), float(snapped_dy))
                translated_entries.append(moved_entry)
            self._layer_hit_entries[layer_index] = translated_entries
            self._rebuild_layer_hit_grid(layer_index)

        self._sync_task_overlay_geometry()

    def _adaptive_grid_spacing_mm(self) -> float:
        px_per_mm = max(self._px_per_mm(), 1e-9)
        base = 24.0 / px_per_mm
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
        min_step = max(float(self._grid_minor_mm), 1e-6)
        for step in steps:
            if step < min_step:
                continue
            if step >= base:
                return step
        return max(min_step, base)

    def _px_per_mm(self) -> float:
        a = self._viewport_pos_from_view(QPointF(0.0, 0.0))
        b = self._viewport_pos_from_view(QPointF(1.0, 0.0))
        return abs(float(b.x()) - float(a.x()))

    def _interaction_overlay_active(self) -> bool:
        return self._marquee_active or bool(self._selected_layer_indices)

    def _draw_interaction_overlay(self, painter: QPainter, overlay_rect: QRect) -> None:  # noqa: ARG002
        if self._marquee_active and not self._marquee_rect.isNull():
            outline = QPen(QColor("#8db7ff"))
            outline.setCosmetic(True)
            painter.setPen(outline)
            painter.fillRect(self._marquee_rect, QColor(76, 139, 245, 40))
            painter.drawRect(self._marquee_rect)

        if not self._selected_layer_indices:
            return
        union_view_rect: QRectF | None = None
        for layer_index in self._selected_layer_indices:
            layer_rect = self._layer_bounds_in_view(int(layer_index))
            if layer_rect is None or layer_rect.isNull():
                continue
            union_view_rect = layer_rect if union_view_rect is None else union_view_rect.united(layer_rect)
        if union_view_rect is None:
            return
        tl = self._viewport_pos_from_view(union_view_rect.topLeft())
        br = self._viewport_pos_from_view(union_view_rect.bottomRight())
        view_rect = QRect(
            QPoint(int(round(min(tl.x(), br.x()))), int(round(min(tl.y(), br.y())))),
            QPoint(int(round(max(tl.x(), br.x()))), int(round(max(tl.y(), br.y())))),
        ).normalized()
        if view_rect.width() < 2 or view_rect.height() < 2:
            return
        fill = QColor(120, 190, 255, 22)
        border = QPen(QColor("#7ec8ff"))
        border.setCosmetic(True)
        border.setWidth(2)
        border.setStyle(Qt.DashLine)
        painter.setPen(border)
        painter.setBrush(fill)
        painter.drawRoundedRect(view_rect, 4, 4)

    def _on_mouse_moved(self, pos) -> None:  # noqa: ANN001
        if not self._available or self._view is None:
            return
        try:
            scene_rect = self._plot.sceneBoundingRect()
            if not scene_rect.contains(pos):
                if self._crosshair_x_line is not None:
                    self._crosshair_x_line.hide()
                if self._crosshair_y_line is not None:
                    self._crosshair_y_line.hide()
                return
            p = self._view.mapSceneToView(pos)
            if self._crosshair_x_line is not None:
                self._crosshair_x_line.setValue(float(p.y()))
                self._crosshair_x_line.show()
            if self._crosshair_y_line is not None:
                self._crosshair_y_line.setValue(float(p.x()))
                self._crosshair_y_line.show()
            self.cursor_moved.emit(float(p.x()), float(p.y()))
        except Exception:
            return

    def _on_mouse_clicked(self, event) -> None:  # noqa: ANN001
        if not self._available or self._view is None:
            return
        try:
            if event.button() != Qt.LeftButton:
                return
            # Left-button interactions are handled on the viewport event filter
            # to support marquee select + drag move parity with Qt renderer.
            if self._plot is not None and self._plot.viewport() is not None:
                return
            scene_pos = event.scenePos()
            p: QPointF = self._view.mapSceneToView(scene_pos)
            self.scene_clicked.emit(float(p.x()), float(p.y()))
        except Exception:
            return

    def _on_range_changed(self, *args) -> None:  # noqa: ANN002, ANN003
        self.zoom_changed.emit(1.0)

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
        self._draw_interaction_overlay(painter, overlay_rect)
        if self._task_overlay_dismissed or not self._task_overlay_lines:
            return
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        font = QFont("Consolas")
        font.setPointSize(9)
        painter.setFont(font)
        line_h = 14
        pad_x = 10
        pad_y = 10
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

    def _sync_task_overlay_geometry(self) -> None:
        if self._task_overlay_widget is None or self._plot is None:
            return
        vp = self._plot.viewport()
        if vp is None:
            return
        self._task_overlay_widget.setGeometry(vp.rect())
        show_task = (not self._task_overlay_dismissed) and bool(self._task_overlay_lines)
        show_interaction = self._interaction_overlay_active()
        if not show_task and not show_interaction:
            self._task_overlay_widget.hide()
            if self._task_overlay_close_button is not None:
                self._task_overlay_close_button.hide()
            return
        self._task_overlay_widget.show()
        self._task_overlay_widget.raise_()
        self._task_overlay_widget.update()
        if self._task_overlay_close_button is None:
            return
        if not show_task:
            self._task_overlay_close_button.hide()
            return
        line_h = 14
        pad_x = 10
        pad_y = 10
        panel_h = (line_h * max(1, len(self._task_overlay_lines))) + 4
        panel_y = max(pad_y, vp.height() - panel_h - pad_y)
        btn_x = pad_x
        btn_y = max(2, panel_y - self._task_overlay_close_button.height() - 2)
        self._task_overlay_close_button.move(btn_x, btn_y)
        self._task_overlay_close_button.show()
        self._task_overlay_close_button.raise_()

    def _dismiss_task_overlay(self) -> None:
        self._task_overlay_dismissed = True
        self._sync_task_overlay_geometry()

    @staticmethod
    def _stamp() -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _path_stroker(self, width: float):
        from PySide6.QtGui import QPainterPathStroker

        stroker = QPainterPathStroker()
        stroker.setWidth(max(0.02, float(width)))
        return stroker

    def _rebuild_layer_hit_grid(self, layer_index: int) -> None:
        entries = self._layer_hit_entries.get(layer_index, [])
        if not entries:
            self._layer_hit_grids[layer_index] = {}
            self._layer_hit_cell_mm[layer_index] = 1.0
            return

        min_x = float("inf")
        min_y = float("inf")
        max_x = float("-inf")
        max_y = float("-inf")
        for entry in entries:
            rect = entry.get("rect")
            if rect is None:
                continue
            min_x = min(min_x, float(rect.left()))
            min_y = min(min_y, float(rect.top()))
            max_x = max(max_x, float(rect.right()))
            max_y = max(max_y, float(rect.bottom()))

        if min_x == float("inf"):
            self._layer_hit_grids[layer_index] = {}
            self._layer_hit_cell_mm[layer_index] = 1.0
            return

        width = max(max_x - min_x, 1e-6)
        height = max(max_y - min_y, 1e-6)
        # Target ~120x120 max grid for balance between memory and query speed.
        cell = max(0.25, min(6.0, max(width, height) / 120.0))
        self._layer_hit_cell_mm[layer_index] = cell

        grid: dict[tuple[int, int], list[int]] = {}
        for idx, entry in enumerate(entries):
            rect = entry.get("rect")
            if rect is None:
                continue
            ix0 = int(math.floor(float(rect.left()) / cell))
            iy0 = int(math.floor(float(rect.top()) / cell))
            ix1 = int(math.floor(float(rect.right()) / cell))
            iy1 = int(math.floor(float(rect.bottom()) / cell))
            for ix in range(ix0, ix1 + 1):
                for iy in range(iy0, iy1 + 1):
                    grid.setdefault((ix, iy), []).append(idx)
        self._layer_hit_grids[layer_index] = grid

    def _query_hit_grid(self, layer_index: int, pt: QPointF, tol_mm: float) -> list[int]:
        grid = self._layer_hit_grids.get(layer_index)
        if not grid:
            return []
        cell = max(self._layer_hit_cell_mm.get(layer_index, 1.0), 1e-6)
        x = float(pt.x())
        y = float(pt.y())
        ix0 = int(math.floor((x - tol_mm) / cell))
        iy0 = int(math.floor((y - tol_mm) / cell))
        ix1 = int(math.floor((x + tol_mm) / cell))
        iy1 = int(math.floor((y + tol_mm) / cell))
        seen: set[int] = set()
        out: list[int] = []
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                for idx in grid.get((ix, iy), []):
                    if idx in seen:
                        continue
                    seen.add(idx)
                    out.append(idx)
        return out

    def _active_toolpath_mode(self, toolpath_kind: str | None) -> str:
        if toolpath_kind == "isolation":
            return self._isolation_view_mode
        if toolpath_kind == "hatching_toolpath":
            return self._hatching_view_mode
        if toolpath_kind == "cutout_toolpath":
            return self._cutout_view_mode
        if toolpath_kind == "surfacing_toolpath":
            return self._cutout_view_mode
        if toolpath_kind in {"drill_toolpath", "centering_holes_toolpath"}:
            return self._drill_view_mode
        return "width"

    def _apply_toolpath_modes(self) -> None:
        for group in self._toolpath_visual_groups:
            kind = str(group.get("kind", "")).strip().lower()
            mode = self._active_toolpath_mode(kind)
            show_center = mode == "centerline"
            for item in group.get("center_items", []):
                try:
                    item.setVisible(show_center)
                except Exception:
                    pass
            for item in group.get("width_items", []):
                try:
                    item.setVisible(not show_center)
                except Exception:
                    pass

    def _compute_scene_bounds(self) -> tuple[float, float, float, float] | None:
        if not self._items and not self._layer_hit_entries:
            return None
        min_x = float("inf")
        min_y = float("inf")
        max_x = float("-inf")
        max_y = float("-inf")
        for item in self._items:
            try:
                if hasattr(item, "isVisible") and not bool(item.isVisible()):
                    continue
                rect = item.sceneBoundingRect()
            except Exception:
                continue
            left = float(rect.left())
            top = float(rect.top())
            right = float(rect.right())
            bottom = float(rect.bottom())
            if not (math.isfinite(left) and math.isfinite(top) and math.isfinite(right) and math.isfinite(bottom)):
                continue
            # Drop obviously invalid extents that can come from malformed display items.
            if max(abs(left), abs(top), abs(right), abs(bottom)) > 1e6:
                continue
            if right < left or bottom < top:
                continue
            min_x = min(min_x, left)
            min_y = min(min_y, top)
            max_x = max(max_x, right)
            max_y = max(max_y, bottom)

        if min_x != float("inf"):
            return min_x, min_y, max_x, max_y

        # Fallback to parsed geometry extents (stable even if renderer items are transient).
        for entries in self._layer_hit_entries.values():
            for entry in entries:
                rect = entry.get("rect")
                if rect is None:
                    continue
                left = float(rect.left())
                top = float(rect.top())
                right = float(rect.right())
                bottom = float(rect.bottom())
                if not (math.isfinite(left) and math.isfinite(top) and math.isfinite(right) and math.isfinite(bottom)):
                    continue
                if max(abs(left), abs(top), abs(right), abs(bottom)) > 1e6:
                    continue
                min_x = min(min_x, left)
                min_y = min(min_y, top)
                max_x = max(max_x, right)
                max_y = max(max_y, bottom)
        if min_x == float("inf"):
            return None
        return min_x, min_y, max_x, max_y


class _TaskTextOverlay(QWidget):
    def __init__(self, canvas: PyQtGraphCanvas, parent: QWidget) -> None:
        super().__init__(parent)
        self._canvas = canvas
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        try:
            self._canvas._draw_task_overlay(painter, self.rect())
        finally:
            painter.end()


def _apply_layer_item_transform(item, layer: Layer) -> None:
    t = QTransform()
    sx = -1.0 if bool(getattr(layer, "mirror_x", False)) else 1.0
    sy = -1.0 if bool(getattr(layer, "mirror_y", False)) else 1.0
    t.scale(sx, sy)
    rot = float(getattr(layer, "rotation_deg", 0.0) or 0.0)
    if abs(rot) > 1e-9:
        t.rotate(rot)
    item.setTransform(t)
    item.setPos(float(getattr(layer, "offset_x_mm", 0.0) or 0.0), -float(getattr(layer, "offset_y_mm", 0.0) or 0.0))


def _path_to_subpaths(path: QPainterPath) -> list[list[QPointF]]:
    # Use polygonized subpaths so closed contours (e.g. round-rect loops) keep their closing edge.
    # This avoids tiny "missing section" gaps when flattening into PlotCurveItem arrays.
    try:
        polys = path.toSubpathPolygons()
        out: list[list[QPointF]] = []
        for poly in polys:
            pts = [QPointF(float(p.x()), float(p.y())) for p in poly]
            if len(pts) < 2:
                continue
            cleaned: list[QPointF] = [pts[0]]
            for p in pts[1:]:
                if abs(cleaned[-1].x() - p.x()) > 1e-9 or abs(cleaned[-1].y() - p.y()) > 1e-9:
                    cleaned.append(p)
            if len(cleaned) >= 2:
                out.append(cleaned)
        if out:
            return out
    except Exception:
        pass

    # Fallback for unexpected path variants.
    subpaths: list[list[QPointF]] = []
    current: list[QPointF] = []
    count = path.elementCount()
    for i in range(count):
        e = path.elementAt(i)
        if e.isMoveTo():
            if len(current) >= 2:
                subpaths.append(current)
            current = [QPointF(e.x, e.y)]
            continue
        current.append(QPointF(e.x, e.y))
    if len(current) >= 2:
        subpaths.append(current)
    return subpaths


def _stitch_connected_subpaths(
    subpaths: list[list[QPointF]],
    *,
    tolerance: float = 1e-6,
) -> list[list[QPointF]]:
    """Merge adjacent subpaths that share endpoints to avoid segmented rendering."""
    if not subpaths:
        return []
    if len(subpaths) == 1:
        return [list(subpaths[0])]

    tol = max(1e-12, float(tolerance))
    endpoint_map: dict[tuple[int, int], list[tuple[int, bool]]] = {}
    normalized: list[list[QPointF]] = []
    for idx, path in enumerate(subpaths):
        cleaned = _dedupe_subpath_points(path)
        if len(cleaned) < 2:
            continue
        normalized.append(cleaned)
    if not normalized:
        return []

    for idx, path in enumerate(normalized):
        endpoint_map.setdefault(_point_key(path[0], tol), []).append((idx, True))
        endpoint_map.setdefault(_point_key(path[-1], tol), []).append((idx, False))

    used: set[int] = set()
    merged: list[list[QPointF]] = []
    for idx in range(len(normalized)):
        if idx in used:
            continue
        used.add(idx)
        chain = list(normalized[idx])

        while True:
            match = _find_chain_match(chain[-1], endpoint_map, normalized, used, tol)
            if match is None:
                break
            other_idx, other_at_start = match
            used.add(other_idx)
            other = normalized[other_idx]
            if other_at_start:
                chain.extend(other[1:])
            else:
                rev = list(reversed(other))
                chain.extend(rev[1:])

        while True:
            match = _find_chain_match(chain[0], endpoint_map, normalized, used, tol)
            if match is None:
                break
            other_idx, other_at_start = match
            used.add(other_idx)
            other = normalized[other_idx]
            if other_at_start:
                rev = list(reversed(other))
                chain = rev[:-1] + chain
            else:
                chain = other[:-1] + chain

        if len(chain) >= 2:
            merged.append(chain)

    return merged


def _find_chain_match(
    endpoint: QPointF,
    endpoint_map: dict[tuple[int, int], list[tuple[int, bool]]],
    subpaths: list[list[QPointF]],
    used: set[int],
    tol: float,
) -> tuple[int, bool] | None:
    for candidate in endpoint_map.get(_point_key(endpoint, tol), []):
        idx, at_start = candidate
        if idx in used:
            continue
        candidate_pt = subpaths[idx][0] if at_start else subpaths[idx][-1]
        if _points_close(endpoint, candidate_pt, tol):
            return idx, at_start
    return None


def _dedupe_subpath_points(path: list[QPointF]) -> list[QPointF]:
    if not path:
        return []
    out = [path[0]]
    for pt in path[1:]:
        prev = out[-1]
        if abs(prev.x() - pt.x()) > 1e-12 or abs(prev.y() - pt.y()) > 1e-12:
            out.append(pt)
    return out


def _point_key(pt: QPointF, tol: float) -> tuple[int, int]:
    return (int(round(float(pt.x()) / tol)), int(round(float(pt.y()) / tol)))


def _points_close(a: QPointF, b: QPointF, tol: float) -> bool:
    dx = float(a.x()) - float(b.x())
    dy = float(a.y()) - float(b.y())
    return (dx * dx + dy * dy) <= (tol * tol)


def _build_layer_hit_entries(layer: Layer, layer_index: int, shapes) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    t = _layer_qtransform(layer)
    dx = float(getattr(layer, "offset_x_mm", 0.0) or 0.0)
    dy = -float(getattr(layer, "offset_y_mm", 0.0) or 0.0)
    for shape in shapes:
        if getattr(shape, "clear", False):
            continue
        p = t.map(shape.path)
        p.translate(dx, dy)
        rect = p.boundingRect()
        info = dict(getattr(shape, "info", {}) or {})
        info["layer_index"] = str(layer_index)
        info["shape_kind"] = str(getattr(shape, "kind", "line"))
        # Keep a lightweight bounding dimension summary in hit metadata so click-inspection
        # can report pad-like dimensions even for compound/grouped geometry.
        if "bounds_width_mm" not in info:
            info["bounds_width_mm"] = f"{max(0.0, float(rect.width())):.6f}"
        if "bounds_height_mm" not in info:
            info["bounds_height_mm"] = f"{max(0.0, float(rect.height())):.6f}"
        if shape.kind == "line":
            info["line_width"] = f"{max(float(getattr(shape, 'line_width', 0.02)), 0.02):.6f}"
        entries.append(
            {
                "kind": str(getattr(shape, "kind", "line")),
                "clear": bool(getattr(shape, "clear", False)),
                "path": p,
                "rect": rect,
                "line_width": max(float(getattr(shape, "line_width", 0.02)), 0.02),
                "info": info,
            }
        )
    return entries


def _entry_contains_point(entry: dict[str, object], pt: QPointF, tol: float = 0.0) -> bool:
    path = entry.get("path")
    if path is None:
        return False
    kind = str(entry.get("kind", "line"))
    if kind == "fill":
        return bool(path.contains(pt))
    width = max(float(entry.get("line_width", 0.02)), 0.02, float(tol))
    from PySide6.QtGui import QPainterPathStroker

    stroker = QPainterPathStroker()
    stroker.setWidth(width)
    hit = stroker.createStroke(path)
    return bool(hit.contains(pt))


def _best_hit_candidate(candidates: list[tuple[int, dict[str, str]]]) -> tuple[int | None, dict[str, str] | None]:
    if not candidates:
        return None, None
    indexed = list(enumerate(candidates))
    _, best = min(indexed, key=lambda item: _hit_candidate_score(item[0], item[1]))
    return best


def _hit_candidate_score(order: int, candidate: tuple[int, dict[str, str]]) -> tuple[float, float, float, int]:
    _layer_index, info = candidate
    kind = str(info.get("shape_kind", "")).strip().lower()
    primitive_type = str(info.get("primitive_type", "")).strip().lower()
    flashed = str(info.get("flashed", "")).strip().lower() == "true"
    try:
        w = max(0.0, float(info.get("bounds_width_mm", "0") or 0.0))
        h = max(0.0, float(info.get("bounds_height_mm", "0") or 0.0))
    except Exception:
        w = h = 0.0
    area = w * h
    pad_like = flashed or primitive_type in {"circle", "rectangle", "obround", "roundrectangle", "polygon"}
    # Prefer small flashed/fill geometry over wide traces when they overlap.
    return (
        0.0 if pad_like else 1.0,
        0.0 if kind == "fill" else 1.0,
        area if area > 0.0 else float("inf"),
        -int(order),
    )


def _layer_qtransform(layer: Layer) -> QTransform:
    t = QTransform()
    sx = -1.0 if bool(getattr(layer, "mirror_x", False)) else 1.0
    sy = -1.0 if bool(getattr(layer, "mirror_y", False)) else 1.0
    t.scale(sx, sy)
    rot = float(getattr(layer, "rotation_deg", 0.0) or 0.0)
    if abs(rot) > 1e-9:
        t.rotate(rot)
    return t


def _should_flatten_toolpath_layer(layer: Layer) -> bool:
    if getattr(layer, "kind", "") != "geometry":
        return False
    kind = str(getattr(layer, "metadata", {}).get("kind", "")).strip().lower()
    return kind in {
        "isolation",
        "hatching_toolpath",
        "cutout_toolpath",
        "drill_toolpath",
        "surfacing_toolpath",
        "centering_holes_toolpath",
    }


def _toolpath_kind(layer: Layer) -> str | None:
    kind = str(getattr(layer, "metadata", {}).get("kind", "")).strip().lower()
    if kind in {
        "isolation",
        "hatching_toolpath",
        "cutout_toolpath",
        "drill_toolpath",
        "surfacing_toolpath",
        "centering_holes_toolpath",
    }:
        return kind
    return None


def _layer_z_value(layer: Layer, layer_index: int) -> float:
    role = str(getattr(layer, "role", "") or "").strip().lower()
    kind = str(getattr(layer, "kind", "") or "").strip().lower()
    meta_kind = str((getattr(layer, "metadata", {}) or {}).get("kind", "")).strip().lower()
    if role in {"drills", "holes"} or kind == "excellon":
        return 80.0
    if meta_kind in {"drill_toolpath", "centering_holes_toolpath"}:
        return 150.0
    if meta_kind in {"isolation", "hatching_toolpath", "cutout_toolpath", "surfacing_toolpath"}:
        return 120.0
    return float(min(max(layer_index, 0), 50))


def _apply_layer_z_values(items: list[object], layer: Layer, layer_index: int) -> None:
    z_value = _layer_z_value(layer, layer_index)
    for item in items:
        try:
            item.setZValue(z_value)
        except Exception:
            pass


def _toolpath_centerline_width(base_width: float) -> float:
    # Fixed pixel width: thin and invariant under zoom.
    return 1.0


def _toolpath_centerline_color(base: QColor) -> QColor:
    c = QColor("#FFFFFF")
    c.setAlpha(255)
    return c


def _stroked_fill_paths_from_lines(shapes) -> list[QPainterPath]:
    out: list[QPainterPath] = []
    for shape in shapes:
        if bool(getattr(shape, "clear", False)):
            continue
        if str(getattr(shape, "kind", "")).lower() != "line":
            continue
        stroker = QPainterPathStroker()
        stroker.setWidth(max(float(getattr(shape, "line_width", 0.02)), 0.02))
        stroker.setJoinStyle(Qt.RoundJoin)
        stroker.setCapStyle(Qt.RoundCap)
        p = stroker.createStroke(shape.path)
        if p.isEmpty():
            continue
        p.setFillRule(Qt.WindingFill)
        out.append(p)
    return out


def _arrow_segments_from_subpaths(
    subpaths: list[list[QPointF]],
    *,
    base_width: float,
) -> tuple[list[float], list[float]]:
    # Keep arrows intentionally tiny and sparse so they stay within narrow path gaps.
    spacing = max(2.2, float(base_width) * 10.0)
    arrow_len = max(0.12, float(base_width) * 0.8)
    wing = arrow_len * 0.22

    xs: list[float] = []
    ys: list[float] = []
    for sp in subpaths:
        if len(sp) < 2:
            continue
        dist_since = 0.0
        for i in range(len(sp) - 1):
            x0 = float(sp[i].x())
            y0 = float(sp[i].y())
            x1 = float(sp[i + 1].x())
            y1 = float(sp[i + 1].y())
            dx = x1 - x0
            dy = y1 - y0
            seg_len = math.hypot(dx, dy)
            if seg_len <= 1e-9:
                continue
            ux = dx / seg_len
            uy = dy / seg_len
            local = 0.0
            while (dist_since + (seg_len - local)) >= spacing:
                step = spacing - dist_since
                t = (local + step) / seg_len
                cx = x0 + (dx * t)
                cy = y0 + (dy * t)
                tip_x = cx + (ux * arrow_len * 0.5)
                tip_y = cy + (uy * arrow_len * 0.5)
                base_x = cx - (ux * arrow_len * 0.5)
                base_y = cy - (uy * arrow_len * 0.5)
                px = -uy
                py = ux
                w1x = base_x + (px * wing)
                w1y = base_y + (py * wing)
                w2x = base_x - (px * wing)
                w2y = base_y - (py * wing)

                xs.extend([w1x, tip_x, math.nan, w2x, tip_x, math.nan])
                ys.extend([w1y, tip_y, math.nan, w2y, tip_y, math.nan])
                local += step
                dist_since = 0.0
            dist_since += seg_len - local
    return xs, ys


def _flatten_decision(shapes) -> tuple[bool, str]:
    # Flattening is only safe for simple dark line-only geometry.
    # Grouped pad/thermal geometry relies on dark+clear composition and exact path semantics.
    group_counts: dict[str, int] = {}
    for shape in shapes:
        if getattr(shape, "clear", False):
            return False, "contains clear geometry"
        if str(getattr(shape, "kind", "")).lower() != "line":
            return False, "contains fill geometry"
        info = getattr(shape, "info", {}) or {}
        group_id = str(info.get("group_id", "")).strip()
        if group_id:
            group_counts[group_id] = group_counts.get(group_id, 0) + 1
        if _path_has_curve(shape.path):
            return False, "contains curve path elements"
    # If a group has multiple segments, preserve exact grouped topology in path mode.
    if any(v > 1 for v in group_counts.values()):
        return False, "contains grouped multi-segment geometry"
    return True, "safe"


def _path_has_curve(path: QPainterPath) -> bool:
    count = path.elementCount()
    for i in range(count):
        e = path.elementAt(i)
        if hasattr(e, "isCurveTo") and callable(getattr(e, "isCurveTo")):
            try:
                if bool(e.isCurveTo()):
                    return True
            except Exception:
                pass
        et = getattr(e, "type", None)
        # Qt element types: 0 MoveTo, 1 LineTo, 2 CurveTo, 3 CurveToData
        if et in (2, 3):
            return True
    return False


class _LayerFillItem(QGraphicsPathItem):
    """Single-item dark/clear fill compositor for one layer.

    Drawing clear geometry via path subtraction can create numeric artifacts on
    large compound polygons. This item paints dark fill first, then paints clear
    shapes in background color clipped to dark fill.
    """

    def __init__(
        self,
        dark_paths: list[QPainterPath],
        clear_paths: list[QPainterPath],
        dark_color: QColor,
        clear_color: QColor,
    ) -> None:
        super().__init__()
        self._dark_paths = [QPainterPath(p) for p in dark_paths]
        self._clear_paths = [QPainterPath(p) for p in clear_paths]
        self._dark_color = QColor(dark_color)
        self._clear_color = QColor(clear_color)
        bounds = None
        for p in self._dark_paths + self._clear_paths:
            rect = p.boundingRect()
            bounds = rect if bounds is None else bounds.united(rect)
        if bounds is None:
            bounds = QPainterPath().boundingRect()
        self.setPath(QPainterPath())
        self._bounds = bounds
        self.setPen(Qt.NoPen)

    def boundingRect(self):  # noqa: ANN201, N802
        return self._bounds

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: ANN001
        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setPen(Qt.NoPen)
            if self._dark_paths:
                painter.setBrush(self._dark_color)
                for p in self._dark_paths:
                    if p.isEmpty():
                        continue
                    painter.drawPath(p)
            if self._clear_paths:
                painter.save()
                painter.setCompositionMode(QPainter.CompositionMode_Clear)
                painter.setBrush(Qt.black)
                for p in self._clear_paths:
                    if p.isEmpty():
                        continue
                    painter.drawPath(p)
                painter.restore()
        finally:
            painter.restore()
