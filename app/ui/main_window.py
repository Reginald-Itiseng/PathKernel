from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QTextEdit,
)

from app.core.io import import_files, scan_folder
from app.core.project import Project
from app.ui.widgets import GraphicsCanvas


LOGGER = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Minimal Gerber/Excellon parser + viewer shell."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PathKernel - Viewer")
        self.resize(1200, 800)

        self.project = Project()
        self.scene_items: dict[int, object] = {}

        self.canvas = GraphicsCanvas(self)
        self.setCentralWidget(self.canvas)

        self.layer_list = QListWidget(self)
        self.layer_list.setSelectionMode(QListWidget.SingleSelection)

        self.metadata = QTextEdit(self)
        self.metadata.setReadOnly(True)

        self._build_docks()
        self._build_menus()
        self._build_statusbar()
        self._connect_signals()

    def _build_docks(self) -> None:
        layers_dock = QDockWidget("Layers", self)
        layers_dock.setWidget(self.layer_list)
        layers_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.addDockWidget(Qt.LeftDockWidgetArea, layers_dock)

        metadata_dock = QDockWidget("Metadata", self)
        metadata_dock.setWidget(self.metadata)
        metadata_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.addDockWidget(Qt.RightDockWidgetArea, metadata_dock)

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        self.open_gerber_action = QAction("Open Gerber(s)", self)
        self.open_excellon_action = QAction("Open Excellon", self)
        self.open_folder_action = QAction("Open Folder", self)
        self.clear_action = QAction("Clear", self)
        self.exit_action = QAction("Exit", self)

        file_menu.addAction(self.open_gerber_action)
        file_menu.addAction(self.open_excellon_action)
        file_menu.addAction(self.open_folder_action)
        file_menu.addSeparator()
        file_menu.addAction(self.clear_action)
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)

        view_menu = self.menuBar().addMenu("View")
        self.fit_action = QAction("Fit", self)
        view_menu.addAction(self.fit_action)

    def _build_statusbar(self) -> None:
        self.cursor_label = QLabel("X: 0.000 mm, Y: 0.000 mm", self)
        self.zoom_label = QLabel("Zoom: 100%", self)
        self.statusBar().addPermanentWidget(self.cursor_label)
        self.statusBar().addPermanentWidget(self.zoom_label)

    def _connect_signals(self) -> None:
        self.open_gerber_action.triggered.connect(self._open_gerbers)
        self.open_excellon_action.triggered.connect(self._open_excellon)
        self.open_folder_action.triggered.connect(self._open_folder)
        self.clear_action.triggered.connect(self._clear_project)
        self.exit_action.triggered.connect(self.close)
        self.fit_action.triggered.connect(self.canvas.fit_scene)

        self.layer_list.itemChanged.connect(self._on_layer_item_changed)
        self.layer_list.currentRowChanged.connect(self._on_layer_selected)

        self.canvas.cursor_moved.connect(self._on_cursor_moved)
        self.canvas.zoom_changed.connect(self._on_zoom_changed)
        self.canvas.scene_clicked.connect(self._on_scene_clicked)

    def _open_gerbers(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Open Gerber Files",
            "",
            "Gerber Files (*.gtl *.gbl *.gto *.gbo *.gts *.gbs *.gko *.gm1 *.gbr *.pho *.art);;All Files (*)",
        )
        self._import_paths([Path(p) for p in paths])

    def _open_excellon(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Open Excellon Files",
            "",
            "Excellon Files (*.drl *.txt *.xln *.drd);;All Files (*)",
        )
        self._import_paths([Path(p) for p in paths])

    def _open_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Open Folder")
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
        try:
            import_files(paths, self.project)
        except Exception as exc:
            LOGGER.exception("Import failed")
            QMessageBox.warning(self, "Import Failed", str(exc))
            return
        self._rebuild_scene()
        self.statusBar().showMessage(f"Imported {len(paths)} file(s)", 3000)

    def _clear_project(self) -> None:
        self.project.clear()
        self.layer_list.clear()
        self.metadata.clear()
        self.canvas.clear_scene()
        self.scene_items.clear()

    def _rebuild_scene(self) -> None:
        self.canvas.clear_scene()
        self.scene_items.clear()

        for idx, layer in enumerate(self.project.layers):
            if layer.visible:
                item = self.canvas.add_layer(layer, layer_index=idx)
                self.scene_items[idx] = item

        self.canvas.fit_scene()
        self._rebuild_layer_list()

    def _rebuild_layer_list(self) -> None:
        self.layer_list.blockSignals(True)
        self.layer_list.clear()
        for layer in self.project.layers:
            item = QListWidgetItem(f"{layer.name} [{layer.kind}]")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            item.setCheckState(Qt.Checked if layer.visible else Qt.Unchecked)
            self.layer_list.addItem(item)
        self.layer_list.blockSignals(False)

    def _on_layer_item_changed(self, item: QListWidgetItem) -> None:
        row = self.layer_list.row(item)
        if row < 0 or row >= len(self.project.layers):
            return
        self.project.layers[row].visible = item.checkState() == Qt.Checked
        self._rebuild_scene()

    def _on_layer_selected(self, index: int) -> None:
        if index < 0 or index >= len(self.project.layers):
            self.metadata.clear()
            return
        layer = self.project.layers[index]
        lines = [
            f"name: {layer.name}",
            f"kind: {layer.kind}",
            f"path: {layer.path}",
            f"visible: {layer.visible}",
            f"role: {layer.role}",
        ]
        for k, v in sorted(layer.metadata.items()):
            lines.append(f"{k}: {v}")
        self.metadata.setPlainText("\n".join(lines))

    def _on_cursor_moved(self, x_mm: float, y_mm: float) -> None:
        self.cursor_label.setText(f"X: {x_mm:.3f} mm, Y: {y_mm:.3f} mm")

    def _on_zoom_changed(self, zoom: float) -> None:
        self.zoom_label.setText(f"Zoom: {zoom * 100.0:.0f}%")

    def _on_scene_clicked(self, x_mm: float, y_mm: float) -> None:
        layer_index, info = self.canvas.inspect_at(x_mm, y_mm, preferred_layer_index=self.layer_list.currentRow())
        if layer_index is None or info is None:
            self.statusBar().showMessage("No primitive at cursor", 1500)
            return
        if 0 <= layer_index < self.layer_list.count():
            self.layer_list.setCurrentRow(layer_index)
        lines = [f"{k}: {v}" for k, v in sorted(info.items())]
        self.metadata.setPlainText("\n".join(lines))
