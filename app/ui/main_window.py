from __future__ import annotations

from dataclasses import replace
import json
import logging
import math
import multiprocessing as mp
from pathlib import Path
from queue import Empty
from types import SimpleNamespace

from PySide6.QtCore import QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDockWidget,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QDoubleSpinBox,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.io import ROLE_DEFAULT_STYLE, import_files, scan_folder
from app.core.background_tasks import (
    cutout_params_payload,
    drill_params_payload,
    hatching_params_payload,
    isolation_params_payload,
    surfacing_params_payload,
    run_task_process_entry,
)
from app.core.drilling import DrillToolpathParams
from app.core.cutout import CutoutParams, extract_cutout_loops
from app.core.cnc_params import calculate_coppercam_params
from app.core.centering_holes import (
    CenteringHolesParams,
    build_centering_holes_toolpath_layer,
    centering_hole_mirror_axis,
)
from app.core.hatching import HatchingParams
from app.core.hpgl import HPGLExportOptions, export_layers_to_hpgl, is_toolpath_layer
from app.core.isolation import IsolationParams, build_isolation_layer
from app.core.surfacing import SurfacingParams
from app.core.project import Layer, Project, ToolDefinition, default_tool_library
from app.core.project_store import deserialize_layer, load_project, save_project, serialize_layer
from app.ui.centering_holes_wizard_dialog import CenteringHolesWizardDialog
from app.ui.icons import icon_for
from app.ui.layer_roles_dialog import LayerRoleEntry, LayerRolesDialog
from app.ui.pyqtgraph_canvas import PyQtGraphCanvas
from app.ui.selected_tools_dialog import SelectedToolsDialog
from app.ui.surfacing_dialog import SurfacingToolpathDialog
from app.ui.tool_library_dialog import ToolLibraryDialog
from app.ui.widgets import GraphicsCanvas


