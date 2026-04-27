"""Fallback VisPy renderer with GraphicsCanvas-compatible signal surface."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from app.core.geometry import build_layer_geometry


class VisPyCanvasWidget(QWidget):
    """Step-2 VisPy renderer (read-only parity for layer display/navigation)."""

    cursor_moved = Signal(float, float)
    zoom_changed = Signal(float)
    scene_clicked = Signal(float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._status_label = QLabel(self)
        self._status_label.setAlignment(Qt.AlignCenter)
        self._status_label.setStyleSheet("color: #b6c2cf; background: #1b1f24;")
        layout.addWidget(self._status_label)

        self._vispy_available = False
        self._scene = None
        self._view = None
        self._grid = None
        self._native = None
        self._layer_handles: list[dict[str, Any]] = []
        self._line_width_visuals: list[tuple[Any, float]] = []
        self._bounds: tuple[float, float, float, float] | None = None
        self._isolation_view_mode = "width"
        self._cutout_view_mode = "width"

        try:
            from vispy import scene  # type: ignore

            canvas = scene.SceneCanvas(keys="interactive", show=False, bgcolor="#1b1f24")
            view = canvas.central_widget.add_view()
            view.camera = scene.PanZoomCamera(aspect=1)
            # Match Qt/QGraphicsView visual orientation (Y increases downward on screen).
            view.camera.flip = (False, True, False)
            grid = scene.visuals.GridLines(parent=view.scene, color=(0.25, 0.30, 0.36, 0.55))

            canvas.native.setParent(self)
            layout.removeWidget(self._status_label)
            self._status_label.hide()
            layout.addWidget(canvas.native)

            self._scene = scene
            self._view = view
            self._grid = grid
            self._native = canvas.native
            self._canvas = canvas
            self._vispy_available = True
            self._wire_events()
        except Exception:
            self._status_label.setText("VisPy renderer unavailable. Install vispy to enable preview renderer.")

    def renderer_name(self) -> str:
        return "vispy" if self._vispy_available else "vispy_placeholder"

    def clear_scene(self) -> None:
        if not self._vispy_available:
            return
        for handle in self._layer_handles:
            for visual in handle.get("visuals", []):
                try:
                    visual.parent = None
                except Exception:
                    pass
        self._layer_handles.clear()
        self._line_width_visuals.clear()
        self._bounds = None
        self._update_axes()

    def add_layer(self, layer, layer_index: int):  # noqa: ANN001
        if not self._vispy_available:
            return None
        scene = self._scene
        view = self._view
        if scene is None or view is None:
            return None

        geometry = build_layer_geometry(layer)
        rgba = _hex_to_rgba(getattr(layer, "color", "#7aa2d6"), alpha=1.0)
        visuals: list[Any] = []
        line_batches: dict[float, dict[str, Any]] = {}
        fill_subpath_count = 0
        fill_vertex_count = 0

        for shape in geometry.shapes:
            subpaths = _path_subpaths(shape.path)
            if shape.kind == "line":
                if shape.clear:
                    continue
                width = max(float(shape.line_width), 0.8)
                bucket = line_batches.setdefault(width, {"strip_pos": [], "strip_connect": []})
                for pts in subpaths:
                    tpts = _transform_points(pts, layer)
                    if len(tpts) < 2:
                        continue
                    _append_subpath_strip(bucket["strip_pos"], bucket["strip_connect"], tpts)
                    self._accum_bounds(tpts)
            elif shape.kind == "fill":
                if shape.clear:
                    continue
                fill_subpath_count += len(subpaths)
                fill_vertex_count += sum(len(p) for p in subpaths)

        # Render dense fill-heavy layers as outlines to keep VisPy responsive.
        render_fill_as_outline = fill_subpath_count > 220 or fill_vertex_count > 55_000

        for shape in geometry.shapes:
            if shape.kind != "fill" or shape.clear:
                continue
            subpaths = _path_subpaths(shape.path)
            if render_fill_as_outline:
                width = 1.0
                bucket = line_batches.setdefault(width, {"strip_pos": [], "strip_connect": []})
                for pts in subpaths:
                    tpts = _transform_points(pts, layer)
                    if len(tpts) < 2:
                        continue
                    if (tpts[0][0] != tpts[-1][0]) or (tpts[0][1] != tpts[-1][1]):
                        tpts = [*tpts, tpts[0]]
                    _append_subpath_strip(bucket["strip_pos"], bucket["strip_connect"], tpts)
                    self._accum_bounds(tpts)
            else:
                for pts in subpaths:
                    tpts = _transform_points(pts, layer)
                    if len(tpts) < 3:
                        continue
                    if (tpts[0][0] != tpts[-1][0]) or (tpts[0][1] != tpts[-1][1]):
                        tpts = [*tpts, tpts[0]]
                    fill_rgba = (rgba[0], rgba[1], rgba[2], rgba[3])
                    try:
                        v = scene.visuals.Polygon(
                            pos=np.asarray(tpts, dtype=np.float64),
                            color=fill_rgba,
                            border_color=None,
                            parent=view.scene,
                        )
                    except Exception:
                        v = scene.visuals.Line(
                            pos=np.asarray(tpts, dtype=np.float64),
                            color=fill_rgba,
                            width=1.0,
                            method="gl",
                            parent=view.scene,
                        )
                    visuals.append(v)
                    self._accum_bounds(tpts)

        # "Single visual" batching: one Line visual per width bucket.
        for width, bucket in line_batches.items():
            if len(bucket["strip_pos"]) < 2:
                continue
            pos_arr = np.asarray(bucket["strip_pos"], dtype=np.float64)
            connect_flags = list(bucket["strip_connect"])
            try:
                v = scene.visuals.Line(
                    pos=pos_arr,
                    color=rgba,
                    width=1.0,
                    method="agg",
                    antialias=True,
                    connect=connect_flags,
                    parent=view.scene,
                )
                visuals.append(v)
                self._line_width_visuals.append((v, float(width)))
            except Exception:
                # Fallback if bool-connect path is unsupported on current backend.
                v = scene.visuals.Line(
                    pos=pos_arr,
                    color=rgba,
                    width=1.0,
                    method="gl",
                    antialias=True,
                    connect="strip",
                    parent=view.scene,
                )
                visuals.append(v)
                self._line_width_visuals.append((v, float(width)))

        handle = {"layer_index": layer_index, "visuals": visuals}
        self._layer_handles.append(handle)
        self._update_axes()
        self._update_line_visual_widths()
        try:
            self._canvas.update()
        except Exception:
            pass
        return handle

    def remove_layer(self, handle) -> None:  # noqa: ANN001
        if not self._vispy_available or handle is None:
            return
        visuals = handle.get("visuals", []) if isinstance(handle, dict) else []
        for visual in visuals:
            try:
                visual.parent = None
            except Exception:
                pass
        self._line_width_visuals = [(v, mm) for (v, mm) in self._line_width_visuals if v not in visuals]
        if isinstance(handle, dict) and handle in self._layer_handles:
            self._layer_handles.remove(handle)
        self._recompute_bounds()
        self._update_axes()
        self._update_line_visual_widths()
        try:
            self._canvas.update()
        except Exception:
            pass

    def fit_scene(self) -> None:
        if not self._vispy_available or self._view is None:
            return
        if self._bounds is None:
            return
        x0, y0, x1, y1 = self._bounds
        w = max(1e-6, x1 - x0)
        h = max(1e-6, y1 - y0)
        margin = 0.06
        self._view.camera.rect = (x0 - (w * margin), y0 - (h * margin), w * (1 + 2 * margin), h * (1 + 2 * margin))
        self._update_line_visual_widths()
        self.zoom_changed.emit(1.0)

    def zoom_in(self, factor: float = 1.2) -> None:
        self._zoom_by(1.0 / max(float(factor), 1e-6))

    def zoom_out(self, factor: float = 1.2) -> None:
        self._zoom_by(max(float(factor), 1e-6))

    def reset_view(self) -> None:
        self.fit_scene()

    def set_grid_visible(self, visible: bool) -> None:
        if self._grid is not None:
            self._grid.visible = bool(visible)

    def set_isolation_view_mode(self, mode: str) -> None:
        self._isolation_view_mode = (mode or "").strip().lower() or "width"

    def set_cutout_view_mode(self, mode: str) -> None:
        self._cutout_view_mode = (mode or "").strip().lower() or "width"

    def inspect_at(self, x_mm: float, y_mm: float, preferred_layer_index: int | None = None):  # noqa: ARG002
        return None, None

    def start_task_overlay(self, title: str) -> None:  # noqa: ARG002
        pass

    def append_task_overlay_line(self, text: str) -> None:  # noqa: ARG002
        pass

    def finish_task_overlay(self, *, success: bool, detail: str = "") -> None:  # noqa: ARG002
        pass

    def _wire_events(self) -> None:
        if not self._vispy_available:
            return
        try:
            self._canvas.events.mouse_move.connect(self._on_mouse_move)
            self._canvas.events.mouse_release.connect(self._on_mouse_release)
            self._view.camera.events.changed.connect(self._on_camera_changed)
        except Exception:
            pass

    def _on_camera_changed(self, event) -> None:  # noqa: ANN001, ARG002
        self._update_line_visual_widths()

    def _on_mouse_move(self, event) -> None:  # noqa: ANN001
        if not self._vispy_available:
            return
        scene_pt = self._map_canvas_to_scene(event.pos)
        if scene_pt is None:
            return
        self.cursor_moved.emit(float(scene_pt[0]), float(scene_pt[1]))

    def _on_mouse_release(self, event) -> None:  # noqa: ANN001
        if not self._vispy_available:
            return
        if getattr(event, "button", None) != 1:
            return
        scene_pt = self._map_canvas_to_scene(event.pos)
        if scene_pt is None:
            return
        self.scene_clicked.emit(float(scene_pt[0]), float(scene_pt[1]))

    def _map_canvas_to_scene(self, pos) -> tuple[float, float] | None:  # noqa: ANN001
        try:
            tr = self._view.scene.node_transform(self._canvas.scene)
            mapped = tr.imap((float(pos[0]), float(pos[1]), 0.0, 1.0))
            return float(mapped[0]), float(mapped[1])
        except Exception:
            return None

    def _zoom_by(self, scale: float) -> None:
        if not self._vispy_available or self._view is None:
            return
        rect = self._view.camera.rect
        cx = rect.left + (rect.width * 0.5)
        cy = rect.bottom + (rect.height * 0.5)
        new_w = max(rect.width * scale, 1e-6)
        new_h = max(rect.height * scale, 1e-6)
        self._view.camera.rect = (cx - (new_w * 0.5), cy - (new_h * 0.5), new_w, new_h)
        self._update_line_visual_widths()
        self.zoom_changed.emit(1.0)

    def _accum_bounds(self, points: list[tuple[float, float]]) -> None:
        if not points:
            return
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        x0 = min(xs)
        y0 = min(ys)
        x1 = max(xs)
        y1 = max(ys)
        if self._bounds is None:
            self._bounds = (x0, y0, x1, y1)
            return
        bx0, by0, bx1, by1 = self._bounds
        self._bounds = (min(bx0, x0), min(by0, y0), max(bx1, x1), max(by1, y1))

    def _recompute_bounds(self) -> None:
        self._bounds = None
        for handle in self._layer_handles:
            for visual in handle.get("visuals", []):
                try:
                    pos = getattr(visual, "pos", None)
                    if pos is None:
                        continue
                    pts = [(float(p[0]), float(p[1])) for p in pos]
                    self._accum_bounds(pts)
                except Exception:
                    continue

    def _update_axes(self) -> None:
        # Step-2 keeps only grid + geometry for stability; axis/ruler parity later.
        pass

    def _update_line_visual_widths(self) -> None:
        if not self._vispy_available or self._view is None or self._native is None:
            return
        rect = self._view.camera.rect
        world_w = float(getattr(rect, "width", 0.0) or 0.0)
        px_w = max(int(self._native.width()), 1)
        px_per_mm = max(px_w / max(world_w, 1e-9), 1e-9)
        for visual, width_mm in self._line_width_visuals:
            width_px = max(1.0, min(90.0, float(width_mm) * px_per_mm))
            try:
                visual.set_data(width=width_px)
            except Exception:
                try:
                    visual._width = width_px  # type: ignore[attr-defined]
                    visual.update()
                except Exception:
                    continue


def _hex_to_rgba(color: str, alpha: float = 1.0) -> tuple[float, float, float, float]:
    c = (color or "").strip().lstrip("#")
    if len(c) != 6:
        return 0.62, 0.72, 0.82, max(0.0, min(1.0, alpha))
    try:
        r = int(c[0:2], 16) / 255.0
        g = int(c[2:4], 16) / 255.0
        b = int(c[4:6], 16) / 255.0
    except Exception:
        return 0.62, 0.72, 0.82, max(0.0, min(1.0, alpha))
    return r, g, b, max(0.0, min(1.0, alpha))


def _path_subpaths(path) -> list[list[tuple[float, float]]]:
    out: list[list[tuple[float, float]]] = []
    try:
        polys = path.toSubpathPolygons()
    except Exception:
        polys = []
    for poly in polys:
        pts: list[tuple[float, float]] = []
        for pt in poly:
            x = float(pt.x())
            y = float(pt.y())
            if not pts:
                pts.append((x, y))
                continue
            lx, ly = pts[-1]
            if abs(lx - x) > 1e-9 or abs(ly - y) > 1e-9:
                pts.append((x, y))
        if len(pts) >= 2:
            out.append(pts)
    return out


def _transform_points(points: list[tuple[float, float]], layer) -> list[tuple[float, float]]:  # noqa: ANN001
    sx = -1.0 if bool(getattr(layer, "mirror_x", False)) else 1.0
    sy = -1.0 if bool(getattr(layer, "mirror_y", False)) else 1.0
    rot = math.radians(float(getattr(layer, "rotation_deg", 0.0) or 0.0))
    cr = math.cos(rot)
    sr = math.sin(rot)
    tx = float(getattr(layer, "offset_x_mm", 0.0) or 0.0)
    ty = -float(getattr(layer, "offset_y_mm", 0.0) or 0.0)

    out: list[tuple[float, float]] = []
    for x, y in points:
        px = x * sx
        py = y * sy
        rx = (px * cr) - (py * sr)
        ry = (px * sr) + (py * cr)
        out.append((rx + tx, ry + ty))
    return out


def _append_subpath_strip(
    strip_pos: list[tuple[float, float]],
    strip_connect: list[bool],
    subpath: list[tuple[float, float]],
) -> None:
    if len(subpath) < 2:
        return
    if not strip_pos:
        strip_pos.extend(subpath)
        strip_connect.extend([True] * (len(subpath) - 1))
        return
    strip_pos.extend(subpath)
    # Break edge from previous subpath tail to this subpath head, then keep flowing inside subpath.
    strip_connect.extend([False] + ([True] * (len(subpath) - 1)))
