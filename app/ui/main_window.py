from __future__ import annotations

"""Main application window.

This file coordinates file import, project model updates, and scene rebuilds.
"""

from pathlib import Path
import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QTextEdit,
    QWidget,
    QVBoxLayout,
)

from app.core.io import import_files, scan_folder
from app.core.project import Layer, Project
from app.ui.widgets import FitToolbar, GraphicsCanvas, LayerPanel


LOGGER = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Top-level UI shell for CAM layer import and inspection."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PathKernel Viewer")
        self.resize(1280, 840)

        self.project = Project()
        self.scene_items: dict[int, object] = {}

        self.canvas = GraphicsCanvas(self)
        self.fit_toolbar = FitToolbar(on_fit=self.canvas.fit_scene, parent=self)
        self.layer_panel = LayerPanel(self)
        self.metadata = QTextEdit(self)
        self.metadata.setReadOnly(True)

        self._build_layout()
        self._build_menus()
        self._build_statusbar()
        self._connect_signals()

    def _build_layout(self) -> None:
        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.fit_toolbar)
        layout.addWidget(self.canvas)
        self.setCentralWidget(container)

        layer_dock = QDockWidget("Layers", self)
        layer_dock.setWidget(self.layer_panel)
        layer_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.addDockWidget(Qt.LeftDockWidgetArea, layer_dock)

        metadata_dock = QDockWidget("Metadata", self)
        metadata_dock.setWidget(self.metadata)
        metadata_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.addDockWidget(Qt.LeftDockWidgetArea, metadata_dock)

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("File")

        self.open_gerber_action = QAction("Open Gerber(s)", self)
        self.open_excellon_action = QAction("Open Excellon", self)
        self.open_folder_action = QAction("Open Folder", self)
        self.exit_action = QAction("Exit", self)

        file_menu.addAction(self.open_gerber_action)
        file_menu.addAction(self.open_excellon_action)
        file_menu.addAction(self.open_folder_action)
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)

    def _build_statusbar(self) -> None:
        self.cursor_label = QLabel("X: 0.000 mm, Y: 0.000 mm", self)
        self.zoom_label = QLabel("Zoom: 100%", self)
        self.statusBar().addPermanentWidget(self.cursor_label)
        self.statusBar().addPermanentWidget(self.zoom_label)

    def _connect_signals(self) -> None:
        self.open_gerber_action.triggered.connect(self._open_gerbers)
        self.open_excellon_action.triggered.connect(self._open_excellon)
        self.open_folder_action.triggered.connect(self._open_folder)
        self.exit_action.triggered.connect(self.close)
        self.layer_panel.visibility_changed.connect(self._on_layer_visibility_changed)
        self.layer_panel.selection_changed.connect(self._on_layer_selected)
        self.canvas.cursor_moved.connect(self._on_cursor_moved)
        self.canvas.zoom_changed.connect(self._on_zoom_changed)

    def _open_gerbers(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Open Gerber Files",
            "",
            "Gerber Files (*.gtl *.gbl *.gto *.gbo *.gts *.gbs *.gko *.gm1 *.gbr *.pho *.art);;All Files (*)",
        )
        self._import_paths([Path(p) for p in files])

    def _open_excellon(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Open Excellon Files",
            "",
            "Excellon Files (*.drl *.txt *.xln *.drd);;All Files (*)",
        )
        self._import_paths([Path(p) for p in files])

    def _open_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Open Folder", "")
        if not folder:
            return
        paths = scan_folder(Path(folder))
        if not paths:
            QMessageBox.information(self, "No CAM Files", "No Gerber/Excellon files found in that folder.")
            return
        self._import_paths(paths)

    def _import_paths(self, paths: list[Path]) -> None:
        if not paths:
            return

        # Batch UI updates during import to avoid visible repaints per file/layer.
        self.setUpdatesEnabled(False)
        try:
            for path in paths:
                try:
                    import_files([path], self.project)
                except Exception as exc:
                    LOGGER.exception("Import failed for %s", path)
                    QMessageBox.warning(self, "Import Failed", str(exc))
            self._rebuild_scene()
            self.layer_panel.set_layers(self.project.layers)
            self.canvas.fit_scene()
        finally:
            self.setUpdatesEnabled(True)

    def _render_layer(self, layer: Layer) -> None:
        item = self.canvas.add_layer(layer)
        if not layer.visible:
            item.setVisible(False)
        self.scene_items[self.project.layers.index(layer)] = item

    def _rebuild_scene(self) -> None:
        # Rebuild from model state for deterministic draw order and visibility.
        self.scene_items.clear()
        self.canvas.clear_scene()
        for idx, layer in enumerate(self.project.layers):
            item = self.canvas.add_layer(layer)
            item.setVisible(layer.visible)
            self.scene_items[idx] = item

    def _on_layer_visibility_changed(self, index: int, visible: bool) -> None:
        self.project.set_visibility(index, visible)
        item = self.scene_items.get(index)
        if item is not None:
            item.setVisible(visible)

    def _on_layer_selected(self, index: int) -> None:
        if index < 0 or index >= len(self.project.layers):
            self.metadata.clear()
            return
        layer = self.project.layers[index]
        lines = [
            f"Name: {layer.name}",
            f"Path: {layer.path}",
            f"Kind: {layer.kind}",
            f"Visible: {layer.visible}",
            f"Color: {layer.color}",
            f"Artifact: {layer.rendered_artifact_path or 'not rendered'}",
        ]
        if layer.bbox:
            lines.append(f"BBox: {layer.bbox}")
        for key, value in layer.metadata.items():
            lines.append(f"{key}: {value}")
        self.metadata.setPlainText("\n".join(lines))

    def _on_cursor_moved(self, x_mm: float, y_mm: float) -> None:
        self.cursor_label.setText(f"X: {x_mm:.3f} mm, Y: {y_mm:.3f} mm")

    def _on_zoom_changed(self, zoom_factor: float) -> None:
        self.zoom_label.setText(f"Zoom: {zoom_factor * 100:.0f}%")