LOGGER = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Minimal Gerber/Excellon parser + viewer shell."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PathKernel - Viewer")
        self.resize(1200, 800)

        self.project = Project()
        self._global_tool_library = self._load_tool_library_from_settings()
        self._selected_tools_assignments = self._load_selected_tools_from_settings()
        self._apply_global_tool_library_to_project()
        self._project_file_path: Path | None = None
        self.scene_items: dict[int, object] = {}
        self._cutout_preview_layer = None
        self._cutout_preview_item = None
        self._cutout_preview_locked_until_reopen = False
        self._cutout_plan_active = False
        self._cutout_plan_source_layer_index: int | None = None
        self._cutout_plan_source_layer_indices: list[int] = []
        self._cutout_plan_loops = []
        self._cutout_plan_loop_source_positions: list[int] = []
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
        self._preview_task_process: mp.Process | None = None
        self._preview_task_queue = None
        self._preview_task_poll_timer = QTimer(self)
        self._preview_task_poll_timer.setInterval(80)
        self._preview_task_poll_timer.timeout.connect(self._poll_preview_task_queue)
        self._preview_debounce_timer = QTimer(self)
        self._preview_debounce_timer.setSingleShot(True)
        self._preview_debounce_timer.setInterval(140)
        self._preview_debounce_timer.timeout.connect(self._start_cutout_preview_task)
        self._preview_request_token = 0
        self._preview_inflight_token = 0
        self._preview_pending_payload: dict[str, object] | None = None
        self._renderer_mode = "pyqtgraph"
        self.canvas = self._create_canvas(self._renderer_mode)
        if self._renderer_mode == "pyqtgraph":
            renderer_name = getattr(self.canvas, "renderer_name", lambda: "unknown")()
            if renderer_name != "pyqtgraph":
                LOGGER.warning("PyQtGraph unavailable at startup; falling back to Qt renderer.")
                self.canvas.deleteLater()
                self._renderer_mode = "qt"
                self.canvas = self._create_canvas(self._renderer_mode)
        self.setCentralWidget(self.canvas)

        self.layer_tree = QTreeWidget(self)
        self.layer_tree.setColumnCount(2)
        self.layer_tree.setHeaderHidden(True)
        self.layer_tree.setRootIsDecorated(True)
        self.layer_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
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
        self.mirror_layer_button: QPushButton | None = None
        self.reassign_layers_button: QPushButton | None = None

        self.metadata = QTextEdit(self)
        self.metadata.setReadOnly(True)
        self.cutout_panel = self._build_cutout_panel()
        self.layers_activity_bar: QFrame | None = None
        self.layers_activity_layers_btn: QToolButton | None = None
        self.layers_activity_metadata_btn: QToolButton | None = None
        self.layers_activity_cutout_btn: QToolButton | None = None
        self.layers_dock: QDockWidget | None = None
        self.metadata_dock: QDockWidget | None = None

        self._apply_layers_panel_style()
        self._build_docks()
        self._sync_cutout_tool_controls()
        self._build_menus()
        self._apply_icons()
        self._build_statusbar()
        self._connect_signals()
        self._sync_toolpath_view_modes_to_canvas()
        self._sync_mirror_layer_controls()
        self._try_restore_last_project()
        self._update_window_title()

    def _build_docks(self) -> None:
        self.layers_dock = QDockWidget("Layers", self)
        self.layers_dock.setWidget(self._build_layers_dock_widget())
        self.layers_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.layers_dock)

        self.metadata_dock = QDockWidget("Metadata", self)
        self.metadata_dock.setWidget(self.metadata)
        self.metadata_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.addDockWidget(Qt.RightDockWidgetArea, self.metadata_dock)

        self.cutout_dock = QDockWidget("Cutout Planner", self)
        self.cutout_dock.setWidget(self.cutout_panel)
        self.cutout_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.addDockWidget(Qt.RightDockWidgetArea, self.cutout_dock)
        self.cutout_dock.hide()
        self.layers_dock.visibilityChanged.connect(self._sync_layers_activity_buttons)
        self.metadata_dock.visibilityChanged.connect(self._sync_layers_activity_buttons)
        self.cutout_dock.visibilityChanged.connect(self._on_cutout_dock_visibility_changed)
        self._apply_layers_panel_style()
        self._sync_layers_activity_buttons()

    def _build_layers_dock_widget(self) -> QWidget:
        panel = QWidget(self)
        root = QHBoxLayout(panel)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.layers_activity_bar = QFrame(panel)
        self.layers_activity_bar.setObjectName("layersActivityBar")
        self.layers_activity_bar.setFixedWidth(52)
        bar_layout = QVBoxLayout(self.layers_activity_bar)
        bar_layout.setContentsMargins(0, 6, 0, 6)
        bar_layout.setSpacing(4)

        self.layers_activity_layers_btn = self._create_layers_activity_button(
            "L", "Layers panel"
        )
        self.layers_activity_metadata_btn = self._create_layers_activity_button(
            "M", "Toggle Metadata panel"
        )
        self.layers_activity_cutout_btn = self._create_layers_activity_button(
            "C", "Toggle Cutout Planner panel"
        )
        self.layers_activity_layers_btn.clicked.connect(self._focus_layers_panel)
        self.layers_activity_metadata_btn.clicked.connect(self._toggle_metadata_panel)
        self.layers_activity_cutout_btn.clicked.connect(self._toggle_cutout_panel)

        bar_layout.addWidget(self.layers_activity_layers_btn)
        bar_layout.addWidget(self.layers_activity_metadata_btn)
        bar_layout.addWidget(self.layers_activity_cutout_btn)
        bar_layout.addStretch(1)

        root.addWidget(self.layers_activity_bar)

        content = QWidget(panel)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(4)

        action_row = QWidget(content)
        action_layout = QHBoxLayout(action_row)
        action_layout.setContentsMargins(4, 4, 4, 0)
        action_layout.setSpacing(4)

        self.mirror_layer_button = QPushButton("Mirror", action_row)
        self.mirror_layer_button.setToolTip(
            "Mirror loaded file layers around the board's vertical centreline"
        )
        self.mirror_layer_button.clicked.connect(self._toggle_loaded_layers_mirror_x)

        self.reassign_layers_button = QPushButton("Roles...", action_row)
        self.reassign_layers_button.setToolTip("Reassign layer types and roles")
        self.reassign_layers_button.clicked.connect(self._reassign_layer_roles)

        action_layout.addWidget(self.mirror_layer_button)
        action_layout.addWidget(self.reassign_layers_button)
        content_layout.addWidget(action_row)
        content_layout.addWidget(self.layer_tree, 1)

        root.addWidget(content, 1)
        return panel

    def _create_layers_activity_button(self, text: str, tooltip: str) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName("layersActivityButton")
        button.setText(text)
        button.setToolTip(tooltip)
        button.setCheckable(True)
        button.setAutoRaise(True)
        button.setFixedSize(44, 44)
        return button

    def _focus_layers_panel(self) -> None:
        if self.layers_dock is not None:
            self.layers_dock.show()
            self.layers_dock.raise_()
        self.layer_tree.setFocus()
        self._sync_layers_activity_buttons()

    def _toggle_metadata_panel(self) -> None:
        if self.metadata_dock is None:
            return
        self.metadata_dock.setVisible(not self.metadata_dock.isVisible())
        self._sync_layers_activity_buttons()

    def _toggle_cutout_panel(self) -> None:
        if self.cutout_dock is None:
            return
        # Keep the same launch path as Tools > Generate Cutout Toolpath.
        # If planner data is not initialized yet, run the full planner-open flow.
        if not self._cutout_plan_active:
            self._open_cutout_planner()
            self._sync_layers_activity_buttons()
            return
        becoming_visible = not self.cutout_dock.isVisible()
        self.cutout_dock.setVisible(becoming_visible)
        if becoming_visible:
            self._on_cutout_planner_reopened()
        self._sync_layers_activity_buttons()

    def _on_cutout_dock_visibility_changed(self, visible: bool) -> None:
        self._sync_layers_activity_buttons()
        if visible:
            return
        # Hide preview together with planner, but keep cached preview geometry
        # so it can be shown again when planner is reopened.
        self._cancel_cutout_preview_task(clear_pending=True, clear_cache=False)
        self._remove_cutout_preview_item()
        self._update_cutout_preview_state_hint()

    def _sync_layers_activity_buttons(self) -> None:
        if self.layers_activity_layers_btn is not None:
            self.layers_activity_layers_btn.setChecked(
                bool(self.layers_dock is not None and self.layers_dock.isVisible())
            )
        if self.layers_activity_metadata_btn is not None:
            self.layers_activity_metadata_btn.setChecked(
                bool(self.metadata_dock is not None and self.metadata_dock.isVisible())
            )
        if self.layers_activity_cutout_btn is not None:
            self.layers_activity_cutout_btn.setChecked(
                bool(self.cutout_dock is not None and self.cutout_dock.isVisible())
            )

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        self.open_project_action = QAction("Open Project...", self)
        self.save_project_action = QAction("Save Project", self)
        self.save_project_as_action = QAction("Save Project As...", self)
        self.export_hpgl_action = QAction("Export Toolpaths to HPGL...", self)
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
        file_menu.addAction(self.export_hpgl_action)
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
        self.drill_width_action = QAction("Drill: Effective Cut Width", self)
        self.drill_centerline_action = QAction("Drill: Centerline + Arrows", self)
        self.renderer_qt_action = QAction("Renderer: Qt", self)
        self.renderer_pyqtgraph_action = QAction("Renderer: PyQtGraph", self)
        self.debug_geometry_dump_action = QAction("Debug: Dump Clicked Geometry", self)
        self.isolation_width_action.setCheckable(True)
        self.isolation_centerline_action.setCheckable(True)
        self.isolation_width_action.setChecked(True)
        self.cutout_width_action.setCheckable(True)
        self.cutout_centerline_action.setCheckable(True)
        self.cutout_width_action.setChecked(True)
        self.drill_width_action.setCheckable(True)
        self.drill_centerline_action.setCheckable(True)
        self.drill_width_action.setChecked(True)
        self.renderer_qt_action.setCheckable(True)
        self.renderer_pyqtgraph_action.setCheckable(True)
        self.debug_geometry_dump_action.setCheckable(True)
        self.renderer_qt_action.setChecked(self._renderer_mode == "qt")
        self.renderer_pyqtgraph_action.setChecked(self._renderer_mode == "pyqtgraph")
        self.debug_geometry_dump_action.setChecked(False)
        self.isolation_view_group = QActionGroup(self)
        self.isolation_view_group.setExclusive(True)
        self.isolation_view_group.addAction(self.isolation_width_action)
        self.isolation_view_group.addAction(self.isolation_centerline_action)
        self.cutout_view_group = QActionGroup(self)
        self.cutout_view_group.setExclusive(True)
        self.cutout_view_group.addAction(self.cutout_width_action)
        self.cutout_view_group.addAction(self.cutout_centerline_action)
        self.drill_view_group = QActionGroup(self)
        self.drill_view_group.setExclusive(True)
        self.drill_view_group.addAction(self.drill_width_action)
        self.drill_view_group.addAction(self.drill_centerline_action)
        self.renderer_group = QActionGroup(self)
        self.renderer_group.setExclusive(True)
        self.renderer_group.addAction(self.renderer_qt_action)
        self.renderer_group.addAction(self.renderer_pyqtgraph_action)
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
        view_menu.addSeparator()
        view_menu.addAction(self.drill_width_action)
        view_menu.addAction(self.drill_centerline_action)
        view_menu.addSeparator()
        view_menu.addAction(self.renderer_qt_action)
        view_menu.addAction(self.renderer_pyqtgraph_action)
        view_menu.addSeparator()
        view_menu.addAction(self.debug_geometry_dump_action)

        tools_menu = self.menuBar().addMenu("Tools")
        self.generate_isolation_action = QAction("Generate Isolation Geometry", self)
        self.generate_cutout_toolpath_action = QAction("Generate Cutout Toolpath", self)
        self.generate_drill_toolpath_action = QAction("Generate Drill Toolpath", self)
        self.generate_centering_holes_action = QAction("Generate Centering Holes Toolpath", self)
        self.generate_hatching_toolpath_action = QAction("Generate Hatching Toolpath", self)
        self.generate_surfacing_toolpath_action = QAction("Generate Surfacing Toolpath", self)
        self.mirror_layer_action = QAction("Mirror Loaded File Layers", self)
        self.mirror_layer_action.setCheckable(True)
        self.mirror_bottom_centering_action = QAction("Mirror Bottom Layer Using Centering Holes", self)
        self.reassign_layer_roles_action = QAction("Reassign Layer Types/Roles...", self)
        tools_menu.addAction(self.mirror_layer_action)
        tools_menu.addAction(self.mirror_bottom_centering_action)
        tools_menu.addAction(self.reassign_layer_roles_action)
        tools_menu.addSeparator()
        tools_menu.addAction(self.generate_isolation_action)
        tools_menu.addAction(self.generate_hatching_toolpath_action)
        tools_menu.addAction(self.generate_cutout_toolpath_action)
        tools_menu.addAction(self.generate_drill_toolpath_action)
        tools_menu.addAction(self.generate_centering_holes_action)
        tools_menu.addAction(self.generate_surfacing_toolpath_action)

        parameters_menu = self.menuBar().addMenu("Parameters")
        self.tool_library_action = QAction("Tool library...", self)
        self.selected_tools_action = QAction("Selected tools...", self)
        parameters_menu.addAction(self.tool_library_action)
        parameters_menu.addAction(self.selected_tools_action)

    @staticmethod
    def _apply_action_icon(action: QAction, icon_id: str, *, size: int = 16, color: str | None = None) -> None:
        icon = icon_for(icon_id, size=size, color=color)
        if icon.isNull():
            return
        action.setIcon(icon)

    @staticmethod
    def _apply_button_icon(
        button: QPushButton | QToolButton,
        icon_id: str,
        *,
        size: int,
        clear_text: bool = True,
        color: str | None = None,
        stroke_scale: float = 1.0,
    ) -> None:
        icon = icon_for(icon_id, size=size, color=color, stroke_scale=stroke_scale)
        if icon.isNull():
            return
        button.setIcon(icon)
        button.setIconSize(QSize(size, size))
        if clear_text:
            button.setText("")

    def _apply_icons(self) -> None:
        self._apply_action_icon(self.open_project_action, "file_open_project")
        self._apply_action_icon(self.save_project_action, "file_save_project")
        self._apply_action_icon(self.save_project_as_action, "file_save_project_as")
        self._apply_action_icon(self.export_hpgl_action, "file_save_project_as")
        self._apply_action_icon(self.open_gerber_action, "file_open_gerber")
        self._apply_action_icon(self.open_excellon_action, "file_open_excellon")
        self._apply_action_icon(self.open_folder_action, "file_open_folder")
        self._apply_action_icon(self.clear_action, "file_clear")
        self._apply_action_icon(self.exit_action, "file_exit")

        self._apply_action_icon(self.fit_action, "view_fit")
        self._apply_action_icon(self.zoom_in_action, "view_zoom_in")
        self._apply_action_icon(self.zoom_out_action, "view_zoom_out")
        self._apply_action_icon(self.actual_size_action, "view_actual_size")
        self._apply_action_icon(self.grid_action, "view_grid")
        self._apply_action_icon(self.isolation_width_action, "view_iso_width")
        self._apply_action_icon(self.isolation_centerline_action, "view_iso_centerline")
        self._apply_action_icon(self.cutout_width_action, "view_cutout_width")
        self._apply_action_icon(self.cutout_centerline_action, "view_cutout_centerline")
        self._apply_action_icon(self.drill_width_action, "view_cutout_width")
        self._apply_action_icon(self.drill_centerline_action, "view_cutout_centerline")
        self._apply_action_icon(self.renderer_qt_action, "view_renderer_qt")
        self._apply_action_icon(self.renderer_pyqtgraph_action, "view_renderer_pyqtgraph")
        self._apply_action_icon(self.debug_geometry_dump_action, "view_debug_dump")

        self._apply_action_icon(self.generate_isolation_action, "tool_generate_isolation")
        self._apply_action_icon(self.generate_hatching_toolpath_action, "tool_generate_isolation")
        self._apply_action_icon(self.generate_cutout_toolpath_action, "tool_generate_cutout")
        self._apply_action_icon(self.generate_drill_toolpath_action, "tool_generate_drill")
        self._apply_action_icon(self.generate_centering_holes_action, "tool_generate_drill")
        self._apply_action_icon(self.generate_surfacing_toolpath_action, "tool_generate_cutout")
        self._apply_action_icon(self.tool_library_action, "param_tool_library")
        self._apply_action_icon(self.selected_tools_action, "param_selected_tools")

        if self.layers_activity_layers_btn is not None:
            self._apply_button_icon(
                self.layers_activity_layers_btn,
                "activity_layers",
                size=26,
                color="#c9d1d9",
                stroke_scale=0.72,
            )
        if self.layers_activity_metadata_btn is not None:
            self._apply_button_icon(
                self.layers_activity_metadata_btn,
                "activity_metadata",
                size=26,
                color="#c9d1d9",
                stroke_scale=0.72,
            )
        if self.layers_activity_cutout_btn is not None:
            self._apply_button_icon(
                self.layers_activity_cutout_btn,
                "activity_cutout_planner",
                size=26,
                color="#c9d1d9",
                stroke_scale=0.72,
            )

        if hasattr(self, "cutout_apply_button") and self.cutout_apply_button is not None:
            self._apply_button_icon(self.cutout_apply_button, "cutout_generate", size=16, clear_text=False)
        if hasattr(self, "cutout_close_button") and self.cutout_close_button is not None:
            self._apply_button_icon(self.cutout_close_button, "cutout_close", size=16, clear_text=False)

    def _build_statusbar(self) -> None:
        self.cursor_label = QLabel("X: 0.000 mm, Y: 0.000 mm", self)
        self.zoom_label = QLabel("Zoom: 100%", self)
        self.statusBar().addPermanentWidget(self.cursor_label)
        self.statusBar().addPermanentWidget(self.zoom_label)

    def _connect_signals(self) -> None:
        self.open_project_action.triggered.connect(self._open_project_file)
        self.save_project_action.triggered.connect(self._save_project_file)
        self.save_project_as_action.triggered.connect(self._save_project_file_as)
        self.export_hpgl_action.triggered.connect(self._export_toolpaths_hpgl)
        self.open_gerber_action.triggered.connect(self._open_gerbers)
        self.open_excellon_action.triggered.connect(self._open_excellon)
        self.open_folder_action.triggered.connect(self._open_folder)
        self.clear_action.triggered.connect(self._clear_project)
        self.exit_action.triggered.connect(self.close)
        self.fit_action.triggered.connect(self._on_fit_action)
        self.zoom_in_action.triggered.connect(self._on_zoom_in_action)
        self.zoom_out_action.triggered.connect(self._on_zoom_out_action)
        self.actual_size_action.triggered.connect(self._on_actual_size_action)
        self.grid_action.toggled.connect(self._on_grid_toggled)
        self.debug_geometry_dump_action.toggled.connect(self._on_debug_dump_toggled)
        self.isolation_width_action.triggered.connect(lambda: self._on_isolation_mode_action("width"))
        self.isolation_centerline_action.triggered.connect(lambda: self._on_isolation_mode_action("centerline"))
        self.cutout_width_action.triggered.connect(lambda: self._on_cutout_mode_action("width"))
        self.cutout_centerline_action.triggered.connect(lambda: self._on_cutout_mode_action("centerline"))
        self.drill_width_action.triggered.connect(lambda: self._on_drill_mode_action("width"))
        self.drill_centerline_action.triggered.connect(lambda: self._on_drill_mode_action("centerline"))
        self.renderer_qt_action.triggered.connect(lambda: self._switch_renderer("qt"))
        self.renderer_pyqtgraph_action.triggered.connect(lambda: self._switch_renderer("pyqtgraph"))
        self.mirror_layer_action.triggered.connect(self._toggle_loaded_layers_mirror_x)
        self.mirror_bottom_centering_action.triggered.connect(self._toggle_bottom_layer_mirror_from_centering_holes)
        self.reassign_layer_roles_action.triggered.connect(self._reassign_layer_roles)
        self.generate_isolation_action.triggered.connect(self._generate_isolation_geometry)
        self.generate_hatching_toolpath_action.triggered.connect(self._generate_hatching_toolpath)
        self.generate_cutout_toolpath_action.triggered.connect(self._open_cutout_planner)
        self.generate_drill_toolpath_action.triggered.connect(self._generate_drill_toolpath)
        self.generate_centering_holes_action.triggered.connect(self._generate_centering_holes_toolpath)
        self.generate_surfacing_toolpath_action.triggered.connect(self._generate_surfacing_toolpath)
        self.tool_library_action.triggered.connect(self._open_tool_library)
        self.selected_tools_action.triggered.connect(self._open_selected_tools)

        self.layer_tree.itemChanged.connect(self._on_layer_item_changed)
        self.layer_tree.currentItemChanged.connect(self._on_layer_selected)
        self.layer_tree.itemSelectionChanged.connect(self._sync_mirror_layer_controls)
        self.layer_tree.itemExpanded.connect(self._on_group_expand)
        self.layer_tree.itemCollapsed.connect(self._on_group_collapse)

        self._connect_canvas_signals()

    def _create_canvas(self, renderer_mode: str):
        mode = (renderer_mode or "").strip().lower()
        if mode == "pyqtgraph":
            return PyQtGraphCanvas(self)
        return GraphicsCanvas(self)

    def _connect_canvas_signals(self) -> None:
        self.canvas.cursor_moved.connect(self._on_cursor_moved)
        self.canvas.zoom_changed.connect(self._on_zoom_changed)
        self.canvas.scene_clicked.connect(self._on_scene_clicked)

    def _sync_toolpath_view_modes_to_canvas(self) -> None:
        iso_mode = "centerline" if self.isolation_centerline_action.isChecked() else "width"
        cutout_mode = "centerline" if self.cutout_centerline_action.isChecked() else "width"
        drill_mode = "centerline" if self.drill_centerline_action.isChecked() else "width"
        try:
            self.canvas.set_isolation_view_mode(iso_mode)
            self.canvas.set_cutout_view_mode(cutout_mode)
            self.canvas.set_drill_view_mode(drill_mode)
        except Exception:
            pass

    def _switch_renderer(self, renderer_mode: str) -> None:
        mode = (renderer_mode or "").strip().lower()
        if mode not in {"qt", "pyqtgraph"}:
            return
        if mode == self._renderer_mode:
            return
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            if self._renderer_mode == "qt":
                self.renderer_qt_action.setChecked(True)
            else:
                self.renderer_pyqtgraph_action.setChecked(True)
            return

        old_canvas = self.canvas
        new_canvas = self._create_canvas(mode)
        if mode == "pyqtgraph":
            renderer_name = getattr(new_canvas, "renderer_name", lambda: "unknown")()
            if renderer_name != "pyqtgraph":
                new_canvas.deleteLater()
                QMessageBox.warning(
                    self,
                    "Renderer Unavailable",
                    "PyQtGraph renderer is unavailable in this environment. Keeping Qt renderer.",
                )
                if self._renderer_mode == "qt":
                    self.renderer_qt_action.setChecked(True)
                else:
                    self.renderer_pyqtgraph_action.setChecked(True)
                return

        self._renderer_mode = mode
        self.canvas = new_canvas
        self.setCentralWidget(self.canvas)
        self._connect_canvas_signals()
        self._sync_toolpath_view_modes_to_canvas()
        self._refresh_canvas(fit=True)
        if mode == "pyqtgraph":
            QMessageBox.information(
                self,
                "Renderer",
                "PyQtGraph renderer enabled (read-only parity in progress).",
            )
        if old_canvas is not None:
            old_canvas.deleteLater()

    def _on_fit_action(self) -> None:
        self.canvas.fit_scene()

    def _on_zoom_in_action(self) -> None:
        self.canvas.zoom_in()

    def _on_zoom_out_action(self) -> None:
        self.canvas.zoom_out()

    def _on_actual_size_action(self) -> None:
        self.canvas.reset_view()

    def _on_grid_toggled(self, visible: bool) -> None:
        self.canvas.set_grid_visible(visible)

    def _on_isolation_mode_action(self, mode: str) -> None:
        self.canvas.set_isolation_view_mode(mode)

    def _on_cutout_mode_action(self, mode: str) -> None:
        self.canvas.set_cutout_view_mode(mode)

    def _on_drill_mode_action(self, mode: str) -> None:
        self.canvas.set_drill_view_mode(mode)

    def _on_debug_dump_toggled(self, enabled: bool) -> None:
        if enabled:
            self.statusBar().showMessage("Geometry debug dump enabled: click geometry to log.", 3500)
        else:
            self.statusBar().showMessage("Geometry debug dump disabled.", 2000)

    def _loaded_layer_indices(self) -> list[int]:
        return [
            idx
            for idx, layer in enumerate(self.project.layers)
            if not self._is_generated_layer(layer)
        ]

    def _toggle_loaded_layers_mirror_x(self) -> None:
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            self._sync_mirror_layer_controls()
            return
        indices = self._loaded_layer_indices()
        if not indices:
            QMessageBox.information(self, "Mirror Layer", "Import one or more file layers first.")
            self._sync_mirror_layer_controls()
            return
        bounds = self._combined_layer_bbox(indices, transformed=True)
        if bounds is None:
            QMessageBox.information(self, "Mirror Layer", "Could not determine loaded file bounds.")
            self._sync_mirror_layer_controls()
            return
        axis_x = (bounds[0] + bounds[2]) * 0.5

        # Horizontal mirroring is the common PCB flip used when milling a
        # single-sided through-hole board from the underside. Adjust the
        # offset so mirroring happens around the loaded board centreline.
        target = any(not bool(self.project.layers[idx].mirror_x) for idx in indices)
        for idx in indices:
            layer = self.project.layers[idx]
            layer.offset_x_mm = (2.0 * axis_x) - float(layer.offset_x_mm)
            self.project.set_layer_mirror(idx, target, bool(layer.mirror_y))

        if hasattr(self.canvas, "apply_layer_transform_updates"):
            self.canvas.apply_layer_transform_updates(indices)
        else:
            self._refresh_canvas(fit=False)
        self._fit_canvas_to_viewport()
        self._clear_cutout_preview()
        self._sync_mirror_layer_controls()
        self._on_layer_selected(self.layer_tree.currentItem(), None)

        names = ", ".join(self.project.layers[idx].name for idx in indices[:3])
        if len(indices) > 3:
            names += f", +{len(indices) - 3} more"
        state = "Mirrored" if target else "Unmirrored"
        self.statusBar().showMessage(
            f"{state} loaded file layers around X={axis_x:.3f} mm: {names}",
            4500,
        )

    def _toggle_bottom_layer_mirror_from_centering_holes(self) -> None:
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            self._sync_mirror_layer_controls()
            return

        axis = self._centering_hole_mirror_axis()
        if axis is None:
            QMessageBox.information(
                self,
                "Mirror Bottom Layer",
                "Generate a centering holes toolpath first.",
            )
            self._sync_mirror_layer_controls()
            return

        indices = self._bottom_layer_indices()
        if not indices:
            QMessageBox.information(
                self,
                "Mirror Bottom Layer",
                "Assign the imported bottom copper layer role first.",
            )
            self._sync_mirror_layer_controls()
            return

        mirror_attr, hole_axis_value = axis
        board_bounds = self._bottom_mirror_board_bbox()
        if board_bounds is None:
            QMessageBox.information(
                self,
                "Mirror Bottom Layer",
                "Could not determine the board bounds.",
            )
            self._sync_mirror_layer_controls()
            return

        if mirror_attr == "mirror_x":
            axis_value = (board_bounds[0] + board_bounds[2]) * 0.5
        else:
            axis_value = (board_bounds[1] + board_bounds[3]) * 0.5
        target = any(not bool(getattr(self.project.layers[idx], mirror_attr, False)) for idx in indices)
        for idx in indices:
            layer = self.project.layers[idx]
            if mirror_attr == "mirror_x":
                layer.offset_x_mm = (2.0 * axis_value) - float(layer.offset_x_mm)
                self.project.set_layer_mirror(idx, target, bool(layer.mirror_y))
            else:
                layer.offset_y_mm = (2.0 * axis_value) - float(layer.offset_y_mm)
                self.project.set_layer_mirror(idx, bool(layer.mirror_x), target)

        if hasattr(self.canvas, "apply_layer_transform_updates"):
            self.canvas.apply_layer_transform_updates(indices)
        else:
            self._refresh_canvas(fit=False)
        self._fit_canvas_to_viewport()
        self._clear_cutout_preview()
        self._sync_mirror_layer_controls()
        self._on_layer_selected(self.layer_tree.currentItem(), None)

        axis_label = "X" if mirror_attr == "mirror_x" else "Y"
        names = ", ".join(self.project.layers[idx].name for idx in indices[:3])
        if len(indices) > 3:
            names += f", +{len(indices) - 3} more"
        state = "Mirrored" if target else "Unmirrored"
        self.statusBar().showMessage(
            (
                f"{state} bottom layer inside board {axis_label}={axis_value:.3f} mm "
                f"(centering holes at {axis_label}={hole_axis_value:.3f} mm): {names}"
            ),
            5000,
        )

    def _bottom_layer_indices(self) -> list[int]:
        return [
            idx
            for idx, layer in enumerate(self.project.layers)
            if not self._is_generated_layer(layer)
            and str(getattr(layer, "role", "unassigned")).strip().lower() == "bottom"
        ]

    def _centering_hole_mirror_axis(self) -> tuple[str, float] | None:
        for layer in reversed(self.project.layers):
            axis = centering_hole_mirror_axis(layer)
            if axis is not None:
                return axis
        return None

    def _bottom_mirror_board_bbox(self) -> tuple[float, float, float, float] | None:
        reference_indices = [
            idx
            for idx, layer in enumerate(self.project.layers)
            if not self._is_generated_layer(layer)
            and str(getattr(layer, "role", "unassigned")).strip().lower() in {"top", "cutout", "artwork"}
        ]
        if reference_indices:
            bounds = self._combined_layer_bbox(reference_indices, transformed=True)
            if bounds is not None:
                return bounds

        non_bottom_indices = [
            idx
            for idx, layer in enumerate(self.project.layers)
            if not self._is_generated_layer(layer)
            and str(getattr(layer, "role", "unassigned")).strip().lower() != "bottom"
        ]
        if non_bottom_indices:
            bounds = self._combined_layer_bbox(non_bottom_indices, transformed=True)
            if bounds is not None:
                return bounds

        return self._combined_layer_bbox(self._bottom_layer_indices(), transformed=True)

    def _sync_mirror_layer_controls(self) -> None:
        indices = self._loaded_layer_indices()
        enabled = bool(indices) and not self._task_running
        checked = bool(indices) and all(bool(self.project.layers[idx].mirror_x) for idx in indices)
        text = "Unmirror" if checked else "Mirror"
        if hasattr(self, "mirror_layer_action"):
            self.mirror_layer_action.blockSignals(True)
            self.mirror_layer_action.setEnabled(enabled)
            self.mirror_layer_action.setChecked(checked)
            self.mirror_layer_action.setText(
                "Unmirror Loaded File Layers" if checked else "Mirror Loaded File Layers"
            )
            self.mirror_layer_action.blockSignals(False)
        if self.mirror_layer_button is not None:
            self.mirror_layer_button.setEnabled(enabled)
            self.mirror_layer_button.setText(text)
        if hasattr(self, "mirror_bottom_centering_action"):
            self.mirror_bottom_centering_action.setEnabled(
                bool(self._bottom_layer_indices())
                and self._centering_hole_mirror_axis() is not None
                and not self._task_running
            )
        if hasattr(self, "reassign_layer_roles_action"):
            self.reassign_layer_roles_action.setEnabled(enabled)
        if self.reassign_layers_button is not None:
            self.reassign_layers_button.setEnabled(enabled)

    def _open_tool_library(self) -> None:
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            return
        dlg = ToolLibraryDialog(self._global_tool_library, self)
        if dlg.exec() != QDialog.Accepted:
            return
        self._global_tool_library = dlg.tool_library()
        self._save_tool_library_to_settings()
        self._apply_global_tool_library_to_project()
        self._sync_cutout_tool_controls()
        self.statusBar().showMessage("Tool library updated.", 3000)

    def _open_selected_tools(self) -> None:
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            return
        dlg = SelectedToolsDialog(self._global_tool_library, self._selected_tools_assignments, self)
        if dlg.exec() != QDialog.Accepted:
            return
        self._selected_tools_assignments = dlg.selected_assignments()
        self._save_selected_tools_to_settings()
        self._sync_cutout_tool_controls()
        self.statusBar().showMessage("Selected tools updated.", 3000)

    def _open_gerbers(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Open Gerber Files",
            "",
            "Gerber Files (*.gtl *.gbl *.gto *.gbo *.gts *.gbs *.gko *.gm1 *.gb0 *.gb1 *.gb2 *.gb3 *.gb4 *.gb5 *.gb6 *.gb7 *.gb8 *.gb9 *.gbr *.pho *.art);;All Files (*)",
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
            imported_layers = import_files(paths, self.project)
        except Exception as exc:
            LOGGER.exception("Import failed")
            QMessageBox.warning(self, "Import Failed", str(exc))
            return
        self._assign_roles_for_imported_layers(imported_layers)
        self._rebuild_scene()
        self._project_file_path = None
        self._update_window_title()
        self.statusBar().showMessage(f"Imported {len(paths)} file(s)", 3000)

    def _assign_roles_for_imported_layers(self, layers: list[Layer]) -> None:
        if not layers:
            return
        first_index = len(self.project.layers) - len(layers)
        entries: list[LayerRoleEntry] = []
        for offset, layer in enumerate(layers):
            layer_index = first_index + offset
            if layer_index < 0 or layer_index >= len(self.project.layers):
                continue
            entries.append(
                LayerRoleEntry(
                    layer_index=layer_index,
                    name=str(layer.name),
                    kind=str(layer.kind),
                    path=str(layer.path),
                    role=str(layer.role),
                )
            )
        if not entries:
            return
        dlg = LayerRolesDialog(entries, self, title="Assign Roles For Imported Files")
        if dlg.exec() != QDialog.Accepted:
            return
        selected_roles = dlg.selected_roles()
        selected_kinds = dlg.selected_kinds()
        for layer_index in sorted(set(selected_roles) | set(selected_kinds)):
            if layer_index < 0 or layer_index >= len(self.project.layers):
                continue
            self._apply_layer_kind(layer_index, selected_kinds.get(layer_index, ""))
            self._apply_layer_role(layer_index, selected_roles.get(layer_index, ""))

    def _reassign_layer_roles(self) -> None:
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            return
        indices = self._loaded_layer_indices()
        if not indices:
            QMessageBox.information(self, "Layer Roles", "Import one or more file layers first.")
            return
        entries = [
            LayerRoleEntry(
                layer_index=idx,
                name=str(self.project.layers[idx].name),
                kind=str(self.project.layers[idx].kind),
                path=str(self.project.layers[idx].path),
                role=str(self.project.layers[idx].role),
            )
            for idx in indices
        ]
        dlg = LayerRolesDialog(entries, self, title="Reassign Layer Types/Roles")
        if dlg.exec() != QDialog.Accepted:
            return
        selected_roles = dlg.selected_roles()
        selected_kinds = dlg.selected_kinds()
        changed_indices: list[int] = []
        for layer_index in sorted(set(selected_roles) | set(selected_kinds)):
            if layer_index < 0 or layer_index >= len(self.project.layers):
                continue
            before = (
                str(self.project.layers[layer_index].kind),
                str(self.project.layers[layer_index].role),
            )
            self._apply_layer_kind(layer_index, selected_kinds.get(layer_index, ""))
            self._apply_layer_role(layer_index, selected_roles.get(layer_index, ""))
            after = (
                str(self.project.layers[layer_index].kind),
                str(self.project.layers[layer_index].role),
            )
            if after != before:
                changed_indices.append(layer_index)
        self._rebuild_scene()
        if changed_indices:
            self.statusBar().showMessage(f"Updated {len(changed_indices)} layer assignment(s)", 3000)
        else:
            self.statusBar().showMessage("Layer assignments unchanged", 2000)

    def _apply_layer_kind(self, layer_index: int, kind: str) -> None:
        normalized = str(kind or "").strip().lower()
        if normalized not in {"gerber", "excellon", "geometry"}:
            return
        layer = self.project.layers[layer_index]
        layer.kind = normalized
        layer.metadata["kind"] = normalized

    def _apply_layer_role(self, layer_index: int, role: str) -> None:
        if not role:
            return
        self.project.set_layer_role(layer_index, role)
        layer = self.project.layers[layer_index]
        style = ROLE_DEFAULT_STYLE.get(str(layer.role))
        if style is not None:
            layer.color, layer.opacity = style

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

    def _export_toolpaths_hpgl(self) -> None:
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            return

        candidates = [(idx, layer) for idx, layer in enumerate(self.project.layers) if is_toolpath_layer(layer)]
        if not candidates:
            QMessageBox.information(
                self,
                "Export HPGL",
                "No generated toolpath layers found.\nGenerate isolation/cutout/drill/centering/surfacing toolpaths first.",
            )
            return

        start_dir = self._project_file_path.parent if self._project_file_path else Path.cwd()
        out_dir_text = QFileDialog.getExistingDirectory(self, "Export HPGL Folder", str(start_dir))
        if not out_dir_text:
            return
        out_dir = Path(out_dir_text)
        out_dir.mkdir(parents=True, exist_ok=True)

        def _safe_stem(value: str) -> str:
            raw = str(value).strip()
            if not raw:
                return "toolpath"
            cleaned = "".join(ch if (ch.isalnum() or ch in {"-", "_"}) else "_" for ch in raw)
            while "__" in cleaned:
                cleaned = cleaned.replace("__", "_")
            cleaned = cleaned.strip("_")
            return cleaned or "toolpath"

        exported = 0
        failed: list[str] = []
        used_names: dict[str, int] = {}

        for _, layer in candidates:
            stem_base = _safe_stem(layer.name)
            count = used_names.get(stem_base, 0)
            used_names[stem_base] = count + 1
            stem = stem_base if count == 0 else f"{stem_base}_{count+1}"
            out_path = out_dir / f"{stem}.plt"
            try:
                # Export true centerline path primitives from generated toolpath layers.
                # Do not normalize to origin; keep coordinates as calculated in workspace.
                result = export_layers_to_hpgl(
                    [layer],
                    options=HPGLExportOptions(
                        pen_number=1,
                        units_per_mm=40.0,
                        normalize_to_origin=False,
                    ),
                )
                out_path.write_text(result.hpgl_text, encoding="utf-8", newline="\n")
                exported += 1
            except Exception as exc:
                LOGGER.exception("HPGL export failed for layer '%s'", layer.name)
                failed.append(f"{layer.name}: {exc}")

        if failed and exported == 0:
            QMessageBox.warning(self, "Export HPGL Failed", "\n".join(failed[:10]))
            return
        if failed:
            QMessageBox.warning(
                self,
                "Export HPGL (partial)",
                "Some layers failed to export:\n" + "\n".join(failed[:10]),
            )

        self.statusBar().showMessage(
            f"HPGL exported: {exported} file(s) to {out_dir}",
            7000,
        )

    def _load_project_file(self, path: Path) -> None:
        try:
            loaded = load_project(path)
        except Exception as exc:
            LOGGER.exception("Open project failed")
            QMessageBox.warning(self, "Open Project Failed", str(exc))
            return
        self.project = loaded
        self._apply_global_tool_library_to_project()
        self._project_file_path = path
        self._set_last_project_path(path)
        self._reset_cutout_planner(refresh_canvas=False)
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

    def _apply_global_tool_library_to_project(self) -> None:
        self.project.tool_library = [replace(tool) for tool in self._global_tool_library]

    def _load_tool_library_from_settings(self) -> list[ToolDefinition]:
        settings = QSettings()
        raw = settings.value("tools/library_json", "")
        if not raw:
            return default_tool_library()
        try:
            decoded = json.loads(str(raw))
        except Exception:
            LOGGER.warning("Tool library settings JSON is invalid; using defaults.")
            return default_tool_library()
        return self._decode_tool_library_payload(decoded)

    def _save_tool_library_to_settings(self) -> None:
        payload = []
        for tool in self._global_tool_library:
            payload.append(
                {
                    "slot": int(tool.slot),
                    "defined": bool(tool.defined),
                    "tool_number": str(tool.tool_number),
                    "name": str(tool.name),
                    "diameter_mm": float(tool.diameter_mm),
                    "profile": str(tool.profile),
                    "angle_deg": float(tool.angle_deg),
                    "tip_diameter_mm": float(tool.tip_diameter_mm),
                    "max_rpm": int(tool.max_rpm),
                    "plunge_mm_s": float(tool.plunge_mm_s),
                    "max_depth_per_pass_mm": float(tool.max_depth_per_pass_mm),
                }
            )
        settings = QSettings()
        settings.setValue("tools/library_json", json.dumps(payload))
        settings.sync()

    def _load_selected_tools_from_settings(self) -> dict[str, object]:
        settings = QSettings()
        raw = settings.value("tools/selected_tools_json", "")
        defaults: dict[str, object] = {
            "engraving": None,
            "hatching": None,
            "cutting": None,
            "centering": None,
            "engraving_depth_mm": 0.0,
            "engraving_margin_mm": 0.0,
            "engraving_speed_mm_s": 0.0,
            "hatching_depth_mm": 0.0,
            "hatching_margin_mm": 0.0,
            "hatching_speed_mm_s": 0.0,
            "cutting_depth_mm": 0.0,
            "cutting_speed_mm_s": 0.0,
            "centering_hole_diameter_mm": 0.0,
            "centering_extra_depth_mm": 0.0,
            "drill_use_single_tool_boring": False,
            "drill_allow_oversize_tool_for_small_holes": False,
            "drill_single_tool_slot": None,
            "drill_use_closest_smaller_boring": False,
            "drill_use_closest_greater_no_boring": False,
            "drill_series_tool_slots": [],
            "drill_boring_cycle_mode": "drill_at_center",
            "drill_lateral_stepover_pct": 50.0,
            "drill_depth_mm": 0.0,
            "drill_boring_speed_mm_s": 0.0,
            "drilling": [],
        }
        if not raw:
            return defaults
        try:
            data = json.loads(str(raw))
        except Exception:
            LOGGER.warning("Selected tools settings JSON is invalid; using defaults.")
            return defaults
        if not isinstance(data, dict):
            return defaults
        out = dict(defaults)
        for key in ("engraving", "hatching", "cutting", "centering"):
            val = data.get(key)
            out[key] = int(val) if isinstance(val, int) else None
        out["engraving_depth_mm"] = self._safe_float(data.get("engraving_depth_mm"), 0.0)
        out["engraving_margin_mm"] = self._safe_float(data.get("engraving_margin_mm"), 0.0)
        if "engraving_speed_mm_s" in data:
            out["engraving_speed_mm_s"] = self._safe_float(data.get("engraving_speed_mm_s"), 0.0)
        else:
            out["engraving_speed_mm_s"] = self._safe_float(data.get("engraving_speed_mm_min"), 0.0) / 60.0
        out["hatching_depth_mm"] = self._safe_float(data.get("hatching_depth_mm"), 0.0)
        out["hatching_margin_mm"] = self._safe_float(data.get("hatching_margin_mm"), 0.0)
        if "hatching_speed_mm_s" in data:
            out["hatching_speed_mm_s"] = self._safe_float(data.get("hatching_speed_mm_s"), 0.0)
        else:
            out["hatching_speed_mm_s"] = self._safe_float(data.get("hatching_speed_mm_min"), 0.0) / 60.0
        out["cutting_depth_mm"] = self._safe_float(data.get("cutting_depth_mm"), 0.0)
        if "cutting_speed_mm_s" in data:
            out["cutting_speed_mm_s"] = self._safe_float(data.get("cutting_speed_mm_s"), 0.0)
        else:
            out["cutting_speed_mm_s"] = self._safe_float(data.get("cutting_speed_mm_min"), 0.0) / 60.0
        out["centering_hole_diameter_mm"] = self._safe_float(data.get("centering_hole_diameter_mm"), 0.0)
        out["centering_extra_depth_mm"] = self._safe_float(data.get("centering_extra_depth_mm"), 0.0)
        out["drill_use_single_tool_boring"] = bool(data.get("drill_use_single_tool_boring", False))
        out["drill_allow_oversize_tool_for_small_holes"] = bool(
            data.get("drill_allow_oversize_tool_for_small_holes", False)
        )
        single_slot = data.get("drill_single_tool_slot")
        out["drill_single_tool_slot"] = int(single_slot) if isinstance(single_slot, int) else None
        out["drill_use_closest_smaller_boring"] = bool(data.get("drill_use_closest_smaller_boring", False))
        out["drill_use_closest_greater_no_boring"] = bool(data.get("drill_use_closest_greater_no_boring", False))
        series = data.get("drill_series_tool_slots")
        if isinstance(series, list):
            out["drill_series_tool_slots"] = [int(v) for v in series if isinstance(v, int)]
        else:
            out["drill_series_tool_slots"] = []
        cycle_mode = str(data.get("drill_boring_cycle_mode", "drill_at_center")).strip().lower()
        if cycle_mode not in {"drill_at_center", "lift_up_at_center"}:
            cycle_mode = "drill_at_center"
        out["drill_boring_cycle_mode"] = cycle_mode
        out["drill_lateral_stepover_pct"] = self._safe_float(data.get("drill_lateral_stepover_pct"), 50.0)
        out["drill_depth_mm"] = self._safe_float(data.get("drill_depth_mm"), 0.0)
        if "drill_boring_speed_mm_s" in data:
            out["drill_boring_speed_mm_s"] = self._safe_float(data.get("drill_boring_speed_mm_s"), 0.0)
        else:
            out["drill_boring_speed_mm_s"] = self._safe_float(data.get("drill_boring_speed_mm_min"), 0.0) / 60.0
        drilling = data.get("drilling")
        if isinstance(drilling, list):
            out["drilling"] = [int(v) for v in drilling if isinstance(v, int)]
            if not out["drill_series_tool_slots"]:
                out["drill_series_tool_slots"] = list(out["drilling"])
        return out

    def _save_selected_tools_to_settings(self) -> None:
        payload = {
            "engraving": self._selected_tools_assignments.get("engraving"),
            "hatching": self._selected_tools_assignments.get("hatching"),
            "cutting": self._selected_tools_assignments.get("cutting"),
            "centering": self._selected_tools_assignments.get("centering"),
            "engraving_depth_mm": float(self._selected_tools_assignments.get("engraving_depth_mm", 0.0)),
            "engraving_margin_mm": float(self._selected_tools_assignments.get("engraving_margin_mm", 0.0)),
            "engraving_speed_mm_s": float(self._selected_tools_assignments.get("engraving_speed_mm_s", 0.0)),
            "hatching_depth_mm": float(self._selected_tools_assignments.get("hatching_depth_mm", 0.0)),
            "hatching_margin_mm": float(self._selected_tools_assignments.get("hatching_margin_mm", 0.0)),
            "hatching_speed_mm_s": float(self._selected_tools_assignments.get("hatching_speed_mm_s", 0.0)),
            "cutting_depth_mm": float(self._selected_tools_assignments.get("cutting_depth_mm", 0.0)),
            "cutting_speed_mm_s": float(self._selected_tools_assignments.get("cutting_speed_mm_s", 0.0)),
            "centering_hole_diameter_mm": float(self._selected_tools_assignments.get("centering_hole_diameter_mm", 0.0)),
            "centering_extra_depth_mm": float(self._selected_tools_assignments.get("centering_extra_depth_mm", 0.0)),
            "drill_use_single_tool_boring": bool(self._selected_tools_assignments.get("drill_use_single_tool_boring", False)),
            "drill_allow_oversize_tool_for_small_holes": bool(
                self._selected_tools_assignments.get("drill_allow_oversize_tool_for_small_holes", False)
            ),
            "drill_single_tool_slot": self._selected_tools_assignments.get("drill_single_tool_slot"),
            "drill_use_closest_smaller_boring": bool(
                self._selected_tools_assignments.get("drill_use_closest_smaller_boring", False)
            ),
            "drill_use_closest_greater_no_boring": bool(
                self._selected_tools_assignments.get("drill_use_closest_greater_no_boring", False)
            ),
            "drill_series_tool_slots": list(self._selected_tools_assignments.get("drill_series_tool_slots", [])),
            "drill_boring_cycle_mode": str(self._selected_tools_assignments.get("drill_boring_cycle_mode", "drill_at_center")),
            "drill_lateral_stepover_pct": float(self._selected_tools_assignments.get("drill_lateral_stepover_pct", 50.0)),
            "drill_depth_mm": float(self._selected_tools_assignments.get("drill_depth_mm", 0.0)),
            "drill_boring_speed_mm_s": float(self._selected_tools_assignments.get("drill_boring_speed_mm_s", 0.0)),
            "drilling": list(self._selected_tools_assignments.get("drilling", [])),
        }
        settings = QSettings()
        settings.setValue("tools/selected_tools_json", json.dumps(payload))
        settings.sync()

    def _decode_tool_library_payload(self, payload) -> list[ToolDefinition]:  # noqa: ANN001
        defaults = default_tool_library()
        if not isinstance(payload, list):
            return defaults
        out: list[ToolDefinition] = []
        for i in range(50):
            slot = i + 1
            data = payload[i] if i < len(payload) and isinstance(payload[i], dict) else None
            if data is None:
                out.append(replace(defaults[i]))
                continue
            profile = str(data.get("profile", "cylindrical/flute")).strip().lower()
            if profile not in {"cylindrical/flute", "conical"}:
                profile = "cylindrical/flute"
            plunge_mm_s = 0.0
            if "plunge_mm_s" in data:
                plunge_mm_s = self._safe_float(data.get("plunge_mm_s"), 0.0)
            else:
                # Backward compatibility: previous settings used mm/min.
                plunge_mm_min = self._safe_float(data.get("plunge_mm_min"), 0.0)
                plunge_mm_s = plunge_mm_min / 60.0
            out.append(
                ToolDefinition(
                    slot=slot,
                    defined=bool(data.get("defined", False)),
                    tool_number=str(data.get("tool_number", str(slot))),
                    name=str(data.get("name", "")),
                    diameter_mm=self._safe_float(data.get("diameter_mm"), 0.0),
                    profile=profile,
                    angle_deg=self._safe_float(data.get("angle_deg"), 0.0),
                    tip_diameter_mm=self._safe_float(data.get("tip_diameter_mm"), 0.0),
                    max_rpm=self._safe_int(data.get("max_rpm"), 0),
                    plunge_mm_s=plunge_mm_s,
                    max_depth_per_pass_mm=self._safe_float(data.get("max_depth_per_pass_mm"), 0.0),
                )
            )
        return out

    @staticmethod
    def _safe_float(value, default: float = 0.0) -> float:  # noqa: ANN001
        try:
            return float(value)
        except Exception:
            return float(default)

    @staticmethod
    def _safe_int(value, default: int = 0) -> int:  # noqa: ANN001
        try:
            return int(value)
        except Exception:
            return int(default)

    def _update_window_title(self) -> None:
        suffix = self._project_file_path.name if self._project_file_path else "Untitled"
        self.setWindowTitle(f"PathKernel - Viewer [{suffix}]")

    def _clear_project(self) -> None:
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            return
        self._reset_cutout_planner(refresh_canvas=False)
        self.project.clear()
        self._apply_global_tool_library_to_project()
        self._project_file_path = None
        self.layer_tree.clear()
        self._layer_index_to_item.clear()
        self.metadata.clear()
        self.canvas.clear_scene()
        self.scene_items.clear()
        self._update_window_title()
        self._sync_mirror_layer_controls()

    def _rebuild_scene(self) -> None:
        self._refresh_canvas(fit=True)
        self._rebuild_layer_list()

    def _refresh_canvas(self, *, fit: bool) -> None:  # noqa: ARG002
        self.canvas.clear_scene()
        self.scene_items.clear()
        self._cutout_preview_item = None

        for idx, layer in enumerate(self.project.layers):
            if layer.visible:
                item = self.canvas.add_layer(layer, layer_index=idx)
                self.scene_items[idx] = item
        if self._cutout_plan_active:
            if self._cutout_preview_locked_until_reopen:
                self._clear_cutout_preview()
            elif self._has_generated_cutout_toolpath(source_layer_indices=self._active_cutout_source_indices()):
                # Keep preview locked out while generated cutout output exists.
                self._cutout_preview_locked_until_reopen = True
                self._clear_cutout_preview()
            elif self._cutout_preview_layer is not None and bool(self.cutout_dock and self.cutout_dock.isVisible()):
                self._cutout_preview_item = self.canvas.add_layer(self._cutout_preview_layer, layer_index=-1)
            elif self._cutout_plan_loops and bool(self.cutout_dock and self.cutout_dock.isVisible()):
                self._schedule_cutout_preview_update()

        # Always re-center and fit after scene refresh to keep geometry visible.
        self._fit_canvas_to_viewport()

    def _fit_canvas_to_viewport(self) -> None:
        """Fit immediately and once again after pending UI/layout updates."""
        try:
            self.canvas.fit_scene()
        except Exception:
            pass

        def _deferred_fit() -> None:
            try:
                self.canvas.fit_scene()
            except Exception:
                pass

        QTimer.singleShot(0, _deferred_fit)

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
        self._sync_mirror_layer_controls()

    def _on_layer_item_changed(self, item: QTreeWidgetItem, column: int) -> None:  # noqa: ARG002
        layer_index = self._layer_index_from_item(item)
        if layer_index is None or layer_index < 0 or layer_index >= len(self.project.layers):
            return
        visible = item.checkState(0) == Qt.Checked
        layer = self.project.layers[layer_index]
        layer.visible = visible
        changed = False
        # Fast path: update only the toggled layer instead of rebuilding the full canvas.
        existing = self.scene_items.get(layer_index)
        if visible:
            if existing is None:
                self.scene_items[layer_index] = self.canvas.add_layer(layer, layer_index=layer_index)
                changed = True
        else:
            if existing is not None:
                if hasattr(self.canvas, "remove_layer"):
                    self.canvas.remove_layer(existing)
                    if hasattr(self.canvas, "remove_layer_index"):
                        self.canvas.remove_layer_index(layer_index)
                else:
                    self._refresh_canvas(fit=False)
                self.scene_items.pop(layer_index, None)
                changed = True
        if changed:
            self._fit_canvas_to_viewport()

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
            f"offset_x_mm: {layer.offset_x_mm:.6f}",
            f"offset_y_mm: {layer.offset_y_mm:.6f}",
            f"rotation_deg: {layer.rotation_deg:.6f}",
            f"mirror_x: {layer.mirror_x}",
            f"mirror_y: {layer.mirror_y}",
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
        if self.debug_geometry_dump_action.isChecked():
            preferred = self._current_layer_index()
            dump = self._build_geometry_debug_dump(x_mm, y_mm, preferred_layer_index=preferred)
            self.metadata.setPlainText(dump)
            log_path = self._append_geometry_debug_dump(dump)
            if log_path is not None:
                self.statusBar().showMessage(f"Geometry debug dump saved: {log_path}", 5000)
            else:
                self.statusBar().showMessage("Geometry debug dump generated", 3000)
            return
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
        click_info = dict(info)
        self._augment_clicked_geometry_metadata(click_info)
        lines = [f"{k}: {v}" for k, v in sorted(click_info.items())]
        self.metadata.setPlainText("\n".join(lines))

    def _augment_clicked_geometry_metadata(self, info: dict[str, str]) -> None:
        shape_kind = str(info.get("shape_kind", "")).strip().lower()
        if shape_kind != "fill":
            return

        diameter = self._info_float(info, "diameter_mm")
        width = self._info_float(info, "width_mm")
        height = self._info_float(info, "height_mm")
        if width is None:
            width = self._info_float(info, "bounds_width_mm")
        if height is None:
            height = self._info_float(info, "bounds_height_mm")
        corner = self._info_float(info, "corner_radius_mm")
        hole = self._info_float(info, "hole_diameter_mm")

        size_label: str | None = None
        if diameter is not None and diameter > 0.0:
            size_label = f"{diameter:.4f} dia"
        elif width is not None and height is not None and width > 0.0 and height > 0.0:
            size_label = f"{width:.4f} x {height:.4f}"

        if size_label is not None:
            details: list[str] = [size_label]
            if corner is not None and corner > 0.0:
                details.append(f"R{corner:.4f}")
            if hole is not None and hole > 0.0:
                details.append(f"hole {hole:.4f} dia")
            info["pad_size_mm"] = ", ".join(details)

    @staticmethod
    def _info_float(info: dict[str, str], key: str) -> float | None:
        try:
            value = info.get(key)
            if value is None:
                return None
            out = float(str(value))
            if out != out:  # NaN guard
                return None
            return out
        except Exception:
            return None

    def _build_geometry_debug_dump(
        self,
        x_mm: float,
        y_mm: float,
        *,
        preferred_layer_index: int | None = None,
    ) -> str:
        if hasattr(self.canvas, "debug_dump_at"):
            try:
                return self.canvas.debug_dump_at(x_mm, y_mm, preferred_layer_index=preferred_layer_index)
            except Exception as exc:
                return (
                    "geometry-debug-error\n"
                    f"renderer={self._renderer_mode}\n"
                    f"click_mm=({float(x_mm):.6f},{float(y_mm):.6f})\n"
                    f"error={exc}"
                )
        return (
            "geometry-debug-unavailable\n"
            f"renderer={self._renderer_mode}\n"
            f"click_mm=({float(x_mm):.6f},{float(y_mm):.6f})"
        )

    def _append_geometry_debug_dump(self, dump_text: str) -> Path | None:
        try:
            out_dir = Path.cwd() / "debug"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / "geometry_dump.log"
            with out_path.open("a", encoding="utf-8") as f:
                f.write(dump_text.rstrip())
                f.write("\n" + ("-" * 78) + "\n")
            return out_path
        except Exception:
            LOGGER.exception("Failed to write geometry debug dump")
            return None

    def _generate_isolation_geometry(self) -> None:
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            return
        source_layers = self._selected_layers_for_operation()
        if not source_layers:
            QMessageBox.information(self, "Isolation", "Select a Gerber layer first.")
            return

        invalid_layers = [layer.name for layer in source_layers if layer.kind != "gerber"]
        if invalid_layers:
            QMessageBox.information(self, "Isolation", "Isolation currently supports Gerber layers only.")
            return
        source_layer = source_layers[0]
        source_name = self._source_layers_label(source_layers)

        engraving_tool = self._selected_engraving_tool()
        if engraving_tool is None:
            QMessageBox.information(
                self,
                "Isolation",
                "No engraving tool selected. Configure it in Parameters > Selected tools...",
            )
            return
        tool_dia = max(0.001, self._safe_float(getattr(engraving_tool, "diameter_mm", 0.2), 0.2))

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
        engraving_depth = max(0.0, self._safe_float(self._selected_tools_assignments.get("engraving_depth_mm"), 0.0))
        engraving_margin = max(0.0, self._safe_float(self._selected_tools_assignments.get("engraving_margin_mm"), 0.0))
        hatching_margin = max(0.0, self._safe_float(self._selected_tools_assignments.get("hatching_margin_mm"), 0.0))
        tool_profile = str(getattr(engraving_tool, "profile", "cylindrical/flute") or "cylindrical/flute")
        tool_tip_dia = max(0.0, self._safe_float(getattr(engraving_tool, "tip_diameter_mm", 0.0), 0.0))
        tool_angle = max(0.0, self._safe_float(getattr(engraving_tool, "angle_deg", 0.0), 0.0))
        params = IsolationParams(
            tool_diameter_mm=float(tool_dia),
            passes=int(passes),
            overlap=0.0,
            iso_type=iso_type_map.get(selected_label, 2),
            extra_pad_contours=int(extra_pad_contours),
            tool_profile=tool_profile,
            tool_tip_diameter_mm=tool_tip_dia,
            tool_angle_deg=tool_angle,
            cutting_depth_mm=engraving_depth,
            trace_margin_mm=engraving_margin,
            hatching_margin_mm=hatching_margin,
        )

        def on_done(layer):
            tool_label = str(getattr(engraving_tool, "tool_number", "") or "").strip()
            if not tool_label:
                tool_label = str(getattr(engraving_tool, "slot", ""))
            layer.metadata["selected_engraving_tool"] = tool_label
            layer.metadata["selected_engraving_speed_mm_s"] = (
                f"{self._safe_float(self._selected_tools_assignments.get('engraving_speed_mm_s'), 0.0):.6f}"
            )
            layer.metadata["selected_hatching_speed_mm_s"] = (
                f"{self._safe_float(self._selected_tools_assignments.get('hatching_speed_mm_s'), 0.0):.6f}"
            )
            self.project.add_layer(layer)
            self._rebuild_scene()
            self._select_layer_item(len(self.project.layers) - 1)
            self._fit_canvas_to_viewport()
            clearance_warn = str(layer.metadata.get("tool_clearance_warning", "")).strip()
            if clearance_warn:
                QMessageBox.warning(self, "Tool Size Warning", clearance_warn)
            self.statusBar().showMessage(
                f"Isolation generated from '{source_name}' using engraving tool {tool_label}",
                4500,
            )

        self._run_process_task(
            title=f"Isolation {source_name}",
            task_name="isolation_generate_multi" if len(source_layers) > 1 else "isolation_generate",
            payload={
                "source_layer": serialize_layer(source_layer),
                "source_layers": [serialize_layer(layer) for layer in source_layers],
                "params": isolation_params_payload(params),
            },
            decode_result=lambda data: deserialize_layer(dict(data["generated_layer"])),
            on_success=on_done,
            failure_title="Isolation Failed",
        )

    def _generate_hatching_toolpath(self) -> None:
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            return
        source_layers = self._selected_layers_for_operation()
        if not source_layers:
            QMessageBox.information(self, "Hatching Toolpath", "Select a top/bottom copper Gerber layer first.")
            return

        source_layer = source_layers[0]
        source_name = self._source_layers_label(source_layers)
        if any(layer.kind != "gerber" for layer in source_layers):
            QMessageBox.information(self, "Hatching Toolpath", "Hatching currently supports Gerber copper layers only.")
            return
        if any(
            str(getattr(layer, "role", "unassigned")).strip().lower()
            not in {"top", "bottom", "artwork", "unassigned"}
            for layer in source_layers
        ):
            QMessageBox.information(
                self,
                "Hatching Toolpath",
                "Select a copper/artwork Gerber layer, not a drill or cutout layer.",
            )
            return

        hatching_tool = self._selected_hatching_tool()
        if hatching_tool is None:
            QMessageBox.information(
                self,
                "Hatching Toolpath",
                "No hatching tool selected. Configure it in Parameters > Selected tools...",
            )
            return

        hatch_angle, ok = QInputDialog.getDouble(
            self,
            "Hatching Angle",
            "Hatch angle (degrees):",
            0.0,
            -360.0,
            360.0,
            2,
        )
        if not ok:
            return

        boundary_margin, ok = QInputDialog.getDouble(
            self,
            "Hatching Boundary Margin",
            "Extra clearing margin outside copper bounds (mm):",
            1.0,
            0.0,
            1000.0,
            3,
        )
        if not ok:
            return

        copper_keepout_margin, ok = QInputDialog.getDouble(
            self,
            "Copper Keepout",
            "Extra keepout around copper features (mm):",
            0.0,
            0.0,
            1000.0,
            3,
        )
        if not ok:
            return

        tool_dia = max(0.001, self._safe_float(getattr(hatching_tool, "diameter_mm", 0.2), 0.2))
        hatching_depth = max(0.0, self._safe_float(self._selected_tools_assignments.get("hatching_depth_mm"), 0.0))
        hatching_margin = max(0.0, self._safe_float(self._selected_tools_assignments.get("hatching_margin_mm"), 0.0))
        tool_profile = str(getattr(hatching_tool, "profile", "cylindrical/flute") or "cylindrical/flute")
        tool_tip_dia = max(0.0, self._safe_float(getattr(hatching_tool, "tip_diameter_mm", 0.0), 0.0))
        tool_angle = max(0.0, self._safe_float(getattr(hatching_tool, "angle_deg", 0.0), 0.0))
        params = HatchingParams(
            tool_diameter_mm=tool_dia,
            tool_profile=tool_profile,
            tool_tip_diameter_mm=tool_tip_dia,
            tool_angle_deg=tool_angle,
            cutting_depth_mm=hatching_depth,
            hatching_margin_mm=hatching_margin,
            hatch_angle_deg=float(hatch_angle),
            copper_keepout_margin_mm=float(copper_keepout_margin),
            boundary_margin_mm=float(boundary_margin),
        )

        source_layer_ids = {id(layer) for layer in source_layers}
        keepout_layers = [
            layer
            for layer in self.project.layers
            if id(layer) not in source_layer_ids
            and str(layer.metadata.get("kind", "")).strip().lower()
            in {"isolation", "isolation_toolpath"}
            and self._layer_is_derived_from_any(layer, source_layers)
        ]
        board_layers = [
            layer
            for layer in self.project.layers
            if id(layer) not in source_layer_ids
            and str(getattr(layer, "role", "unassigned")).strip().lower() == "cutout"
            and layer.kind in {"gerber", "geometry"}
        ]

        def on_done(layer):
            tool_label = str(getattr(hatching_tool, "tool_number", "") or "").strip()
            if not tool_label:
                tool_label = str(getattr(hatching_tool, "slot", ""))
            layer.metadata["selected_hatching_tool"] = tool_label
            layer.metadata["selected_hatching_depth_mm"] = f"{hatching_depth:.6f}"
            layer.metadata["selected_hatching_speed_mm_s"] = (
                f"{self._safe_float(self._selected_tools_assignments.get('hatching_speed_mm_s'), 0.0):.6f}"
            )
            self.project.add_layer(layer)
            self._rebuild_scene()
            self._select_layer_item(len(self.project.layers) - 1)
            self._fit_canvas_to_viewport()
            line_count = len(list(getattr(getattr(layer, "source", None), "primitives", []) or []))
            self.statusBar().showMessage(
                f"Hatching toolpath generated from '{source_name}' using hatching tool {tool_label} ({line_count} segment(s))",
                5000,
            )

        self._run_process_task(
            title=f"Hatching Toolpath {source_name}",
            task_name="hatching_generate_multi" if len(source_layers) > 1 else "hatching_generate",
            payload={
                "source_layer": serialize_layer(source_layer),
                "source_layers": [serialize_layer(layer) for layer in source_layers],
                "params": hatching_params_payload(params),
                "keepout_layers": [serialize_layer(layer) for layer in keepout_layers],
                "board_layers": [serialize_layer(layer) for layer in board_layers],
                "copper_keepout_layers": [serialize_layer(layer) for layer in source_layers],
            },
            decode_result=lambda data: deserialize_layer(dict(data["generated_layer"])),
            on_success=on_done,
            failure_title="Hatching Toolpath Failed",
        )

    def _generate_drill_toolpath(self) -> None:
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            return
        source_layers = self._selected_layers_for_operation()
        if not source_layers:
            QMessageBox.information(self, "Drill Toolpath", "Select an Excellon layer first.")
            return

        source_layer = source_layers[0]
        source_name = self._source_layers_label(source_layers)
        if any(layer.kind != "excellon" for layer in source_layers):
            QMessageBox.information(self, "Drill Toolpath", "Drill toolpath currently supports Excellon layers only.")
            return

        if not bool(self._selected_tools_assignments.get("drill_use_single_tool_boring", False)):
            QMessageBox.information(
                self,
                "Drill Toolpath",
                "Enable Strategy A in Parameters > Selected tools > Drilling tools.",
            )
            return

        drill_tool = self._selected_drill_single_tool()
        if drill_tool is None:
            QMessageBox.information(
                self,
                "Drill Toolpath",
                "Select one drill tool for Strategy A in Parameters > Selected tools.",
            )
            return

        tool_dia = max(0.001, self._safe_float(getattr(drill_tool, "diameter_mm", 0.0), 0.0))
        stepover_pct = max(1.0, min(100.0, self._safe_float(self._selected_tools_assignments.get("drill_lateral_stepover_pct"), 50.0)))
        cycle_mode = str(self._selected_tools_assignments.get("drill_boring_cycle_mode", "drill_at_center")).strip().lower()
        if cycle_mode not in {"drill_at_center", "lift_up_at_center"}:
            cycle_mode = "drill_at_center"
        params = DrillToolpathParams(
            tool_diameter_mm=tool_dia,
            lateral_stepover_pct=stepover_pct,
            boring_cycle_mode=cycle_mode,
            drilling_depth_mm=max(0.0, self._safe_float(self._selected_tools_assignments.get("drill_depth_mm"), 0.0)),
            boring_speed_mm_s=max(0.0, self._safe_float(self._selected_tools_assignments.get("drill_boring_speed_mm_s"), 0.0)),
            allow_oversize_tool_for_small_holes=bool(
                self._selected_tools_assignments.get("drill_allow_oversize_tool_for_small_holes", False)
            ),
        )

        def on_done(layer):
            tool_label = str(getattr(drill_tool, "tool_number", "") or "").strip()
            if not tool_label:
                tool_label = str(getattr(drill_tool, "slot", ""))
            layer.metadata["selected_drill_tool"] = tool_label
            layer.metadata["selected_drill_depth_mm"] = (
                f"{self._safe_float(self._selected_tools_assignments.get('drill_depth_mm'), 0.0):.6f}"
            )
            layer.metadata["selected_drill_boring_speed_mm_s"] = (
                f"{self._safe_float(self._selected_tools_assignments.get('drill_boring_speed_mm_s'), 0.0):.6f}"
            )
            self.project.add_layer(layer)
            self._rebuild_scene()
            self._select_layer_item(len(self.project.layers) - 1)
            self._fit_canvas_to_viewport()
            skipped = self._safe_int(layer.metadata.get("drill_skipped_small_holes"), 0)
            if skipped > 0:
                QMessageBox.warning(
                    self,
                    "Drill Toolpath Warning",
                    (
                        f"{skipped} hole(s) were skipped because they are smaller than the selected drill tool.\n"
                        "Choose a smaller drill tool in Strategy A for those holes."
                    ),
                )
            oversize = self._safe_int(layer.metadata.get("drill_oversize_small_holes"), 0)
            if oversize > 0:
                QMessageBox.information(
                    self,
                    "Drill Toolpath",
                    f"{oversize} hole(s) smaller than the selected drill tool will be drilled oversize.",
                )
            self.statusBar().showMessage(
                f"Drill toolpath generated from '{source_name}' using tool {tool_label}",
                4500,
            )

        self._run_process_task(
            title=f"Drill Toolpath {source_name}",
            task_name="drill_generate_multi" if len(source_layers) > 1 else "drill_generate",
            payload={
                "source_layer": serialize_layer(source_layer),
                "source_layers": [serialize_layer(layer) for layer in source_layers],
                "params": drill_params_payload(params),
            },
            decode_result=lambda data: deserialize_layer(dict(data["generated_layer"])),
            on_success=on_done,
            failure_title="Drill Toolpath Failed",
        )

    def _generate_centering_holes_toolpath(self) -> None:
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            return

        board_bounds = self._imported_board_bbox()
        if board_bounds is None:
            QMessageBox.information(
                self,
                "Centering Holes",
                "No imported board layers found. Import top/bottom/cutout/artwork first.",
            )
            return

        centering_tool = self._selected_centering_tool()
        if centering_tool is None:
            QMessageBox.information(
                self,
                "Centering Holes",
                "No centering tool selected. Configure it in Parameters > Selected tools...",
            )
            return

        tool_dia = max(0.001, self._safe_float(getattr(centering_tool, "diameter_mm", 0.0), 0.0))
        hole_dia = max(
            0.001,
            self._safe_float(
                self._selected_tools_assignments.get("centering_hole_diameter_mm"),
                tool_dia,
            ),
        )
        extra_depth = max(0.0, self._safe_float(self._selected_tools_assignments.get("centering_extra_depth_mm"), 0.0))

        dlg = CenteringHolesWizardDialog(
            self,
            hole_diameter_mm=hole_dia,
            initial_distance_mm=12.0,
            board_bounds_mm=board_bounds,
        )
        if dlg.exec() != QDialog.Accepted:
            return

        source_layer = self._first_reference_layer(default_bounds=board_bounds)
        params = CenteringHolesParams(
            board_min_x_mm=board_bounds[0],
            board_min_y_mm=board_bounds[1],
            board_max_x_mm=board_bounds[2],
            board_max_y_mm=board_bounds[3],
            orientation=dlg.selected_orientation(),
            outline_to_hole_center_mm=max(0.0, dlg.selected_distance_mm()),
            hole_diameter_mm=hole_dia,
            tool_diameter_mm=tool_dia,
            extra_depth_mm=extra_depth,
        )
        try:
            layer = build_centering_holes_toolpath_layer(source_layer, self.project, params)
        except Exception as exc:
            QMessageBox.warning(self, "Centering Holes Failed", str(exc))
            return

        tool_label = str(getattr(centering_tool, "tool_number", "") or "").strip()
        if not tool_label:
            tool_label = str(getattr(centering_tool, "slot", ""))
        layer.metadata["selected_centering_tool"] = tool_label
        # Reuse existing drill HPGL metadata keys for tool selection.
        layer.metadata["selected_drill_tool"] = tool_label
        layer.metadata["selected_drill_depth_mm"] = f"{extra_depth:.6f}"

        self.project.add_layer(layer)
        self._rebuild_scene()
        self._select_layer_item(len(self.project.layers) - 1)
        self._fit_canvas_to_viewport()
        self.statusBar().showMessage(
            (
                f"Centering holes toolpath generated (orientation: {dlg.selected_orientation()}, "
                f"distance: {dlg.selected_distance_mm():.3f} mm)"
            ),
            5000,
        )

    def _generate_surfacing_toolpath(self) -> None:
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            return

        index = self._current_layer_index()
        selected_layer = None
        if index is not None and 0 <= index < len(self.project.layers):
            selected_layer = self.project.layers[index]

        default_bounds = None
        if selected_layer is not None:
            default_bounds = self._layer_bbox_or_none(selected_layer, transformed=True)
        if default_bounds is None:
            default_bounds = self._imported_board_bbox()
        if default_bounds is None:
            default_bounds = (0.0, 0.0, 100.0, 100.0)

        source_layer = selected_layer if selected_layer is not None else self._first_reference_layer(default_bounds=default_bounds)
        source_name = source_layer.name if selected_layer is not None else "manual area"
        cutting_slot = self._selected_tools_assignments.get("cutting")
        default_tool_slot = int(cutting_slot) if isinstance(cutting_slot, int) else None

        dlg = SurfacingToolpathDialog(
            default_bounds=self._layer_bbox_for_dialog(source_layer, fallback=default_bounds),
            tools=list(self._global_tool_library),
            default_tool_slot=default_tool_slot,
            parent=self,
        )
        if dlg.exec() != QDialog.Accepted:
            return

        dialog_payload = dict(dlg.params_payload())
        selected_tool_slot = dialog_payload.pop("selected_tool_slot", None)
        surfacing_tool = self._tool_by_slot(selected_tool_slot if isinstance(selected_tool_slot, int) else None)
        params = SurfacingParams(**dialog_payload)

        def on_done(layer):
            tool_summary = f"manual dia {params.tool_diameter_mm:.3f} mm"
            if surfacing_tool is not None:
                tool_label = str(getattr(surfacing_tool, "tool_number", "") or "").strip()
                if not tool_label:
                    tool_label = str(getattr(surfacing_tool, "slot", ""))
                layer.metadata["selected_cutting_tool"] = tool_label
                layer.metadata["selected_surfacing_tool"] = tool_label
                tool_summary = f"tool {tool_label}"
            layer.metadata["selected_cutting_depth_mm"] = (
                f"{self._safe_float(self._selected_tools_assignments.get('cutting_depth_mm'), 0.0):.6f}"
            )
            layer.metadata["selected_cutting_speed_mm_s"] = (
                f"{self._safe_float(self._selected_tools_assignments.get('cutting_speed_mm_s'), 0.0):.6f}"
            )
            self.project.add_layer(layer)
            self._rebuild_scene()
            self._select_layer_item(len(self.project.layers) - 1)
            self._fit_canvas_to_viewport()

            pass_count = self._safe_int(layer.metadata.get("surfacing_pass_count"), 0)
            min_x = self._safe_float(layer.metadata.get("surfacing_bounds_min_x_mm"), 0.0)
            max_x = self._safe_float(layer.metadata.get("surfacing_bounds_max_x_mm"), 0.0)
            min_y = self._safe_float(layer.metadata.get("surfacing_bounds_min_y_mm"), 0.0)
            max_y = self._safe_float(layer.metadata.get("surfacing_bounds_max_y_mm"), 0.0)
            self.statusBar().showMessage(
                (
                    f"Surfacing toolpath generated from '{source_name}' "
                    f"for bbox X[{min_x:.3f},{max_x:.3f}] Y[{min_y:.3f},{max_y:.3f}] "
                    f"with {pass_count} pass(es), {tool_summary}"
                ),
                5000,
            )

        self._run_process_task(
            title=f"Surfacing Toolpath {source_name}",
            task_name="surfacing_generate",
            payload={
                "source_layer": serialize_layer(source_layer),
                "params": surfacing_params_payload(params),
            },
            decode_result=lambda data: deserialize_layer(dict(data["generated_layer"])),
            on_success=on_done,
            failure_title="Surfacing Toolpath Failed",
        )

    def _layer_bbox_for_dialog(
        self,
        layer,
        *,
        fallback: tuple[float, float, float, float] = (0.0, 0.0, 100.0, 100.0),
    ) -> tuple[float, float, float, float]:
        out = self._layer_bbox_or_none(layer, transformed=True)
        if out is not None:
            return out
        return fallback

    def _layer_bbox_or_none(self, layer, *, transformed: bool = False) -> tuple[float, float, float, float] | None:
        bbox = getattr(layer, "bbox", None)
        if isinstance(bbox, tuple) and len(bbox) == 4:
            try:
                min_x = float(bbox[0])
                min_y = float(bbox[1])
                max_x = float(bbox[2])
                max_y = float(bbox[3])
                if (max_x - min_x) > 1e-9 and (max_y - min_y) > 1e-9:
                    out = (min_x, min_y, max_x, max_y)
                    return self._transform_layer_bbox(layer, out) if transformed else out
            except Exception:
                pass

        src_bounds = getattr(getattr(layer, "source", None), "bounds", None)
        if isinstance(src_bounds, tuple) and len(src_bounds) == 2:
            try:
                x_pair, y_pair = src_bounds
                min_x = float(min(x_pair[0], x_pair[1]))
                max_x = float(max(x_pair[0], x_pair[1]))
                min_y = float(min(y_pair[0], y_pair[1]))
                max_y = float(max(y_pair[0], y_pair[1]))
                if (max_x - min_x) > 1e-9 and (max_y - min_y) > 1e-9:
                    out = (min_x, min_y, max_x, max_y)
                    return self._transform_layer_bbox(layer, out) if transformed else out
            except Exception:
                pass
        return None

    def _combined_layer_bbox(
        self,
        layer_indices: list[int],
        *,
        transformed: bool,
    ) -> tuple[float, float, float, float] | None:
        min_x = float("inf")
        min_y = float("inf")
        max_x = float("-inf")
        max_y = float("-inf")
        found = False
        for idx in layer_indices:
            if idx < 0 or idx >= len(self.project.layers):
                continue
            layer = self.project.layers[idx]
            bbox = self._layer_bbox_or_none(layer, transformed=transformed)
            if bbox is None:
                continue
            bx0, by0, bx1, by1 = bbox
            min_x = min(min_x, bx0)
            min_y = min(min_y, by0)
            max_x = max(max_x, bx1)
            max_y = max(max_y, by1)
            found = True
        if not found:
            return None
        return min_x, min_y, max_x, max_y

    @staticmethod
    def _transform_layer_bbox(
        layer,
        bbox: tuple[float, float, float, float],
    ) -> tuple[float, float, float, float]:
        min_x, min_y, max_x, max_y = bbox
        sx = -1.0 if bool(getattr(layer, "mirror_x", False)) else 1.0
        sy = -1.0 if bool(getattr(layer, "mirror_y", False)) else 1.0
        rot = math.radians(float(getattr(layer, "rotation_deg", 0.0) or 0.0))
        cr = math.cos(rot)
        sr = math.sin(rot)
        tx = float(getattr(layer, "offset_x_mm", 0.0) or 0.0)
        ty = float(getattr(layer, "offset_y_mm", 0.0) or 0.0)

        points: list[tuple[float, float]] = []
        for x, y in (
            (min_x, min_y),
            (min_x, max_y),
            (max_x, min_y),
            (max_x, max_y),
        ):
            mx = float(x) * sx
            my = float(y) * sy
            rx = mx * cr - my * sr
            ry = mx * sr + my * cr
            points.append((rx + tx, ry + ty))

        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return min(xs), min(ys), max(xs), max(ys)

    def _imported_board_bbox(self) -> tuple[float, float, float, float] | None:
        preferred = self._imported_bbox_for_roles({"top", "bottom", "cutout", "artwork"})
        if preferred is not None:
            return preferred
        return self._imported_bbox_for_roles(None)

    def _imported_bbox_for_roles(self, roles: set[str] | None) -> tuple[float, float, float, float] | None:
        min_x = float("inf")
        min_y = float("inf")
        max_x = float("-inf")
        max_y = float("-inf")
        found = False
        for layer in self.project.layers:
            if self._is_generated_layer(layer):
                continue
            role = str(getattr(layer, "role", "unassigned")).strip().lower()
            if roles is not None and role not in roles:
                continue
            bbox = self._layer_bbox_or_none(layer, transformed=True)
            if bbox is None:
                continue
            bx0, by0, bx1, by1 = bbox
            min_x = min(min_x, bx0)
            min_y = min(min_y, by0)
            max_x = max(max_x, bx1)
            max_y = max(max_y, by1)
            found = True
        if not found:
            return None
        return min_x, min_y, max_x, max_y

    @staticmethod
    def _layer_is_derived_from(layer, source_layer) -> bool:  # noqa: ANN001
        meta = getattr(layer, "metadata", {}) or {}
        source_name = str(getattr(source_layer, "name", "") or "").strip()
        derived_names = {
            part.strip()
            for part in str(meta.get("derived_from", "")).split(",")
            if part.strip()
        }
        if source_name and source_name in derived_names:
            return True
        derived_path = str(meta.get("path", "")).strip()
        source_path = str(getattr(source_layer, "path", "") or "").strip()
        return bool(derived_path and source_path and derived_path == source_path)

    def _layer_is_derived_from_any(self, layer, source_layers: list[Layer]) -> bool:  # noqa: ANN001
        return any(self._layer_is_derived_from(layer, source_layer) for source_layer in source_layers)

    @staticmethod
    def _source_layers_label(source_layers: list[Layer]) -> str:
        names = [str(getattr(layer, "name", "") or "").strip() for layer in source_layers]
        names = [name for name in names if name]
        if not names:
            return "selected layers"
        if len(names) <= 2:
            return ", ".join(names)
        return f"{names[0]}, {names[1]}, +{len(names) - 2} more"

    def _first_reference_layer(self, *, default_bounds: tuple[float, float, float, float]) -> Layer:
        for layer in self.project.layers:
            if not self._is_generated_layer(layer):
                return layer
        if self.project.layers:
            return self.project.layers[0]
        return self._virtual_reference_layer(default_bounds=default_bounds)

    def _virtual_reference_layer(self, *, default_bounds: tuple[float, float, float, float]) -> Layer:
        min_x, min_y, max_x, max_y = default_bounds
        source = SimpleNamespace(
            units="mm",
            primitives=[],
            bounds=((float(min_x), float(max_x)), (float(min_y), float(max_y))),
        )
        return Layer(
            name="manual_reference",
            path=Path("manual_reference.virtual"),
            kind="geometry",
            source=source,
            role="unassigned",
            bbox=(float(min_x), float(min_y), float(max_x), float(max_y)),
            metadata={"virtual_source": "true"},
        )

    def _selected_engraving_tool(self):
        slot_value = self._selected_tools_assignments.get("engraving")
        return self._tool_by_slot(slot_value if isinstance(slot_value, int) else None)

    def _selected_hatching_tool(self):
        slot_value = self._selected_tools_assignments.get("hatching")
        return self._tool_by_slot(slot_value if isinstance(slot_value, int) else None)

    def _selected_cutting_tool(self):
        slot_value = self._selected_tools_assignments.get("cutting")
        return self._tool_by_slot(slot_value if isinstance(slot_value, int) else None)

    def _selected_centering_tool(self):
        slot_value = self._selected_tools_assignments.get("centering")
        return self._tool_by_slot(slot_value if isinstance(slot_value, int) else None)

    def _selected_drill_single_tool(self):
        slot_value = self._selected_tools_assignments.get("drill_single_tool_slot")
        return self._tool_by_slot(slot_value if isinstance(slot_value, int) else None)

    def _tool_by_slot(self, slot_value: int | None):
        if not isinstance(slot_value, int):
            return None
        for tool in self._global_tool_library:
            if not bool(getattr(tool, "defined", False)):
                continue
            try:
                if int(getattr(tool, "slot", -1)) == int(slot_value):
                    return tool
            except Exception:
                continue
        return None

    def _active_cutout_tool_diameter_mm(self) -> float:
        tool = self._selected_cutting_tool()
        if tool is not None:
            return max(0.001, self._safe_float(getattr(tool, "diameter_mm", 0.0), 0.0))
        return max(0.001, float(self.cutout_tool_dia_spin.value()))

    def _update_cutout_preset_summary(self) -> None:
        if not hasattr(self, "cutout_preset_summary_label"):
            return
        self.cutout_preset_summary_label.setText(self._cutout_preset_summary_text())

    def _cutout_preset_summary_text(self) -> str:
        tool = self._selected_cutting_tool()
        depth_mm = max(0.0, self._safe_float(self._selected_tools_assignments.get("cutting_depth_mm"), 0.0))
        speed_mm_s = max(0.0, self._safe_float(self._selected_tools_assignments.get("cutting_speed_mm_s"), 0.0))
        if tool is None:
            manual_dia = max(0.001, float(self.cutout_tool_dia_spin.value()))
            return (
                "Preset source: manual cutout settings.\n"
                f"Applied tool diameter: {manual_dia:.4f} mm\n"
                f"Cutting depth preset: {depth_mm:.4f} mm\n"
                f"Cutting speed preset: {speed_mm_s:.3f} mm/s\n"
                "Tip: set Parameters > Selected tools > Cutting tool to lock this planner to a tool preset."
            )

        tool_no = str(getattr(tool, "tool_number", "") or "").strip() or str(getattr(tool, "slot", ""))
        tool_name = str(getattr(tool, "name", "") or "").strip() or "Unnamed"
        profile = str(getattr(tool, "profile", "cylindrical/flute") or "cylindrical/flute").strip().lower()
        dia_mm = max(0.001, self._safe_float(getattr(tool, "diameter_mm", 0.0), 0.0))
        plunge_mm_s = max(0.0, self._safe_float(getattr(tool, "plunge_mm_s", 0.0), 0.0))
        max_depth_pass = max(0.0, self._safe_float(getattr(tool, "max_depth_per_pass_mm", 0.0), 0.0))
        max_rpm = max(0, self._safe_int(getattr(tool, "max_rpm", 0), 0))

        lines = [
            f"Preset source: Selected tools > Cutting tool (tool {tool_no}: {tool_name})",
            f"Profile: {profile}",
            f"Applied tool diameter: {dia_mm:.4f} mm",
            f"Cutting depth preset: {depth_mm:.4f} mm",
            f"Cutting speed preset: {speed_mm_s:.3f} mm/s",
            f"Plunge speed limit: {plunge_mm_s:.3f} mm/s",
            f"Max depth per pass: {max_depth_pass:.4f} mm",
        ]
        if max_rpm > 0:
            lines.append(f"Spindle speed limit: {max_rpm} rpm")

        if profile == "conical":
            angle_deg = max(0.0, self._safe_float(getattr(tool, "angle_deg", 0.0), 0.0))
            tip_dia = max(0.0, self._safe_float(getattr(tool, "tip_diameter_mm", 0.0), 0.0))
            if tip_dia <= 0.0:
                tip_dia = dia_mm
            calc = calculate_coppercam_params(T_dia=tip_dia, T_angle=angle_deg, D_cut=depth_mm)
            eff_dia = max(0.0, self._safe_float(calc.get("total_path_width"), 0.0))
            lines.append(f"Conical angle: {angle_deg:.2f} deg, tip diameter: {tip_dia:.4f} mm")
            lines.append(f"Effective diameter at depth: {eff_dia:.4f} mm")

        return "\n".join(lines)

    def _sync_cutout_tool_controls(self) -> None:
        tool = self._selected_cutting_tool()
        if tool is None:
            self.cutout_tool_dia_spin.setEnabled(True)
            self.cutout_tool_dia_spin.setToolTip(
                "Manual diameter (used if no cutting tool is selected in Parameters > Selected tools...)"
            )
            self._update_cutout_preset_summary()
            if self._cutout_plan_active:
                self._on_cutout_setting_changed()
            return

        dia = max(0.001, self._safe_float(getattr(tool, "diameter_mm", 0.0), 0.0))
        old = self.cutout_tool_dia_spin.blockSignals(True)
        try:
            self.cutout_tool_dia_spin.setValue(dia)
        finally:
            self.cutout_tool_dia_spin.blockSignals(old)
        self.cutout_tool_dia_spin.setEnabled(False)
        tool_label = str(getattr(tool, "tool_number", "") or "").strip() or str(getattr(tool, "slot", ""))
        self.cutout_tool_dia_spin.setToolTip(
            f"Using Selected tools > Cutting tool preset (tool {tool_label}, {dia:.4f} mm)"
        )
        self._update_cutout_preset_summary()
        if self._cutout_plan_active:
            self._on_cutout_setting_changed()

    def _on_cutout_planner_reopened(self) -> None:
        if not self._cutout_plan_active:
            self._update_cutout_preview_state_hint()
            return
        if self._has_generated_cutout_toolpath(source_layer_indices=self._active_cutout_source_indices()):
            # Keep lock armed while a generated cutout layer still exists.
            self._clear_cutout_preview()
            self._update_cutout_preview_state_hint()
            return
        if self._cutout_preview_locked_until_reopen:
            self._cutout_preview_locked_until_reopen = False
        if self._cutout_plan_loops:
            # Force refresh so preview is visible immediately after planner is shown again.
            self._schedule_cutout_preview_update()
            return
        self._update_cutout_preview_state_hint()

    def _open_cutout_planner(self) -> None:
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            return
        source_indices = self._selected_layer_indices()
        if not source_indices:
            QMessageBox.information(self, "Cutout Toolpath", "Select a cutout/gerber layer first.")
            return

        source_layers = [self.project.layers[idx] for idx in source_indices]
        if any(layer.role != "cutout" for layer in source_layers):
            QMessageBox.information(
                self,
                "Cutout Toolpath",
                "Cutout toolpath can be generated only from a layer with role 'cutout'.",
            )
            return
        if any(layer.kind not in {"gerber", "geometry"} for layer in source_layers):
            QMessageBox.information(self, "Cutout Toolpath", "Cutout toolpath currently supports Gerber/geometry layers.")
            return

        # Fast reopen path: preserve cached loop extraction for same source layers.
        if (
            self._cutout_plan_active
            and self._active_cutout_source_indices() == source_indices
            and bool(self._cutout_plan_loops)
        ):
            was_hidden = not bool(self.cutout_dock.isVisible())
            self._sync_cutout_tool_controls()
            self._update_cutout_source_label()
            self.cutout_dock.show()
            if was_hidden:
                self._on_cutout_planner_reopened()
            elif self._cutout_preview_layer is None:
                self._schedule_cutout_preview_update()
            elif self._cutout_preview_item is None:
                self._apply_cutout_preview_layer()
            return

        source_name = self._source_layers_label(source_layers)

        def on_done(loop_items):
            loops = [item[0] for item in loop_items]
            loop_sources = [int(item[1]) for item in loop_items]
            if not loops:
                QMessageBox.warning(self, "Cutout Toolpath Failed", "No closed loops were found in the selected cutout layer(s).")
                return
            self._cutout_preview_locked_until_reopen = False
            self._cutout_plan_active = True
            self._cutout_plan_source_layer_index = source_indices[0]
            self._cutout_plan_source_layer_indices = list(source_indices)
            self._cutout_plan_loops = loops
            self._cutout_plan_loop_source_positions = loop_sources
            self._cutout_plan_compensations = self._default_cutout_loop_compensations(loops)
            self._sync_cutout_tool_controls()
            self._populate_cutout_table()
            self._update_cutout_source_label()
            self.cutout_dock.show()
            self._refresh_canvas(fit=False)
            self._schedule_cutout_preview_update()

        def decode_loops(data):
            from shapely.geometry import Polygon

            decoded = []
            for item in data.get("loops", []) or []:
                ext = [(float(p[0]), float(p[1])) for p in item.get("exterior", [])]
                interiors = []
                for ring in item.get("interiors", []) or []:
                    interiors.append([(float(p[0]), float(p[1])) for p in ring])
                if len(ext) >= 3:
                    decoded.append((Polygon(ext, interiors), int(item.get("source_pos", 0))))
            return decoded

        self._run_process_task(
            title=f"Cutout Loop Extraction {source_name}",
            task_name="cutout_extract_loops_multi" if len(source_layers) > 1 else "cutout_extract_loops",
            payload={
                "source_layer": serialize_layer(source_layers[0]),
                "source_layers": [serialize_layer(layer) for layer in source_layers],
            },
            decode_result=decode_loops,
            on_success=on_done,
            failure_title="Cutout Toolpath Failed",
        )

    def _apply_cutout_plan(self) -> None:
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            return
        source_indices = self._active_cutout_source_indices()
        if not self._cutout_plan_active or not source_indices:
            return
        self._cancel_cutout_preview_task(clear_pending=True, clear_cache=False)
        if any(idx < 0 or idx >= len(self.project.layers) for idx in source_indices):
            return
        source_layers = [self.project.layers[idx] for idx in source_indices]
        cutting_tool = self._selected_cutting_tool()
        tool_dia = self._active_cutout_tool_diameter_mm()
        params = CutoutParams(
            tool_diameter_mm=tool_dia,
            compensation="outside",
            loop_compensations=list(self._cutout_plan_compensations),
            holding_tab_count=int(self.cutout_tab_count_spin.value()),
            holding_tab_width_mm=float(self.cutout_tab_width_spin.value()),
        )
        source_name = self._source_layers_label(source_layers)
        comps_by_layer = self._cutout_loop_compensations_by_source(len(source_layers))

        def on_done(layer):
            self._cutout_preview_locked_until_reopen = True
            self._cancel_cutout_preview_task(clear_pending=True, clear_cache=True)
            if cutting_tool is not None:
                tool_label = str(getattr(cutting_tool, "tool_number", "") or "").strip() or str(
                    getattr(cutting_tool, "slot", "")
                )
                layer.metadata["selected_cutting_tool"] = tool_label
            layer.metadata["selected_cutting_depth_mm"] = (
                f"{self._safe_float(self._selected_tools_assignments.get('cutting_depth_mm'), 0.0):.6f}"
            )
            layer.metadata["selected_cutting_speed_mm_s"] = (
                f"{self._safe_float(self._selected_tools_assignments.get('cutting_speed_mm_s'), 0.0):.6f}"
            )
            self.project.add_layer(layer)
            self._rebuild_scene()
            self._select_layer_item(len(self.project.layers) - 1)
            self._fit_canvas_to_viewport()
            if cutting_tool is not None:
                tool_label = str(getattr(cutting_tool, "tool_number", "") or "").strip() or str(
                    getattr(cutting_tool, "slot", "")
                )
                self.statusBar().showMessage(
                    f"Cutout toolpath generated from '{source_name}' using cutting tool {tool_label}",
                    4500,
                )
            else:
                self.statusBar().showMessage(
                    f"Cutout toolpath generated from '{source_name}' (manual diameter {tool_dia:.3f} mm)",
                    4500,
                )

        self._run_process_task(
            title=f"Cutout Toolpath {source_name}",
            task_name="cutout_generate_multi" if len(source_layers) > 1 else "cutout_generate",
            payload={
                "source_layer": serialize_layer(source_layers[0]),
                "source_layers": [serialize_layer(layer) for layer in source_layers],
                "params": cutout_params_payload(params),
                "loop_compensations_by_layer": comps_by_layer,
            },
            decode_result=lambda data: deserialize_layer(dict(data["generated_layer"])),
            on_success=on_done,
            failure_title="Cutout Toolpath Failed",
        )

    def _close_cutout_planner(self) -> None:
        # Fast close: hide planner UI but keep computed loop cache.
        self.cutout_dock.hide()
        self._update_cutout_preview_state_hint()

    def _reset_cutout_planner(self, *, refresh_canvas: bool = True) -> None:
        self._cancel_cutout_preview_task(clear_pending=True, clear_cache=False)
        self._cutout_preview_locked_until_reopen = False
        self._cutout_plan_active = False
        self._cutout_plan_source_layer_index = None
        self._cutout_plan_source_layer_indices = []
        self._cutout_plan_loops = []
        self._cutout_plan_loop_source_positions = []
        self._cutout_plan_compensations = []
        self.cutout_loop_table.setRowCount(0)
        self.cutout_source_label.setText("Source: (none)")
        self.cutout_dock.hide()
        self._clear_cutout_preview()
        self._update_cutout_preview_state_hint()
        if refresh_canvas:
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
            delete_icon = icon_for("layer_delete", size=14, color="#111827")
            if not delete_icon.isNull():
                delete_button.setIcon(delete_icon)
                delete_button.setIconSize(QSize(12, 12))
                delete_button.setText("")
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
        layer_meta = getattr(layer, "metadata", {}) or {}
        layer_kind = str(layer_meta.get("kind", "")).strip().lower()
        layer_is_preview = str(layer_meta.get("preview", "")).strip().lower() in {"1", "true", "yes", "on"}
        layer_derived_from = str(layer_meta.get("derived_from", "")).strip()
        active_source_names = self._active_cutout_source_names()
        derived_names = {part.strip() for part in layer_derived_from.split(",") if part.strip()}
        deleting_active_generated_cutout = bool(
            layer_kind == "cutout_toolpath"
            and not layer_is_preview
            and (
                not active_source_names
                or not layer_derived_from
                or bool(derived_names & set(active_source_names))
            )
        )
        answer = QMessageBox.question(
            self,
            "Delete Layer",
            f"Delete layer '{layer.name}'?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        if deleting_active_generated_cutout:
            # Keep preview disabled after deletion until planner is explicitly reopened.
            self._cutout_preview_locked_until_reopen = True
            self._cancel_cutout_preview_task(clear_pending=True, clear_cache=True)
        self.project.layers.pop(layer_index)
        if self._cutout_plan_source_layer_index is not None:
            if self._cutout_plan_source_layer_index == layer_index:
                self._reset_cutout_planner(refresh_canvas=False)
            elif self._cutout_plan_source_layer_index > layer_index:
                self._cutout_plan_source_layer_index -= 1
        if self._cutout_plan_source_layer_indices:
            if layer_index in self._cutout_plan_source_layer_indices:
                self._reset_cutout_planner(refresh_canvas=False)
            else:
                self._cutout_plan_source_layer_indices = [
                    idx - 1 if idx > layer_index else idx
                    for idx in self._cutout_plan_source_layer_indices
                ]
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

    def _selected_layer_indices(self) -> list[int]:
        indices: list[int] = []
        seen: set[int] = set()
        for item in self.layer_tree.selectedItems():
            idx = self._layer_index_from_item(item)
            if idx is None or idx in seen:
                continue
            if 0 <= idx < len(self.project.layers):
                indices.append(idx)
                seen.add(idx)
        current = self._current_layer_index()
        if not indices and current is not None and 0 <= current < len(self.project.layers):
            indices.append(current)
        return indices

    def _selected_layers_for_operation(self) -> list[Layer]:
        return [self.project.layers[idx] for idx in self._selected_layer_indices()]

    def _active_cutout_source_indices(self) -> list[int]:
        indices = list(getattr(self, "_cutout_plan_source_layer_indices", []) or [])
        if indices:
            return [idx for idx in indices if 0 <= idx < len(self.project.layers)]
        idx = self._cutout_plan_source_layer_index
        if idx is not None and 0 <= int(idx) < len(self.project.layers):
            return [int(idx)]
        return []

    def _active_cutout_source_names(self) -> list[str]:
        return [str(self.project.layers[idx].name) for idx in self._active_cutout_source_indices()]

    def _cutout_loop_compensations_by_source(self, source_count: int) -> list[list[str]]:
        grouped: list[list[str]] = [[] for _ in range(max(0, int(source_count)))]
        positions = list(getattr(self, "_cutout_plan_loop_source_positions", []) or [])
        if len(positions) != len(self._cutout_plan_compensations):
            positions = [0 for _ in self._cutout_plan_compensations]
        for comp, source_pos in zip(self._cutout_plan_compensations, positions):
            if 0 <= int(source_pos) < len(grouped):
                grouped[int(source_pos)].append(str(comp))
        return grouped

    def _cutout_loop_source_name(self, loop_row: int) -> str:
        positions = list(getattr(self, "_cutout_plan_loop_source_positions", []) or [])
        source_indices = self._active_cutout_source_indices()
        if 0 <= loop_row < len(positions):
            source_pos = int(positions[loop_row])
            if 0 <= source_pos < len(source_indices):
                return str(self.project.layers[source_indices[source_pos]].name)
        if source_indices:
            return str(self.project.layers[source_indices[0]].name)
        return ""

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
        if self.layers_activity_bar is not None:
            self.layers_activity_bar.setStyleSheet(
                """
                QFrame#layersActivityBar {
                    background-color: #161b22;
                    border-right: 1px solid #30363d;
                }
                QToolButton#layersActivityButton {
                    background-color: transparent;
                    color: #8b949e;
                    border: none;
                    border-left: 3px solid transparent;
                    border-radius: 0px;
                    padding-left: 6px;
                }
                QToolButton#layersActivityButton:hover {
                    color: #c9d1d9;
                    background-color: rgba(110, 118, 129, 0.12);
                    border-left: 3px solid transparent;
                }
                QToolButton#layersActivityButton:checked {
                    color: #ffffff;
                    background-color: transparent;
                    border-left: 3px solid #58a6ff;
                }
                QToolButton#layersActivityButton:pressed {
                    background-color: rgba(110, 118, 129, 0.16);
                }
                """
            )

    def _build_cutout_panel(self) -> QWidget:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.cutout_preset_group = QGroupBox("Cutout Preset Summary", panel)
        preset_layout = QVBoxLayout(self.cutout_preset_group)
        preset_layout.setContentsMargins(8, 8, 8, 8)
        preset_layout.setSpacing(4)
        self.cutout_preset_summary_label = QLabel(self.cutout_preset_group)
        self.cutout_preset_summary_label.setWordWrap(True)
        self.cutout_preset_summary_label.setText(
            "Loading preset summary..."
        )
        preset_layout.addWidget(self.cutout_preset_summary_label)
        layout.addWidget(self.cutout_preset_group)

        self.cutout_source_label = QLabel("Source: (none)", panel)
        layout.addWidget(self.cutout_source_label)
        self.cutout_help_label = QLabel("Click loop geometry in viewport to select loop row.", panel)
        layout.addWidget(self.cutout_help_label)
        self.cutout_preview_state_label = QLabel("Preview status: inactive.", panel)
        self.cutout_preview_state_label.setWordWrap(True)
        self.cutout_preview_state_label.setStyleSheet("color: #7f8c9a;")
        layout.addWidget(self.cutout_preview_state_label)

        self.cutout_tool_dia_spin = QDoubleSpinBox(panel)
        self.cutout_tool_dia_spin.setDecimals(3)
        self.cutout_tool_dia_spin.setRange(0.001, 1000.0)
        self.cutout_tool_dia_spin.setValue(1.0)
        self.cutout_tool_dia_spin.setPrefix("Tool dia (mm): ")
        self.cutout_tool_dia_spin.setToolTip("Manual diameter (used if no cutting tool is selected in Parameters > Selected tools...)")
        self.cutout_tool_dia_spin.valueChanged.connect(self._on_cutout_setting_changed)
        layout.addWidget(self.cutout_tool_dia_spin)

        self.cutout_tabs_group = QGroupBox("Holding Breaks", panel)
        tabs_layout = QHBoxLayout(self.cutout_tabs_group)
        tabs_layout.setContentsMargins(8, 8, 8, 8)
        tabs_layout.setSpacing(6)

        self.cutout_tab_count_spin = QSpinBox(self.cutout_tabs_group)
        self.cutout_tab_count_spin.setRange(0, 64)
        self.cutout_tab_count_spin.setValue(0)
        self.cutout_tab_count_spin.setPrefix("Count: ")
        self.cutout_tab_count_spin.setToolTip("Number of uncut holding breaks to leave around each cutout loop")
        self.cutout_tab_count_spin.valueChanged.connect(self._on_cutout_setting_changed)

        self.cutout_tab_width_spin = QDoubleSpinBox(self.cutout_tabs_group)
        self.cutout_tab_width_spin.setDecimals(3)
        self.cutout_tab_width_spin.setRange(0.001, 1000.0)
        self.cutout_tab_width_spin.setValue(1.0)
        self.cutout_tab_width_spin.setPrefix("Width (mm): ")
        self.cutout_tab_width_spin.setToolTip("Length of each uncut holding break along the cutout path")
        self.cutout_tab_width_spin.valueChanged.connect(self._on_cutout_setting_changed)

        tabs_layout.addWidget(self.cutout_tab_count_spin)
        tabs_layout.addWidget(self.cutout_tab_width_spin)
        layout.addWidget(self.cutout_tabs_group)

        self.cutout_loop_table = QTableWidget(panel)
        self.cutout_loop_table.setColumnCount(4)
        self.cutout_loop_table.setHorizontalHeaderLabels(["Loop", "Source", "Area (mm^2)", "Comp"])
        self.cutout_loop_table.verticalHeader().setVisible(False)
        self.cutout_loop_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.cutout_loop_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.cutout_loop_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.cutout_loop_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.cutout_loop_table.itemSelectionChanged.connect(self._refresh_canvas_no_fit)
        layout.addWidget(self.cutout_loop_table, 1)

        self.cutout_apply_button = QPushButton("Generate Toolpath", panel)
        self.cutout_close_button = QPushButton("Close Planner", panel)
        self.cutout_apply_button.clicked.connect(self._apply_cutout_plan)
        self.cutout_close_button.clicked.connect(self._close_cutout_planner)
        layout.addWidget(self.cutout_apply_button)
        layout.addWidget(self.cutout_close_button)
        self._update_cutout_preset_summary()
        self._update_cutout_preview_state_hint()
        return panel

    def _populate_cutout_table(self) -> None:
        self.cutout_loop_table.blockSignals(True)
        self.cutout_loop_table.setRowCount(len(self._cutout_plan_loops))
        for row, loop in enumerate(self._cutout_plan_loops):
            idx_item = QTableWidgetItem(str(row + 1))
            idx_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            source_item = QTableWidgetItem(self._cutout_loop_source_name(row))
            source_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            area_item = QTableWidgetItem(f"{loop.area:.3f}")
            area_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            combo = QComboBox(self.cutout_loop_table)
            combo.addItems(["outside", "inside", "onpath"])
            combo.setCurrentText(self._cutout_plan_compensations[row])
            combo.currentTextChanged.connect(lambda value, r=row: self._on_cutout_comp_changed(r, value))
            self.cutout_loop_table.setItem(row, 0, idx_item)
            self.cutout_loop_table.setItem(row, 1, source_item)
            self.cutout_loop_table.setItem(row, 2, area_item)
            self.cutout_loop_table.setCellWidget(row, 3, combo)
        self.cutout_loop_table.blockSignals(False)
        if self.cutout_loop_table.rowCount() > 0:
            self.cutout_loop_table.selectRow(0)

    def _on_cutout_comp_changed(self, row: int, value: str) -> None:
        if 0 <= row < len(self._cutout_plan_compensations):
            self._cutout_plan_compensations[row] = value
            self._on_cutout_setting_changed()

    def _on_cutout_setting_changed(self) -> None:
        self._update_cutout_preset_summary()
        if not self._cutout_plan_active:
            return
        self._schedule_cutout_preview_update()

    def _refresh_canvas_no_fit(self) -> None:
        if self._cutout_plan_active:
            self._highlight_selected_loop()
            return
        self._refresh_canvas(fit=False)

    def _update_cutout_source_label(self) -> None:
        source_indices = self._active_cutout_source_indices()
        if not source_indices:
            self.cutout_source_label.setText("Source: (none)")
            self._update_cutout_preview_state_hint()
            return
        source_layers = [self.project.layers[idx] for idx in source_indices]
        self.cutout_source_label.setText(
            f"Source: {self._source_layers_label(source_layers)} ({len(self._cutout_plan_loops)} loops)"
        )
        self._update_cutout_preview_state_hint()

    def _update_cutout_preview_state_hint(self) -> None:
        if not hasattr(self, "cutout_preview_state_label"):
            return
        label = self.cutout_preview_state_label
        source_indices = self._active_cutout_source_indices()
        if not self._cutout_plan_active or not source_indices:
            label.setText("Preview status: inactive.")
            label.setStyleSheet("color: #7f8c9a;")
            return
        if self._cutout_preview_locked_until_reopen:
            label.setText("Preview disabled: reopen the Cutout Planner to re-enable preview.")
            label.setStyleSheet("color: #f0b429;")
            return
        if self._has_generated_cutout_toolpath(source_layer_indices=source_indices):
            label.setText(
                "Preview disabled: generated cutout toolpath exists. Delete that cutout toolpath layer to re-enable preview."
            )
            label.setStyleSheet("color: #f0b429;")
            return
        if self._preview_task_process is not None or self._preview_debounce_timer.isActive() or self._preview_pending_payload:
            label.setText("Preview status: generating in background...")
            label.setStyleSheet("color: #58a6ff;")
            return
        if self._cutout_preview_layer is not None:
            label.setText("Preview status: enabled.")
            label.setStyleSheet("color: #7ee787;")
            return
        label.setText("Preview status: enabled (waiting for update).")
        label.setStyleSheet("color: #7f8c9a;")

    def _render_cutout_preview(self) -> None:
        # Kept for compatibility with existing call sites; preview is now async.
        self._schedule_cutout_preview_update()

    def _schedule_cutout_preview_update(self) -> None:
        # Preview lifecycle rules:
        # 1) preview is visible only while planner dock is visible,
        # 2) preview is locked once a generated cutout layer exists,
        # 3) preview is re-enabled only by reopening planner.
        if self.cutout_dock is not None and not self.cutout_dock.isVisible():
            self._cancel_cutout_preview_task(clear_pending=True, clear_cache=False)
            self._remove_cutout_preview_item()
            return
        source_indices = self._active_cutout_source_indices()
        if not self._cutout_plan_active or not source_indices:
            self._update_cutout_preview_state_hint()
            return
        if self._cutout_preview_locked_until_reopen:
            self._cancel_cutout_preview_task(clear_pending=True, clear_cache=True)
            self._update_cutout_preview_state_hint()
            return
        if any(idx < 0 or idx >= len(self.project.layers) for idx in source_indices):
            self._update_cutout_preview_state_hint()
            return
        if self._has_generated_cutout_toolpath(source_layer_indices=source_indices):
            # Arm lock so deleting generated output does not auto-enable preview.
            self._cutout_preview_locked_until_reopen = True
            self._cancel_cutout_preview_task(clear_pending=True, clear_cache=True)
            self._update_cutout_preview_state_hint()
            return
        source_layers = [self.project.layers[idx] for idx in source_indices]
        params = CutoutParams(
            tool_diameter_mm=self._active_cutout_tool_diameter_mm(),
            compensation="outside",
            loop_compensations=list(self._cutout_plan_compensations),
            holding_tab_count=int(self.cutout_tab_count_spin.value()),
            holding_tab_width_mm=float(self.cutout_tab_width_spin.value()),
        )
        self._preview_request_token += 1
        self._preview_pending_payload = {
            "token": int(self._preview_request_token),
            "source_layer_index": int(source_indices[0]),
            "source_layer_indices": list(source_indices),
            "source_layer": serialize_layer(source_layers[0]),
            "source_layers": [serialize_layer(layer) for layer in source_layers],
            "params": cutout_params_payload(params),
            "loop_compensations_by_layer": self._cutout_loop_compensations_by_source(len(source_layers)),
        }
        if self._task_running:
            # Queue latest payload; _finish_task_state() will trigger preview start.
            self._update_cutout_preview_state_hint()
            return
        self._preview_debounce_timer.start()
        self._update_cutout_preview_state_hint()

    def _start_cutout_preview_task(self) -> None:
        if self._task_running:
            return
        payload = self._preview_pending_payload
        if not payload:
            return
        self._preview_pending_payload = None
        self._cancel_cutout_preview_task(clear_pending=False, clear_cache=False)
        token = int(payload.get("token", 0))
        self._preview_inflight_token = token
        self._preview_task_queue = mp.Queue()
        # Preview generation uses the same worker entrypoint as final generation,
        # but result is marked as preview and never added to project layers.
        self._preview_task_process = mp.Process(
            target=run_task_process_entry,
            args=(
                "cutout_generate_multi"
                if len(list(payload.get("source_layers", []) or [])) > 1
                else "cutout_generate",
                {
                    "source_layer": payload["source_layer"],
                    "source_layers": payload.get("source_layers", []),
                    "params": payload["params"],
                    "loop_compensations_by_layer": payload.get("loop_compensations_by_layer", []),
                },
                self._preview_task_queue,
            ),
            daemon=True,
        )
        self._preview_task_process.start()
        self._preview_task_poll_timer.start()
        self._update_cutout_preview_state_hint()

    def _poll_preview_task_queue(self) -> None:
        if self._preview_task_queue is None:
            return
        while True:
            try:
                msg = self._preview_task_queue.get_nowait()
            except Empty:
                break
            if not isinstance(msg, dict):
                continue
            mtype = str(msg.get("type", ""))
            if mtype == "log":
                # Keep preview generation non-intrusive; do not spam overlay logs.
                continue
            if mtype == "error":
                LOGGER.warning("Cutout preview task failed: %s", str(msg.get("message", "")))
                self._finish_preview_task_state()
                self._update_cutout_preview_state_hint()
                return
            if mtype == "result":
                token = int(self._preview_inflight_token)
                # Drop stale results if user changed parameters while preview was running.
                if token != int(self._preview_request_token):
                    self._finish_preview_task_state()
                    return
                try:
                    preview = deserialize_layer(dict(msg.get("result", {}).get("generated_layer", {})))
                except Exception:
                    LOGGER.exception("Cutout preview decode failed")
                    self._finish_preview_task_state()
                    return
                preview.metadata["preview"] = "true"
                preview.color = "#FFD166"
                self._cutout_preview_layer = preview
                self._apply_cutout_preview_layer()
                self._finish_preview_task_state()
                self._update_cutout_preview_state_hint()
                return

        if self._preview_task_process is not None and not self._preview_task_process.is_alive():
            self._finish_preview_task_state()
            self._update_cutout_preview_state_hint()

    def _finish_preview_task_state(self) -> None:
        self._preview_task_poll_timer.stop()
        if self._preview_task_process is not None:
            try:
                if self._preview_task_process.is_alive():
                    self._preview_task_process.terminate()
                self._preview_task_process.join(timeout=0.5)
            except Exception:
                pass
        if self._preview_task_queue is not None:
            try:
                self._preview_task_queue.close()
            except Exception:
                pass
        self._preview_task_process = None
        self._preview_task_queue = None
        self._update_cutout_preview_state_hint()

    def _cancel_cutout_preview_task(self, *, clear_pending: bool, clear_cache: bool) -> None:
        self._preview_debounce_timer.stop()
        self._finish_preview_task_state()
        if clear_pending:
            self._preview_pending_payload = None
        if clear_cache:
            self._clear_cutout_preview()
        self._update_cutout_preview_state_hint()

    def _has_generated_cutout_toolpath(
        self,
        *,
        source_layer_index: int | None = None,
        source_layer_indices: list[int] | None = None,
    ) -> bool:
        source_names: set[str] = set()
        indices = list(source_layer_indices or [])
        if not indices and source_layer_index is not None:
            indices = [int(source_layer_index)]
        for idx in indices:
            if 0 <= int(idx) < len(self.project.layers):
                source_names.add(str(self.project.layers[int(idx)].name))

        for layer in self.project.layers:
            meta = getattr(layer, "metadata", {}) or {}
            kind = str(meta.get("kind", "")).strip().lower()
            if kind != "cutout_toolpath":
                continue
            if str(meta.get("preview", "")).strip().lower() in {"1", "true", "yes", "on"}:
                continue
            if not source_names:
                return True
            derived_names = {
                part.strip()
                for part in str(meta.get("derived_from", "")).split(",")
                if part.strip()
            }
            if not derived_names or bool(derived_names & source_names):
                return True
        return False

    def _apply_cutout_preview_layer(self) -> None:
        if self._cutout_preview_layer is None:
            self._remove_cutout_preview_item()
            return
        if self.cutout_dock is not None and not self.cutout_dock.isVisible():
            self._remove_cutout_preview_item()
            return
        if self._cutout_preview_locked_until_reopen:
            self._clear_cutout_preview()
            return
        if self._has_generated_cutout_toolpath(source_layer_indices=self._active_cutout_source_indices()):
            self._cutout_preview_locked_until_reopen = True
            self._clear_cutout_preview()
            return
        self._remove_cutout_preview_item()
        try:
            self._cutout_preview_item = self.canvas.add_layer(self._cutout_preview_layer, layer_index=-1)
        except Exception:
            self._cutout_preview_item = None
            return
        self._highlight_selected_loop()

    def _highlight_selected_loop(self) -> None:
        if self._cutout_preview_item is None:
            return
        row = self.cutout_loop_table.currentRow()
        if row < 0 or row >= len(self._cutout_plan_loops):
            return
        # Keep highlight simple: ensure selected row is visible and show status hint.
        self.statusBar().showMessage(f"Selected cutout loop {row + 1}", 1000)

    def _remove_cutout_preview_item(self) -> None:
        if self._cutout_preview_item is not None:
            try:
                if hasattr(self.canvas, "remove_layer"):
                    self.canvas.remove_layer(self._cutout_preview_item)
                else:
                    self.canvas.scene().removeItem(self._cutout_preview_item)
            except Exception:
                pass
        self._cutout_preview_item = None

    def _clear_cutout_preview(self) -> None:
        self._remove_cutout_preview_item()
        self._cutout_preview_layer = None
        self._update_cutout_preview_state_hint()

    def _run_process_task(self, *, title: str, task_name: str, payload, decode_result, on_success, failure_title: str) -> None:
        if self._task_running:
            QMessageBox.information(self, "Busy", "Wait for the current task to finish.")
            return
        # Main generation pipeline:
        # - worker process computes geometry
        # - queue streams logs/errors/results
        # - UI decodes result and mutates project on main thread only
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
        if self._preview_pending_payload is not None:
            self._preview_debounce_timer.start(0)

    def _set_busy_controls(self, busy: bool) -> None:
        enabled = not busy
        for action in (
            self.open_project_action,
            self.save_project_action,
            self.save_project_as_action,
            self.export_hpgl_action,
            self.open_gerber_action,
            self.open_excellon_action,
            self.open_folder_action,
            self.clear_action,
            self.generate_isolation_action,
            self.generate_hatching_toolpath_action,
            self.generate_cutout_toolpath_action,
            self.generate_drill_toolpath_action,
            self.generate_centering_holes_action,
            self.generate_surfacing_toolpath_action,
            self.mirror_layer_action,
            self.mirror_bottom_centering_action,
            self.reassign_layer_roles_action,
            self.tool_library_action,
            self.selected_tools_action,
            self.renderer_qt_action,
            self.renderer_pyqtgraph_action,
            self.debug_geometry_dump_action,
        ):
            action.setEnabled(enabled)
        self.layer_tree.setEnabled(enabled)
        if self.layers_activity_layers_btn is not None:
            self.layers_activity_layers_btn.setEnabled(enabled)
        if self.layers_activity_metadata_btn is not None:
            self.layers_activity_metadata_btn.setEnabled(enabled)
        if self.layers_activity_cutout_btn is not None:
            self.layers_activity_cutout_btn.setEnabled(enabled)
        if self.mirror_layer_button is not None:
            self.mirror_layer_button.setEnabled(enabled and bool(self._loaded_layer_indices()))
        if self.reassign_layers_button is not None:
            self.reassign_layers_button.setEnabled(enabled and bool(self._loaded_layer_indices()))
        self.cutout_apply_button.setEnabled(enabled)
        self.cutout_close_button.setEnabled(enabled)
        self._sync_mirror_layer_controls()

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
        try:
            px_per_mm = max(abs(self.canvas.transform().m11()), 1e-9)
        except Exception:
            px_per_mm = 1.0
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
        self._cancel_cutout_preview_task(clear_pending=True, clear_cache=False)
        self._finish_task_state()
        super().closeEvent(event)
