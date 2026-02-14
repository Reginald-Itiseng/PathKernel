from __future__ import annotations

import logging
import multiprocessing as mp
from pathlib import Path
from queue import Empty

from PySide6.QtCore import QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QFileDialog,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QDoubleSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.io import import_files, scan_folder
from app.core.background_tasks import (
    cutout_params_payload,
    isolation_params_payload,
    run_task_process_entry,
)
from app.core.cutout import CutoutParams, build_cutout_toolpath_layer, extract_cutout_loops
from app.core.isolation import IsolationParams, build_isolation_layer
from app.core.project import Project
from app.core.project_store import deserialize_layer, load_project, save_project, serialize_layer
from app.ui.widgets import GraphicsCanvas


LOGGER = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Minimal Gerber/Excellon parser + viewer shell."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PathKernel - Viewer")
        self.resize(1200, 800)

        self.project = Project()
        self._project_file_path: Path | None = None
        self.scene_items: dict[int, object] = {}
        self._cutout_preview_layer = None
        self._cutout_preview_item = None
        self._cutout_plan_active = False
        self._cutout_plan_source_layer_index: int | None = None
        self._cutout_plan_loops = []
        self._cutout_plan_compensations: list[str] = []
        self._task_running = False
        self._task_process: mp.Process | None = None
        self._task_queue = None
        self._task_poll_timer = QTimer(self)
        self._task_poll_timer.setInterval(80)
        self._task_poll_timer.timeout.connect(self._poll_task_queue)
        self._task_on_success = None
        self._task_failure_title = "Task Failed"
        self._task_result_received = False

        self.canvas = GraphicsCanvas(self)
        self.setCentralWidget(self.canvas)

        self.layer_tree = QTreeWidget(self)
        self.layer_tree.setColumnCount(2)
        self.layer_tree.setHeaderHidden(True)
        self.layer_tree.setRootIsDecorated(True)
        self.layer_tree.setUniformRowHeights(True)
        self.layer_tree.setIndentation(14)
        self.layer_tree.setTextElideMode(Qt.ElideRight)
        self.layer_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.layer_tree.header().setSectionResizeMode(1, QHeaderView.Fixed)
        self.layer_tree.header().setStretchLastSection(False)
        self.layer_tree.setColumnWidth(1, 18)
        self._layer_index_to_item: dict[int, QTreeWidgetItem] = {}
        self._import_group_expanded = False
        self._generated_group_expanded = True

        self.metadata = QTextEdit(self)
        self.metadata.setReadOnly(True)
        self.cutout_panel = self._build_cutout_panel()

        self._apply_layers_panel_style()
        self._build_docks()
        self._build_menus()
        self._build_statusbar()
        self._connect_signals()
        self._try_restore_last_project()
        self._update_window_title()

    def _build_docks(self) -> None:
        layers_dock = QDockWidget("Layers", self)
        layers_dock.setWidget(self.layer_tree)
        layers_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.addDockWidget(Qt.LeftDockWidgetArea, layers_dock)

        metadata_dock = QDockWidget("Metadata", self)
        metadata_dock.setWidget(self.metadata)
        metadata_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.addDockWidget(Qt.RightDockWidgetArea, metadata_dock)

        self.cutout_dock = QDockWidget("Cutout Planner", self)
        self.cutout_dock.setWidget(self.cutout_panel)
        self.cutout_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.addDockWidget(Qt.RightDockWidgetArea, self.cutout_dock)
        self.cutout_dock.hide()

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        self.open_project_action = QAction("Open Project...", self)
        self.save_project_action = QAction("Save Project", self)
        self.save_project_as_action = QAction("Save Project As...", self)
        self.open_gerber_action = QAction("Open Gerber(s)", self)
        self.open_excellon_action = QAction("Open Excellon", self)
        self.open_folder_action = QAction("Open Folder", self)
        self.clear_action = QAction("Clear", self)
        self.exit_action = QAction("Exit", self)
        self.open_project_action.setShortcut("Ctrl+Shift+O")
        self.save_project_action.setShortcut("Ctrl+S")
        self.save_project_as_action.setShortcut("Ctrl+Shift+S")

        file_menu.addAction(self.open_project_action)
        file_menu.addAction(self.save_project_action)
        file_menu.addAction(self.save_project_as_action)
        file_menu.addSeparator()
        file_menu.addAction(self.open_gerber_action)
        file_menu.addAction(self.open_excellon_action)
        file_menu.addAction(self.open_folder_action)
        file_menu.addSeparator()
        file_menu.addAction(self.clear_action)
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)

        view_menu = self.menuBar().addMenu("View")
        self.fit_action = QAction("Fit", self)
        self.zoom_in_action = QAction("Zoom In", self)
        self.zoom_out_action = QAction("Zoom Out", self)
        self.actual_size_action = QAction("Actual Size (1:1)", self)
        self.grid_action = QAction("Show Grid", self)
        self.grid_action.setCheckable(True)
        self.grid_action.setChecked(True)
        self.isolation_width_action = QAction("Isolation: Effective Cut Width", self)
        self.isolation_centerline_action = QAction("Isolation: Centerline + Arrows", self)
        self.cutout_width_action = QAction("Cutout: Effective Cut Width", self)
        self.cutout_centerline_action = QAction("Cutout: Centerline + Arrows", self)
        self.isolation_width_action.setCheckable(True)
        self.isolation_centerline_action.setCheckable(True)
        self.isolation_width_action.setChecked(True)
        self.cutout_width_action.setCheckable(True)
        self.cutout_centerline_action.setCheckable(True)
        self.cutout_width_action.setChecked(True)
        self.isolation_view_group = QActionGroup(self)
        self.isolation_view_group.setExclusive(True)
        self.isolation_view_group.addAction(self.isolation_width_action)
        self.isolation_view_group.addAction(self.isolation_centerline_action)
        self.cutout_view_group = QActionGroup(self)
        self.cutout_view_group.setExclusive(True)
        self.cutout_view_group.addAction(self.cutout_width_action)
        self.cutout_view_group.addAction(self.cutout_centerline_action)
        self.fit_action.setShortcut("F")
        self.zoom_in_action.setShortcut("+")
        self.zoom_out_action.setShortcut("-")
        self.actual_size_action.setShortcut("1")
        view_menu.addAction(self.fit_action)
        view_menu.addAction(self.zoom_in_action)
        view_menu.addAction(self.zoom_out_action)
        view_menu.addAction(self.actual_size_action)
        view_menu.addSeparator()
        view_menu.addAction(self.grid_action)
        view_menu.addSeparator()
        view_menu.addAction(self.isolation_width_action)
        view_menu.addAction(self.isolation_centerline_action)
        view_menu.addSeparator()
        view_menu.addAction(self.cutout_width_action)
        view_menu.addAction(self.cutout_centerline_action)

        tools_menu = self.menuBar().addMenu("Tools")
        self.generate_isolation_action = QAction("Generate Isolation Geometry", self)
        self.generate_cutout_toolpath_action = QAction("Generate Cutout Toolpath", self)
        tools_menu.addAction(self.generate_isolation_action)
        tools_menu.addAction(self.generate_cutout_toolpath_action)

    def _build_statusbar(self) -> None:
        self.cursor_label = QLabel("X: 0.000 mm, Y: 0.000 mm", self)
        self.zoom_label = QLabel("Zoom: 100%", self)
        self.statusBar().addPermanentWidget(self.cursor_label)
        self.statusBar().addPermanentWidget(self.zoom_label)

    def _connect_signals(self) -> None:
        self.open_project_action.triggered.connect(self._open_project_file)
        self.save_project_action.triggered.connect(self._save_project_file)
        self.save_project_as_action.triggered.connect(self._save_project_file_as)
        self.open_gerber_action.triggered.connect(self._open_gerbers)
        self.open_excellon_action.triggered.connect(self._open_excellon)
        self.open_folder_action.triggered.connect(self._open_folder)
        self.clear_action.triggered.connect(self._clear_project)
        self.exit_action.triggered.connect(self.close)
        self.fit_action.triggered.connect(self.canvas.fit_scene)
        self.zoom_in_action.triggered.connect(self.canvas.zoom_in)
        self.zoom_out_action.triggered.connect(self.canvas.zoom_out)
        self.actual_size_action.triggered.connect(self.canvas.reset_view)
        self.grid_action.toggled.connect(self.canvas.set_grid_visible)
        self.isolation_width_action.triggered.connect(lambda: self.canvas.set_isolation_view_mode("width"))
        self.isolation_centerline_action.triggered.connect(lambda: self.canvas.set_isolation_view_mode("centerline"))
        self.cutout_width_action.triggered.connect(lambda: self.canvas.set_cutout_view_mode("width"))
        self.cutout_centerline_action.triggered.connect(lambda: self.canvas.set_cutout_view_mode("centerline"))
        self.generate_isolation_action.triggered.connect(self._generate_isolation_geometry)
        self.generate_cutout_toolpath_action.triggered.connect(self._open_cutout_planner)

        self.layer_tree.itemChanged.connect(self._on_layer_item_changed)
        self.layer_tree.currentItemChanged.connect(self._on_layer_selected)
        self.layer_tree.itemExpanded.connect(self._on_group_expand)
        self.layer_tree.itemCollapsed.connect(self._on_group_collapse)

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
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            return
        if not paths:
            return
        try:
            import_files(paths, self.project)
        except Exception as exc:
            LOGGER.exception("Import failed")
            QMessageBox.warning(self, "Import Failed", str(exc))
            return
        self._rebuild_scene()
        self._project_file_path = None
        self._update_window_title()
        self.statusBar().showMessage(f"Imported {len(paths)} file(s)", 3000)

    def _open_project_file(self) -> None:
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            return
        start_dir = self._project_file_path.parent if self._project_file_path else Path.cwd()
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Open Project",
            str(start_dir),
            "PathKernel Project (*.pkproj);;JSON Files (*.json);;All Files (*)",
        )
        if not filename:
            return
        self._load_project_file(Path(filename))

    def _save_project_file(self) -> None:
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            return
        if self._project_file_path is None:
            self._save_project_file_as()
            return
        self._save_project_to_path(self._project_file_path)

    def _save_project_file_as(self) -> None:
        start_dir = self._project_file_path.parent if self._project_file_path else Path.cwd()
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Save Project",
            str(start_dir / "project.pkproj"),
            "PathKernel Project (*.pkproj);;JSON Files (*.json);;All Files (*)",
        )
        if not filename:
            return
        path = Path(filename)
        if path.suffix == "":
            path = path.with_suffix(".pkproj")
        self._save_project_to_path(path)

    def _save_project_to_path(self, path: Path) -> None:
        try:
            save_project(path, self.project)
        except Exception as exc:
            LOGGER.exception("Save project failed")
            QMessageBox.warning(self, "Save Project Failed", str(exc))
            return
        self._project_file_path = path
        self._set_last_project_path(path)
        self._update_window_title()
        self.statusBar().showMessage(f"Project saved: {path.name}", 3000)

    def _load_project_file(self, path: Path) -> None:
        try:
            loaded = load_project(path)
        except Exception as exc:
            LOGGER.exception("Open project failed")
            QMessageBox.warning(self, "Open Project Failed", str(exc))
            return
        self.project = loaded
        self._project_file_path = path
        self._set_last_project_path(path)
        self._close_cutout_planner()
        self._rebuild_scene()
        self._update_window_title()
        self.statusBar().showMessage(f"Project loaded: {path.name}", 3000)

    def _set_last_project_path(self, path: Path) -> None:
        settings = QSettings()
        settings.setValue("session/last_project_path", str(path))
        settings.sync()

    def _try_restore_last_project(self) -> None:
        settings = QSettings()
        value = settings.value("session/last_project_path", "")
        if not value:
            return
        path = Path(str(value))
        if not path.exists():
            return
        self._load_project_file(path)

    def _update_window_title(self) -> None:
        suffix = self._project_file_path.name if self._project_file_path else "Untitled"
        self.setWindowTitle(f"PathKernel - Viewer [{suffix}]")

    def _clear_project(self) -> None:
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            return
        self.project.clear()
        self._project_file_path = None
        self.layer_tree.clear()
        self._layer_index_to_item.clear()
        self._clear_cutout_preview()
        self._cutout_plan_active = False
        self.metadata.clear()
        self.canvas.clear_scene()
        self.scene_items.clear()
        self._update_window_title()

    def _rebuild_scene(self) -> None:
        self._refresh_canvas(fit=True)
        self._rebuild_layer_list()

    def _refresh_canvas(self, *, fit: bool) -> None:
        self.canvas.clear_scene()
        self.scene_items.clear()

        for idx, layer in enumerate(self.project.layers):
            if layer.visible:
                item = self.canvas.add_layer(layer, layer_index=idx)
                self.scene_items[idx] = item
        if self._cutout_plan_active:
            self._render_cutout_preview()

        if fit:
            self.canvas.fit_scene()

    def _rebuild_layer_list(self) -> None:
        self.layer_tree.blockSignals(True)
        self.layer_tree.clear()
        self._layer_index_to_item.clear()

        imported: list[tuple[int, object]] = []
        generated: list[tuple[int, object]] = []
        for idx, layer in enumerate(self.project.layers):
            if self._is_generated_layer(layer):
                generated.append((idx, layer))
            else:
                imported.append((idx, layer))

        import_group = self._append_layer_group("Imported Geometry", imported, group_key="imported")
        generated_group = self._append_layer_group("Generated Geometry", generated, group_key="generated")
        import_group.setExpanded(self._import_group_expanded)
        generated_group.setExpanded(self._generated_group_expanded)

        self.layer_tree.blockSignals(False)

    def _on_layer_item_changed(self, item: QTreeWidgetItem, column: int) -> None:  # noqa: ARG002
        layer_index = self._layer_index_from_item(item)
        if layer_index is None or layer_index < 0 or layer_index >= len(self.project.layers):
            return
        self.project.layers[layer_index].visible = item.checkState(0) == Qt.Checked
        # Avoid rebuilding the layer tree from inside itemChanged; this can re-enter Qt model updates.
        self._refresh_canvas(fit=False)

    def _on_layer_selected(self, current: QTreeWidgetItem | None, previous: QTreeWidgetItem | None) -> None:  # noqa: ARG002
        layer_index = self._layer_index_from_item(current)
        if layer_index is None or layer_index < 0 or layer_index >= len(self.project.layers):
            self.metadata.clear()
            return
        layer = self.project.layers[layer_index]
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
        # Display coordinates in Cartesian convention (positive Y upward).
        self.cursor_label.setText(f"X: {x_mm:.3f} mm, Y: {-y_mm:.3f} mm")

    def _on_zoom_changed(self, zoom: float) -> None:
        self.zoom_label.setText(f"Zoom: {zoom * 100.0:.0f}%")

    def _on_scene_clicked(self, x_mm: float, y_mm: float) -> None:
        if self._cutout_plan_active and self._select_cutout_loop_at(x_mm, y_mm):
            return
        preferred = self._current_layer_index()
        layer_index, info = self.canvas.inspect_at(x_mm, y_mm, preferred_layer_index=preferred)
        if layer_index is None or info is None:
            self.statusBar().showMessage("No primitive at cursor", 1500)
            return
        target_item = self._layer_index_to_item.get(layer_index)
        if target_item is not None:
            parent = target_item.parent()
            if parent is not None:
                parent.setExpanded(True)
            self.layer_tree.setCurrentItem(target_item)
        lines = [f"{k}: {v}" for k, v in sorted(info.items())]
        self.metadata.setPlainText("\n".join(lines))

    def _generate_isolation_geometry(self) -> None:
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            return
        index = self._current_layer_index()
        if index is None or index < 0 or index >= len(self.project.layers):
            QMessageBox.information(self, "Isolation", "Select a Gerber layer first.")
            return

        source_layer = self.project.layers[index]
        source_name = source_layer.name
        if source_layer.kind != "gerber":
            QMessageBox.information(self, "Isolation", "Isolation currently supports Gerber layers only.")
            return

        tool_dia, ok = QInputDialog.getDouble(
            self,
            "Isolation Tool Diameter",
            "Tool diameter (mm):",
            0.2,
            0.001,
            1000.0,
            3,
        )
        if not ok:
            return

        passes, ok = QInputDialog.getInt(
            self,
            "Isolation Passes",
            "Number of passes:",
            1,
            1,
            50,
            1,
        )
        if not ok:
            return

        overlap, ok = QInputDialog.getDouble(
            self,
            "Isolation Overlap",
            "Pass overlap (0.0 - 0.95):",
            0.15,
            0.0,
            0.95,
            2,
        )
        if not ok:
            return

        extra_pad_contours, ok = QInputDialog.getInt(
            self,
            "Extra Pad Contours",
            "Extra contours around pads:",
            0,
            0,
            20,
            1,
        )
        if not ok:
            return

        iso_labels = ["both", "exterior", "interior"]
        selected_label, ok = QInputDialog.getItem(
            self,
            "Isolation Type",
            "Isolation type:",
            iso_labels,
            0,
            False,
        )
        if not ok:
            return

        iso_type_map = {"exterior": 0, "interior": 1, "both": 2}
        params = IsolationParams(
            tool_diameter_mm=float(tool_dia),
            passes=int(passes),
            overlap=float(overlap),
            iso_type=iso_type_map.get(selected_label, 2),
            extra_pad_contours=int(extra_pad_contours),
        )

        def on_done(layer):
            self.project.add_layer(layer)
            self._rebuild_scene()
            self._select_layer_item(len(self.project.layers) - 1)
            self.statusBar().showMessage(f"Isolation generated from '{source_name}'", 4000)

        self._run_process_task(
            title=f"Isolation {source_name}",
            task_name="isolation_generate",
            payload={
                "source_layer": serialize_layer(source_layer),
                "params": isolation_params_payload(params),
            },
            decode_result=lambda data: deserialize_layer(dict(data["generated_layer"])),
            on_success=on_done,
            failure_title="Isolation Failed",
        )

    def _open_cutout_planner(self) -> None:
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            return
        index = self._current_layer_index()
        if index is None or index < 0 or index >= len(self.project.layers):
            QMessageBox.information(self, "Cutout Toolpath", "Select a cutout/gerber layer first.")
            return

        source_layer = self.project.layers[index]
        if source_layer.role != "cutout":
            QMessageBox.information(
                self,
                "Cutout Toolpath",
                "Cutout toolpath can be generated only from a layer with role 'cutout'.",
            )
            return
        if source_layer.kind not in {"gerber", "geometry"}:
            QMessageBox.information(self, "Cutout Toolpath", "Cutout toolpath currently supports Gerber/geometry layers.")
            return

        source_name = source_layer.name

        def on_done(loops):
            if not loops:
                QMessageBox.warning(self, "Cutout Toolpath Failed", "No closed loops were found in this cutout layer.")
                return
            self._cutout_plan_active = True
            self._cutout_plan_source_layer_index = index
            self._cutout_plan_loops = loops
            self._cutout_plan_compensations = self._default_cutout_loop_compensations(loops)
            self._populate_cutout_table()
            self._update_cutout_source_label()
            self.cutout_dock.show()
            self._refresh_canvas(fit=False)

        def decode_loops(data):
            from shapely.geometry import Polygon

            decoded = []
            for item in data.get("loops", []) or []:
                ext = [(float(p[0]), float(p[1])) for p in item.get("exterior", [])]
                interiors = []
                for ring in item.get("interiors", []) or []:
                    interiors.append([(float(p[0]), float(p[1])) for p in ring])
                if len(ext) >= 3:
                    decoded.append(Polygon(ext, interiors))
            return decoded

        self._run_process_task(
            title=f"Cutout Loop Extraction {source_name}",
            task_name="cutout_extract_loops",
            payload={"source_layer": serialize_layer(source_layer)},
            decode_result=decode_loops,
            on_success=on_done,
            failure_title="Cutout Toolpath Failed",
        )

    def _apply_cutout_plan(self) -> None:
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            return
        if not self._cutout_plan_active or self._cutout_plan_source_layer_index is None:
            return
        idx = self._cutout_plan_source_layer_index
        if idx < 0 or idx >= len(self.project.layers):
            return
        source_layer = self.project.layers[idx]
        params = CutoutParams(
            tool_diameter_mm=float(self.cutout_tool_dia_spin.value()),
            compensation="outside",
            loop_compensations=list(self._cutout_plan_compensations),
        )
        source_name = source_layer.name

        def on_done(layer):
            self.project.add_layer(layer)
            self._rebuild_scene()
            self._select_layer_item(len(self.project.layers) - 1)
            self.statusBar().showMessage(f"Cutout toolpath generated from '{source_name}'", 4000)

        self._run_process_task(
            title=f"Cutout Toolpath {source_name}",
            task_name="cutout_generate",
            payload={
                "source_layer": serialize_layer(source_layer),
                "params": cutout_params_payload(params),
            },
            decode_result=lambda data: deserialize_layer(dict(data["generated_layer"])),
            on_success=on_done,
            failure_title="Cutout Toolpath Failed",
        )

    def _close_cutout_planner(self) -> None:
        self._cutout_plan_active = False
        self._cutout_plan_source_layer_index = None
        self._cutout_plan_loops = []
        self._cutout_plan_compensations = []
        self.cutout_loop_table.setRowCount(0)
        self.cutout_source_label.setText("Source: (none)")
        self.cutout_dock.hide()
        self._clear_cutout_preview()
        self._refresh_canvas(fit=False)

    def _append_layer_group(
        self,
        title: str,
        entries: list[tuple[int, object]],
        *,
        group_key: str,
    ) -> QTreeWidgetItem:
        header = QTreeWidgetItem([title, ""])
        header_font = QFont(self.layer_tree.font())
        header_font.setBold(True)
        header.setFont(0, header_font)
        header.setForeground(0, QColor("#c9d1d9"))
        header.setBackground(0, QColor("#2b3139"))
        header.setFlags(Qt.ItemIsEnabled)
        header.setData(0, Qt.UserRole + 2, group_key)
        self.layer_tree.addTopLevelItem(header)
        header_row = self.layer_tree.indexOfTopLevelItem(header)
        if header_row >= 0:
            self.layer_tree.setFirstColumnSpanned(header_row, self.layer_tree.rootIndex(), True)

        if not entries:
            empty = QTreeWidgetItem(["(empty)", ""])
            empty.setForeground(0, QColor("#7f8b98"))
            empty.setFlags(Qt.ItemIsEnabled)
            header.addChild(empty)
            self.layer_tree.setFirstColumnSpanned(0, self.layer_tree.indexFromItem(header), True)
            return header

        for layer_index, layer in entries:
            label = f"{layer.name}  [{layer.kind}]"
            if self._is_generated_layer(layer):
                tag = layer.metadata.get("kind", "").strip().lower()
                if tag:
                    label = f"{layer.name}  [{tag}]"
            item = QTreeWidgetItem([label, ""])
            item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            item.setCheckState(0, Qt.Checked if layer.visible else Qt.Unchecked)
            item.setData(0, Qt.UserRole, layer_index)
            item.setSizeHint(0, QSize(0, 18))
            header.addChild(item)
            delete_button = QPushButton("-", self.layer_tree)
            delete_button.setObjectName("layerDeleteButton")
            delete_button.setToolTip(f"Delete layer '{layer.name}'")
            delete_button.setFixedSize(14, 14)
            delete_button.clicked.connect(lambda _checked=False, idx=layer_index: self._delete_layer(idx))
            holder = QWidget(self.layer_tree)
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 1, 0)
            holder_layout.setSpacing(0)
            holder_layout.addStretch(1)
            holder_layout.addWidget(delete_button, alignment=Qt.AlignRight | Qt.AlignVCenter)
            self.layer_tree.setItemWidget(item, 1, holder)
            self._layer_index_to_item[layer_index] = item

        return header

    def _delete_layer(self, layer_index: int) -> None:
        if layer_index < 0 or layer_index >= len(self.project.layers):
            return
        layer = self.project.layers[layer_index]
        answer = QMessageBox.question(
            self,
            "Delete Layer",
            f"Delete layer '{layer.name}'?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.project.layers.pop(layer_index)
        if self._cutout_plan_source_layer_index is not None:
            if self._cutout_plan_source_layer_index == layer_index:
                self._close_cutout_planner()
            elif self._cutout_plan_source_layer_index > layer_index:
                self._cutout_plan_source_layer_index -= 1
        self._rebuild_scene()
        if self.project.layers:
            self._select_layer_item(min(layer_index, len(self.project.layers) - 1))
        self.statusBar().showMessage(f"Deleted layer '{layer.name}'", 2500)

    def _is_generated_layer(self, layer) -> bool:
        if "derived_from" in layer.metadata:
            return True
        return layer.kind == "geometry"

    def _layer_index_from_item(self, item: QTreeWidgetItem | None) -> int | None:
        if item is None:
            return None
        value = item.data(0, Qt.UserRole)
        return int(value) if isinstance(value, int) else None

    def _current_layer_index(self) -> int | None:
        return self._layer_index_from_item(self.layer_tree.currentItem())

    def _select_layer_item(self, layer_index: int) -> None:
        item = self._layer_index_to_item.get(layer_index)
        if item is None:
            return
        parent = item.parent()
        if parent is not None:
            parent.setExpanded(True)
        self.layer_tree.setCurrentItem(item)

    def _on_group_expand(self, item: QTreeWidgetItem) -> None:
        key = item.data(0, Qt.UserRole + 2)
        if key == "imported":
            self._import_group_expanded = True
        elif key == "generated":
            self._generated_group_expanded = True

    def _on_group_collapse(self, item: QTreeWidgetItem) -> None:
        key = item.data(0, Qt.UserRole + 2)
        if key == "imported":
            self._import_group_expanded = False
        elif key == "generated":
            self._generated_group_expanded = False

    def _apply_layers_panel_style(self) -> None:
        self.layer_tree.setStyleSheet(
            """
            QTreeWidget {
                background-color: #1f2329;
                color: #d0d7de;
                border: 1px solid #30363d;
                outline: none;
                padding: 2px;
            }
            QTreeWidget::item {
                padding: 1px 4px;
                min-height: 16px;
            }
            QTreeWidget::item:selected {
                background-color: #2f81f7;
                color: #ffffff;
            }
            QPushButton#layerDeleteButton {
                background-color: #f2f5f9;
                color: #111827;
                border: 1px solid #6b7280;
                border-radius: 3px;
                font-weight: 700;
                padding: 0px;
            }
            QPushButton#layerDeleteButton:hover {
                background-color: #ef4444;
                color: #ffffff;
                border: 1px solid #b91c1c;
            }
            QPushButton#layerDeleteButton:pressed {
                background-color: #b91c1c;
                color: #ffffff;
            }
            """
        )

    def _build_cutout_panel(self) -> QWidget:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.cutout_source_label = QLabel("Source: (none)", panel)
        layout.addWidget(self.cutout_source_label)
        self.cutout_help_label = QLabel("Click loop geometry in viewport to select loop row.", panel)
        layout.addWidget(self.cutout_help_label)

        self.cutout_tool_dia_spin = QDoubleSpinBox(panel)
        self.cutout_tool_dia_spin.setDecimals(3)
        self.cutout_tool_dia_spin.setRange(0.001, 1000.0)
        self.cutout_tool_dia_spin.setValue(1.0)
        self.cutout_tool_dia_spin.setPrefix("Tool dia (mm): ")
        self.cutout_tool_dia_spin.valueChanged.connect(self._on_cutout_setting_changed)
        layout.addWidget(self.cutout_tool_dia_spin)

        self.cutout_loop_table = QTableWidget(panel)
        self.cutout_loop_table.setColumnCount(3)
        self.cutout_loop_table.setHorizontalHeaderLabels(["Loop", "Area (mm^2)", "Comp"])
        self.cutout_loop_table.verticalHeader().setVisible(False)
        self.cutout_loop_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.cutout_loop_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.cutout_loop_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.cutout_loop_table.itemSelectionChanged.connect(self._refresh_canvas_no_fit)
        layout.addWidget(self.cutout_loop_table, 1)

        self.cutout_apply_button = QPushButton("Generate Toolpath", panel)
        self.cutout_close_button = QPushButton("Close Planner", panel)
        self.cutout_apply_button.clicked.connect(self._apply_cutout_plan)
        self.cutout_close_button.clicked.connect(self._close_cutout_planner)
        layout.addWidget(self.cutout_apply_button)
        layout.addWidget(self.cutout_close_button)
        return panel

    def _populate_cutout_table(self) -> None:
        self.cutout_loop_table.blockSignals(True)
        self.cutout_loop_table.setRowCount(len(self._cutout_plan_loops))
        for row, loop in enumerate(self._cutout_plan_loops):
            idx_item = QTableWidgetItem(str(row + 1))
            idx_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            area_item = QTableWidgetItem(f"{loop.area:.3f}")
            area_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            combo = QComboBox(self.cutout_loop_table)
            combo.addItems(["outside", "inside", "onpath"])
            combo.setCurrentText(self._cutout_plan_compensations[row])
            combo.currentTextChanged.connect(lambda value, r=row: self._on_cutout_comp_changed(r, value))
            self.cutout_loop_table.setItem(row, 0, idx_item)
            self.cutout_loop_table.setItem(row, 1, area_item)
            self.cutout_loop_table.setCellWidget(row, 2, combo)
        self.cutout_loop_table.blockSignals(False)
        if self.cutout_loop_table.rowCount() > 0:
            self.cutout_loop_table.selectRow(0)

    def _on_cutout_comp_changed(self, row: int, value: str) -> None:
        if 0 <= row < len(self._cutout_plan_compensations):
            self._cutout_plan_compensations[row] = value
            self._on_cutout_setting_changed()

    def _on_cutout_setting_changed(self) -> None:
        if not self._cutout_plan_active:
            return
        self._refresh_canvas(fit=False)

    def _refresh_canvas_no_fit(self) -> None:
        self._refresh_canvas(fit=False)

    def _update_cutout_source_label(self) -> None:
        if self._cutout_plan_source_layer_index is None:
            self.cutout_source_label.setText("Source: (none)")
            return
        layer = self.project.layers[self._cutout_plan_source_layer_index]
        self.cutout_source_label.setText(f"Source: {layer.name} ({len(self._cutout_plan_loops)} loops)")

    def _render_cutout_preview(self) -> None:
        self._clear_cutout_preview()
        if self._task_running:
            return
        if not self._cutout_plan_active or self._cutout_plan_source_layer_index is None:
            return
        idx = self._cutout_plan_source_layer_index
        if idx < 0 or idx >= len(self.project.layers):
            return
        layer = self.project.layers[idx]
        params = CutoutParams(
            tool_diameter_mm=float(self.cutout_tool_dia_spin.value()),
            compensation="outside",
            loop_compensations=list(self._cutout_plan_compensations),
        )
        try:
            preview = build_cutout_toolpath_layer(layer, self.project, params)
        except Exception:
            return
        preview.metadata["preview"] = "true"
        preview.color = "#FFD166"
        self._cutout_preview_layer = preview
        self._cutout_preview_item = self.canvas.add_layer(preview, layer_index=-1)
        self._highlight_selected_loop()

    def _highlight_selected_loop(self) -> None:
        if self._cutout_preview_item is None:
            return
        row = self.cutout_loop_table.currentRow()
        if row < 0 or row >= len(self._cutout_plan_loops):
            return
        # Keep highlight simple: ensure selected row is visible and show status hint.
        self.statusBar().showMessage(f"Selected cutout loop {row + 1}", 1000)

    def _clear_cutout_preview(self) -> None:
        if self._cutout_preview_item is not None:
            try:
                self.canvas.scene().removeItem(self._cutout_preview_item)
            except Exception:
                pass
        self._cutout_preview_item = None
        self._cutout_preview_layer = None

    def _run_process_task(self, *, title: str, task_name: str, payload, decode_result, on_success, failure_title: str) -> None:
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            return
        self._task_running = True
        self._set_busy_controls(True)
        self.canvas.start_task_overlay(title)
        self._task_failure_title = failure_title
        self._task_on_success = (decode_result, on_success)
        self._task_result_received = False
        self._task_queue = mp.Queue()
        self._task_process = mp.Process(
            target=run_task_process_entry,
            args=(task_name, payload, self._task_queue),
            daemon=True,
        )
        self._task_process.start()
        self._task_poll_timer.start()

    def _poll_task_queue(self) -> None:
        if not self._task_running or self._task_queue is None:
            return
        while True:
            try:
                msg = self._task_queue.get_nowait()
            except Empty:
                break
            if not isinstance(msg, dict):
                continue
            mtype = str(msg.get("type", ""))
            if mtype == "log":
                self.canvas.append_task_overlay_line(str(msg.get("text", "")))
                continue
            if mtype == "error":
                message = str(msg.get("message", "Task failed"))
                trace = str(msg.get("traceback", ""))
                LOGGER.error("Task failed: %s\n%s", message, trace)
                QMessageBox.warning(self, self._task_failure_title, message)
                if trace:
                    self.canvas.append_task_overlay_line(trace)
                self.canvas.finish_task_overlay(success=False, detail=message)
                self._finish_task_state()
                return
            if mtype == "result":
                self._task_result_received = True
                try:
                    decode_result, on_success = self._task_on_success
                    decoded = decode_result(dict(msg.get("result", {})))
                    on_success(decoded)
                except Exception as exc:
                    LOGGER.exception("Task result handling failed")
                    QMessageBox.warning(self, self._task_failure_title, str(exc))
                    self.canvas.finish_task_overlay(success=False, detail=str(exc))
                else:
                    self.canvas.finish_task_overlay(success=True)
                self._finish_task_state()
                return

        if self._task_process is not None and not self._task_process.is_alive() and not self._task_result_received:
            code = self._task_process.exitcode
            message = f"Background task exited unexpectedly (code {code})."
            QMessageBox.warning(self, self._task_failure_title, message)
            self.canvas.finish_task_overlay(success=False, detail=message)
            self._finish_task_state()

    def _finish_task_state(self) -> None:
        self._task_poll_timer.stop()
        if self._task_process is not None:
            try:
                if self._task_process.is_alive():
                    self._task_process.terminate()
                self._task_process.join(timeout=1.0)
            except Exception:
                pass
        if self._task_queue is not None:
            try:
                self._task_queue.close()
            except Exception:
                pass
        self._task_process = None
        self._task_queue = None
        self._task_on_success = None
        self._task_running = False
        self._set_busy_controls(False)

    def _set_busy_controls(self, busy: bool) -> None:
        enabled = not busy
        for action in (
            self.open_project_action,
            self.save_project_action,
            self.save_project_as_action,
            self.open_gerber_action,
            self.open_excellon_action,
            self.open_folder_action,
            self.clear_action,
            self.generate_isolation_action,
            self.generate_cutout_toolpath_action,
        ):
            action.setEnabled(enabled)
        self.layer_tree.setEnabled(enabled)
        self.cutout_apply_button.setEnabled(enabled)
        self.cutout_close_button.setEnabled(enabled)

    def _select_cutout_loop_at(self, x_mm: float, y_mm: float) -> bool:
        if not self._cutout_plan_loops:
            return False
        from shapely.geometry import Point

        p = Point(float(x_mm), float(y_mm))
        best_row = -1
        best_dist = float("inf")
        for row, loop in enumerate(self._cutout_plan_loops):
            d = float(loop.exterior.distance(p))
            if d < best_dist:
                best_dist = d
                best_row = row
        # Dynamic tolerance by zoom: ~10px in scene units.
        px_per_mm = max(abs(self.canvas.transform().m11()), 1e-9)
        tol_mm = 10.0 / px_per_mm
        if best_row >= 0 and best_dist <= tol_mm:
            self.cutout_loop_table.selectRow(best_row)
            self._refresh_canvas(fit=False)
            return True
        return False

    def _default_cutout_loop_compensations(self, loops) -> list[str]:
        # External contours default to outside; nested contours (holes) default to inside.
        defaults: list[str] = []
        for idx, loop in enumerate(loops):
            rp = loop.representative_point()
            depth = 0
            for j, other in enumerate(loops):
                if j == idx:
                    continue
                if other.area <= loop.area:
                    continue
                if other.contains(rp):
                    depth += 1
            defaults.append("inside" if depth % 2 == 1 else "outside")
        return defaults

    def closeEvent(self, event) -> None:  # noqa: N802, ANN001
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish before closing.")
            event.ignore()
            return
        self._finish_task_state()
        super().closeEvent(event)
