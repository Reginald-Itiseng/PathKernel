from __future__ import annotations


from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.cnc_params import calculate_coppercam_params
from app.core.project import ToolDefinition
from app.ui.icons import icon_for


class SelectedToolsDialog(QDialog):
    def __init__(
        self,
        tools: list[ToolDefinition],
        assignments: dict[str, object] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Selected Tools")
        self.resize(1040, 680)
        self._tools = [t for t in tools if bool(t.defined)]
        self._tools_by_slot = {int(t.slot): t for t in self._tools}
        self._assignments = self._normalize_assignments(assignments)
        self._drill_series_rows: list[tuple[QWidget, QComboBox, QPushButton]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        content = QHBoxLayout()
        content.setSpacing(10)
        root.addLayout(content, stretch=1)

        left_col = QWidget(self)
        left_layout = QVBoxLayout(left_col)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        content.addWidget(left_col, stretch=2)

        (
            self.engraving_combo,
            self.engraving_depth_spin,
            self.engraving_margin_spin,
            self.engraving_speed_spin,
            self.engraving_depth_radius_label,
            self.engraving_margin_distance_label,
        ) = self._add_process_group(
            left_layout,
            "Engraving tool",
            with_process_params=True,
            depth_label="Engraving depth",
            speed_label="Engraving speed",
            show_depth_radius=True,
            show_margin_distance=True,
        )
        (
            self.hatching_combo,
            self.hatching_depth_spin,
            self.hatching_margin_spin,
            self.hatching_speed_spin,
            self.hatching_depth_radius_label,
            _,
        ) = self._add_process_group(
            left_layout,
            "Hatching tool",
            with_process_params=True,
            depth_label="Hatching depth",
            speed_label="Hatching speed",
            show_depth_radius=True,
        )
        (
            self.cutting_combo,
            self.cutting_depth_spin,
            _,
            self.cutting_speed_spin,
            _,
            _,
        ) = self._add_process_group(
            left_layout,
            "Cutting tool",
            with_process_params=True,
            depth_label="Cutting depth",
            speed_label="Cutting speed",
            show_margin=False,
            show_speed=True,
        )
        (
            self.centering_combo,
            self.centering_hole_diameter_spin,
            self.centering_extra_depth_spin,
            _,
            _,
            _,
        ) = self._add_process_group(
            left_layout,
            "Centering tool",
            with_process_params=True,
            depth_label="Hole diameter",
            speed_label="",
            margin_label="Extra depth",
            show_margin=True,
            show_speed=False,
        )
        left_layout.addStretch(1)

        drill_group = QGroupBox("Drilling tools", self)
        drill_layout = QVBoxLayout(drill_group)
        drill_layout.setContentsMargins(10, 10, 10, 10)
        drill_layout.setSpacing(8)
        content.addWidget(drill_group, stretch=3)

        section_one = QGroupBox("Strategy A", drill_group)
        section_one_layout = QVBoxLayout(section_one)
        section_one_layout.setContentsMargins(10, 10, 10, 10)
        section_one_layout.setSpacing(6)
        self.drill_single_boring_check = QCheckBox(
            "Use one single tool for all drills, with circular boring",
            section_one,
        )
        self.drill_single_tool_combo = QComboBox(section_one)
        section_one_layout.addWidget(self.drill_single_boring_check)
        section_one_layout.addWidget(self.drill_single_tool_combo)
        drill_layout.addWidget(section_one)

        section_two = QGroupBox("Strategy B", drill_group)
        section_two_layout = QVBoxLayout(section_two)
        section_two_layout.setContentsMargins(10, 10, 10, 10)
        section_two_layout.setSpacing(6)
        self.drill_closest_smaller_check = QCheckBox(
            "Use for each drill the closest smaller tool, with circular boring",
            section_two,
        )
        self.drill_closest_greater_check = QCheckBox(
            "Use for each drill the closest greater tool, without circular boring",
            section_two,
        )
        section_two_layout.addWidget(self.drill_closest_smaller_check)
        section_two_layout.addWidget(self.drill_closest_greater_check)

        self.drill_series_container = QWidget(section_two)
        self.drill_series_layout = QVBoxLayout(self.drill_series_container)
        self.drill_series_layout.setContentsMargins(0, 0, 0, 0)
        self.drill_series_layout.setSpacing(4)
        section_two_layout.addWidget(self.drill_series_container)

        series_actions = QHBoxLayout()
        self.drill_add_series_button = QPushButton("Add tool", section_two)
        add_icon = icon_for("drill_add_tool", size=16)
        if not add_icon.isNull():
            self.drill_add_series_button.setIcon(add_icon)
            self.drill_add_series_button.setIconSize(QSize(14, 14))
        self.drill_add_series_button.clicked.connect(lambda: self._add_drill_series_row(None))
        series_actions.addWidget(self.drill_add_series_button)
        series_actions.addStretch(1)
        section_two_layout.addLayout(series_actions)
        drill_layout.addWidget(section_two)

        section_three = QGroupBox("Drilling process parameters", drill_group)
        section_three_layout = QFormLayout(section_three)
        section_three_layout.setContentsMargins(10, 10, 10, 10)
        section_three_layout.setSpacing(6)
        self.drill_boring_cycle_mode_combo = QComboBox(section_three)
        self.drill_boring_cycle_mode_combo.addItem("Drill at center", "drill_at_center")
        self.drill_boring_cycle_mode_combo.addItem("Lift-up at center", "lift_up_at_center")
        section_three_layout.addRow("In case of boring cycle", self.drill_boring_cycle_mode_combo)

        self.drill_lateral_stepover_pct_spin = QDoubleSpinBox(section_three)
        self.drill_lateral_stepover_pct_spin.setRange(1.0, 100.0)
        self.drill_lateral_stepover_pct_spin.setDecimals(1)
        self.drill_lateral_stepover_pct_spin.setSuffix(" % of tool radius")
        section_three_layout.addRow("Lateral boring passes", self.drill_lateral_stepover_pct_spin)

        self.drill_depth_spin = QDoubleSpinBox(section_three)
        self.drill_depth_spin.setRange(0.0, 1000.0)
        self.drill_depth_spin.setDecimals(4)
        self.drill_depth_spin.setSuffix(" mm")
        section_three_layout.addRow("Drilling depth", self.drill_depth_spin)

        self.drill_boring_speed_spin = QDoubleSpinBox(section_three)
        self.drill_boring_speed_spin.setRange(0.0, 100000.0)
        self.drill_boring_speed_spin.setDecimals(3)
        self.drill_boring_speed_spin.setSuffix(" mm/s")
        section_three_layout.addRow("Boring speed", self.drill_boring_speed_spin)
        drill_layout.addWidget(section_three)
        drill_layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.drill_single_boring_check.toggled.connect(self._update_drill_mode_state)
        self.drill_closest_smaller_check.toggled.connect(self._update_drill_mode_state)
        self.drill_closest_greater_check.toggled.connect(self._update_drill_mode_state)
        self.drill_single_boring_check.toggled.connect(
            lambda checked: self._on_drill_strategy_toggled("single", checked)
        )
        self.drill_closest_smaller_check.toggled.connect(
            lambda checked: self._on_drill_strategy_toggled("closest_smaller", checked)
        )
        self.drill_closest_greater_check.toggled.connect(
            lambda checked: self._on_drill_strategy_toggled("closest_greater", checked)
        )

        self._populate_tool_selectors()
        self._apply_initial_assignments()
        self._connect_geometry_feedback()

    def selected_assignments(self) -> dict[str, object]:
        drill_series = self._drill_series_slots()
        return {
            "engraving": self._combo_value(self.engraving_combo),
            "hatching": self._combo_value(self.hatching_combo),
            "cutting": self._combo_value(self.cutting_combo),
            "centering": self._combo_value(self.centering_combo),
            "engraving_depth_mm": float(self.engraving_depth_spin.value()),
            "engraving_margin_mm": float(self.engraving_margin_spin.value()),
            "engraving_speed_mm_s": float(self.engraving_speed_spin.value()),
            "hatching_depth_mm": float(self.hatching_depth_spin.value()),
            "hatching_margin_mm": float(self.hatching_margin_spin.value()),
            "hatching_speed_mm_s": float(self.hatching_speed_spin.value()),
            "cutting_depth_mm": float(self.cutting_depth_spin.value()),
            "cutting_speed_mm_s": float(self.cutting_speed_spin.value()),
            "centering_hole_diameter_mm": float(self.centering_hole_diameter_spin.value()),
            "centering_extra_depth_mm": float(self.centering_extra_depth_spin.value()),
            "drill_use_single_tool_boring": bool(self.drill_single_boring_check.isChecked()),
            "drill_single_tool_slot": self._combo_value(self.drill_single_tool_combo),
            "drill_use_closest_smaller_boring": bool(self.drill_closest_smaller_check.isChecked()),
            "drill_use_closest_greater_no_boring": bool(self.drill_closest_greater_check.isChecked()),
            "drill_series_tool_slots": drill_series,
            "drill_boring_cycle_mode": str(self.drill_boring_cycle_mode_combo.currentData()),
            "drill_lateral_stepover_pct": float(self.drill_lateral_stepover_pct_spin.value()),
            "drill_depth_mm": float(self.drill_depth_spin.value()),
            "drill_boring_speed_mm_s": float(self.drill_boring_speed_spin.value()),
            # Legacy key kept for backward compatibility with previous dialog versions.
            "drilling": drill_series,
        }

    def _add_process_group(
        self,
        parent_layout: QVBoxLayout,
        title: str,
        *,
        with_process_params: bool = False,
        depth_label: str = "Depth",
        speed_label: str = "Speed",
        margin_label: str = "Margin",
        show_margin: bool = True,
        show_speed: bool = True,
        show_depth_radius: bool = False,
        show_margin_distance: bool = False,
    ) -> tuple[
        QComboBox,
        QDoubleSpinBox | None,
        QDoubleSpinBox | None,
        QDoubleSpinBox | None,
        QLabel | None,
        QLabel | None,
    ]:
        box = QGroupBox(title, self)
        layout = QFormLayout(box)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        combo = QComboBox(box)
        layout.addRow("Tool", combo)
        depth_spin: QDoubleSpinBox | None = None
        margin_spin: QDoubleSpinBox | None = None
        speed_spin: QDoubleSpinBox | None = None
        depth_radius_label: QLabel | None = None
        margin_distance_label: QLabel | None = None
        if with_process_params:
            depth_spin = QDoubleSpinBox(box)
            depth_spin.setRange(0.0, 1000.0)
            depth_spin.setDecimals(4)
            depth_spin.setSuffix(" mm")
            if show_depth_radius:
                depth_radius_label = QLabel("radius: 0.0000 mm", box)
                depth_radius_label.setStyleSheet("color: #7f8c9a;")
                depth_row = QWidget(box)
                depth_row_layout = QHBoxLayout(depth_row)
                depth_row_layout.setContentsMargins(0, 0, 0, 0)
                depth_row_layout.setSpacing(6)
                depth_row_layout.addWidget(depth_spin, stretch=1)
                depth_row_layout.addWidget(depth_radius_label)
                layout.addRow(depth_label, depth_row)
            else:
                layout.addRow(depth_label, depth_spin)

            if show_margin:
                margin_spin = QDoubleSpinBox(box)
                margin_spin.setRange(0.0, 1000.0)
                margin_spin.setDecimals(4)
                margin_spin.setSuffix(" mm")
                if show_margin_distance:
                    margin_distance_label = QLabel("distance: 0.0000 mm", box)
                    margin_distance_label.setStyleSheet("color: #7f8c9a;")
                    margin_row = QWidget(box)
                    margin_row_layout = QHBoxLayout(margin_row)
                    margin_row_layout.setContentsMargins(0, 0, 0, 0)
                    margin_row_layout.setSpacing(6)
                    margin_row_layout.addWidget(margin_spin, stretch=1)
                    margin_row_layout.addWidget(margin_distance_label)
                    layout.addRow(margin_label, margin_row)
                else:
                    layout.addRow(margin_label, margin_spin)

            if show_speed:
                speed_spin = QDoubleSpinBox(box)
                speed_spin.setRange(0.0, 100000.0)
                speed_spin.setDecimals(3)
                speed_spin.setSuffix(" mm/s")
                layout.addRow(speed_label, speed_spin)
        parent_layout.addWidget(box)
        return combo, depth_spin, margin_spin, speed_spin, depth_radius_label, margin_distance_label

    def _populate_tool_selectors(self) -> None:
        for combo in (
            self.engraving_combo,
            self.hatching_combo,
            self.cutting_combo,
            self.centering_combo,
            self.drill_single_tool_combo,
        ):
            self._fill_tool_combo(combo)

    def _fill_tool_combo(self, combo: QComboBox) -> None:
        combo.clear()
        combo.addItem("None", None)
        for tool in self._tools:
            combo.addItem(self._tool_label(tool), int(tool.slot))
        combo.setEnabled(combo.count() > 1)

    def _apply_initial_assignments(self) -> None:
        self._set_combo_slot(self.engraving_combo, self._assignments["engraving"])
        self._set_combo_slot(self.hatching_combo, self._assignments["hatching"])
        self._set_combo_slot(self.cutting_combo, self._assignments["cutting"])
        self._set_combo_slot(self.centering_combo, self._assignments["centering"])
        self._set_combo_slot(self.drill_single_tool_combo, self._assignments["drill_single_tool_slot"])

        self.engraving_depth_spin.setValue(float(self._assignments["engraving_depth_mm"]))
        self.engraving_margin_spin.setValue(float(self._assignments["engraving_margin_mm"]))
        self.engraving_speed_spin.setValue(float(self._assignments["engraving_speed_mm_s"]))
        self.hatching_depth_spin.setValue(float(self._assignments["hatching_depth_mm"]))
        self.hatching_margin_spin.setValue(float(self._assignments["hatching_margin_mm"]))
        self.hatching_speed_spin.setValue(float(self._assignments["hatching_speed_mm_s"]))
        self.cutting_depth_spin.setValue(float(self._assignments["cutting_depth_mm"]))
        self.cutting_speed_spin.setValue(float(self._assignments["cutting_speed_mm_s"]))
        self.centering_hole_diameter_spin.setValue(float(self._assignments["centering_hole_diameter_mm"]))
        self.centering_extra_depth_spin.setValue(float(self._assignments["centering_extra_depth_mm"]))

        self.drill_single_boring_check.setChecked(bool(self._assignments["drill_use_single_tool_boring"]))
        self.drill_closest_smaller_check.setChecked(bool(self._assignments["drill_use_closest_smaller_boring"]))
        self.drill_closest_greater_check.setChecked(bool(self._assignments["drill_use_closest_greater_no_boring"]))
        self._coerce_single_drill_strategy()
        self._set_combo_data(self.drill_boring_cycle_mode_combo, self._assignments["drill_boring_cycle_mode"])
        self.drill_lateral_stepover_pct_spin.setValue(float(self._assignments["drill_lateral_stepover_pct"]))
        self.drill_depth_spin.setValue(float(self._assignments["drill_depth_mm"]))
        self.drill_boring_speed_spin.setValue(float(self._assignments["drill_boring_speed_mm_s"]))

        self._clear_drill_series_rows()
        slots = list(self._assignments["drill_series_tool_slots"])
        if not slots:
            self._add_drill_series_row(None)
        else:
            for slot in slots:
                self._add_drill_series_row(slot)
        self._update_drill_mode_state()
        self._update_engraving_geometry_feedback()
        self._update_hatching_geometry_feedback()

    def _connect_geometry_feedback(self) -> None:
        self.engraving_combo.currentIndexChanged.connect(self._update_engraving_geometry_feedback)
        self.engraving_depth_spin.valueChanged.connect(self._update_engraving_geometry_feedback)
        self.engraving_margin_spin.valueChanged.connect(self._update_engraving_geometry_feedback)
        self.hatching_combo.currentIndexChanged.connect(self._update_hatching_geometry_feedback)
        self.hatching_depth_spin.valueChanged.connect(self._update_hatching_geometry_feedback)

    def _update_engraving_geometry_feedback(self) -> None:
        radius = self._effective_radius_from_combo(self.engraving_combo, self.engraving_depth_spin.value())
        if self.engraving_depth_radius_label is not None:
            self.engraving_depth_radius_label.setText(f"radius: {radius:.4f} mm")
        if self.engraving_margin_distance_label is not None:
            distance = radius + float(self.engraving_margin_spin.value())
            self.engraving_margin_distance_label.setText(f"distance: {distance:.4f} mm")

    def _update_hatching_geometry_feedback(self) -> None:
        radius = self._effective_radius_from_combo(self.hatching_combo, self.hatching_depth_spin.value())
        if self.hatching_depth_radius_label is not None:
            self.hatching_depth_radius_label.setText(f"radius: {radius:.4f} mm")

    def _effective_radius_from_combo(self, combo: QComboBox, depth_mm: float) -> float:
        slot = self._combo_value(combo)
        if slot is None:
            return 0.0
        tool = self._tools_by_slot.get(int(slot))
        if tool is None:
            return 0.0
        profile = str(tool.profile).strip().lower()
        if profile == "conical":
            tip_dia = float(tool.tip_diameter_mm) if float(tool.tip_diameter_mm) > 0.0 else float(tool.diameter_mm)
            params = calculate_coppercam_params(
                T_dia=tip_dia,
                T_angle=float(tool.angle_deg),
                D_cut=max(0.0, float(depth_mm)),
            )
            return float(params["effective_radius"])
        return max(0.0, float(tool.diameter_mm) / 2.0)

    def _add_drill_series_row(self, slot: int | None) -> None:
        row = QWidget(self.drill_series_container)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)

        combo = QComboBox(row)
        self._fill_tool_combo(combo)
        self._set_combo_slot(combo, slot)

        remove_btn = QPushButton("x", row)
        remove_btn.setFixedWidth(24)
        remove_btn.setToolTip("Remove this tool")
        remove_icon = icon_for("drill_remove_tool", size=16)
        if not remove_icon.isNull():
            remove_btn.setIcon(remove_icon)
            remove_btn.setIconSize(QSize(12, 12))
            remove_btn.setText("")
        remove_btn.clicked.connect(lambda: self._remove_drill_series_row(row))

        row_layout.addWidget(combo, stretch=1)
        row_layout.addWidget(remove_btn)
        self.drill_series_layout.addWidget(row)
        self._drill_series_rows.append((row, combo, remove_btn))

    def _remove_drill_series_row(self, row_widget: QWidget) -> None:
        idx = -1
        for i, (row, _, _) in enumerate(self._drill_series_rows):
            if row is row_widget:
                idx = i
                break
        if idx < 0:
            return
        row, _, _ = self._drill_series_rows.pop(idx)
        row.setParent(None)
        row.deleteLater()

    def _clear_drill_series_rows(self) -> None:
        for row, _, _ in self._drill_series_rows:
            row.setParent(None)
            row.deleteLater()
        self._drill_series_rows.clear()

    def _update_drill_mode_state(self) -> None:
        self.drill_single_tool_combo.setEnabled(
            bool(self.drill_single_boring_check.isChecked()) and self.drill_single_tool_combo.count() > 1
        )
        series_enabled = bool(self.drill_closest_smaller_check.isChecked() or self.drill_closest_greater_check.isChecked())
        self.drill_add_series_button.setEnabled(series_enabled)
        for row, _, _ in self._drill_series_rows:
            row.setEnabled(series_enabled)
        boring_active = bool(self.drill_single_boring_check.isChecked() or self.drill_closest_smaller_check.isChecked())
        self.drill_boring_cycle_mode_combo.setEnabled(boring_active)
        self.drill_lateral_stepover_pct_spin.setEnabled(boring_active)
        self.drill_boring_speed_spin.setEnabled(boring_active)
        self.drill_depth_spin.setEnabled(True)

    def _on_drill_strategy_toggled(self, source: str, checked: bool) -> None:
        if not checked:
            return
        # Only one drilling strategy can be active at a time.
        if source != "single":
            self._set_checked_no_signal(self.drill_single_boring_check, False)
        if source != "closest_smaller":
            self._set_checked_no_signal(self.drill_closest_smaller_check, False)
        if source != "closest_greater":
            self._set_checked_no_signal(self.drill_closest_greater_check, False)
        self._update_drill_mode_state()

    def _coerce_single_drill_strategy(self) -> None:
        checks = [
            ("single", self.drill_single_boring_check.isChecked()),
            ("closest_smaller", self.drill_closest_smaller_check.isChecked()),
            ("closest_greater", self.drill_closest_greater_check.isChecked()),
        ]
        chosen = [name for name, is_on in checks if is_on]
        if len(chosen) <= 1:
            return
        keep = chosen[0]
        self._set_checked_no_signal(self.drill_single_boring_check, keep == "single")
        self._set_checked_no_signal(self.drill_closest_smaller_check, keep == "closest_smaller")
        self._set_checked_no_signal(self.drill_closest_greater_check, keep == "closest_greater")

    @staticmethod
    def _set_checked_no_signal(widget: QCheckBox, checked: bool) -> None:
        old = widget.blockSignals(True)
        try:
            widget.setChecked(bool(checked))
        finally:
            widget.blockSignals(old)

    def _set_combo_slot(self, combo: QComboBox, slot: int | None) -> None:
        for i in range(combo.count()):
            data = combo.itemData(i)
            if data is None and slot is None:
                combo.setCurrentIndex(i)
                return
            if isinstance(data, int) and isinstance(slot, int) and data == slot:
                combo.setCurrentIndex(i)
                return
        combo.setCurrentIndex(0)

    def _set_combo_data(self, combo: QComboBox, value: str) -> None:
        for i in range(combo.count()):
            data = combo.itemData(i)
            if isinstance(data, str) and data == value:
                combo.setCurrentIndex(i)
                return
        combo.setCurrentIndex(0)

    def _combo_value(self, combo: QComboBox) -> int | None:
        data = combo.currentData()
        if isinstance(data, int):
            return data
        return None

    def _drill_series_slots(self) -> list[int]:
        out: list[int] = []
        seen: set[int] = set()
        for _, combo, _ in self._drill_series_rows:
            slot = self._combo_value(combo)
            if slot is None or slot in seen:
                continue
            seen.add(slot)
            out.append(slot)
        return out

    def _normalize_assignments(self, assignments: dict[str, object] | None) -> dict[str, object]:
        base: dict[str, object] = {
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
            "drill_single_tool_slot": None,
            "drill_use_closest_smaller_boring": False,
            "drill_use_closest_greater_no_boring": False,
            "drill_series_tool_slots": [],
            "drill_boring_cycle_mode": "drill_at_center",
            "drill_lateral_stepover_pct": 50.0,
            "drill_depth_mm": 0.0,
            "drill_boring_speed_mm_s": 0.0,
        }
        if not isinstance(assignments, dict):
            return base

        for key in ("engraving", "hatching", "cutting", "centering"):
            value = assignments.get(key)
            base[key] = int(value) if isinstance(value, int) else None

        base["engraving_depth_mm"] = self._as_float(assignments.get("engraving_depth_mm"), 0.0)
        base["engraving_margin_mm"] = self._as_float(assignments.get("engraving_margin_mm"), 0.0)
        if "engraving_speed_mm_s" in assignments:
            base["engraving_speed_mm_s"] = self._as_float(assignments.get("engraving_speed_mm_s"), 0.0)
        else:
            base["engraving_speed_mm_s"] = self._as_float(assignments.get("engraving_speed_mm_min"), 0.0) / 60.0

        base["hatching_depth_mm"] = self._as_float(assignments.get("hatching_depth_mm"), 0.0)
        base["hatching_margin_mm"] = self._as_float(assignments.get("hatching_margin_mm"), 0.0)
        if "hatching_speed_mm_s" in assignments:
            base["hatching_speed_mm_s"] = self._as_float(assignments.get("hatching_speed_mm_s"), 0.0)
        else:
            base["hatching_speed_mm_s"] = self._as_float(assignments.get("hatching_speed_mm_min"), 0.0) / 60.0

        base["cutting_depth_mm"] = self._as_float(assignments.get("cutting_depth_mm"), 0.0)
        if "cutting_speed_mm_s" in assignments:
            base["cutting_speed_mm_s"] = self._as_float(assignments.get("cutting_speed_mm_s"), 0.0)
        else:
            base["cutting_speed_mm_s"] = self._as_float(assignments.get("cutting_speed_mm_min"), 0.0) / 60.0
        base["centering_hole_diameter_mm"] = self._as_float(assignments.get("centering_hole_diameter_mm"), 0.0)
        base["centering_extra_depth_mm"] = self._as_float(assignments.get("centering_extra_depth_mm"), 0.0)

        base["drill_use_single_tool_boring"] = self._as_bool(assignments.get("drill_use_single_tool_boring"), False)
        single_slot = assignments.get("drill_single_tool_slot")
        base["drill_single_tool_slot"] = int(single_slot) if isinstance(single_slot, int) else None
        base["drill_use_closest_smaller_boring"] = self._as_bool(
            assignments.get("drill_use_closest_smaller_boring"), False
        )
        base["drill_use_closest_greater_no_boring"] = self._as_bool(
            assignments.get("drill_use_closest_greater_no_boring"), False
        )

        series = assignments.get("drill_series_tool_slots")
        if isinstance(series, list):
            base["drill_series_tool_slots"] = [int(v) for v in series if isinstance(v, int)]
        else:
            legacy = assignments.get("drilling")
            if isinstance(legacy, list):
                base["drill_series_tool_slots"] = [int(v) for v in legacy if isinstance(v, int)]

        cycle_mode = str(assignments.get("drill_boring_cycle_mode", "drill_at_center")).strip().lower()
        if cycle_mode not in {"drill_at_center", "lift_up_at_center"}:
            cycle_mode = "drill_at_center"
        base["drill_boring_cycle_mode"] = cycle_mode
        base["drill_lateral_stepover_pct"] = self._as_float(assignments.get("drill_lateral_stepover_pct"), 50.0)
        base["drill_depth_mm"] = self._as_float(assignments.get("drill_depth_mm"), 0.0)
        if "drill_boring_speed_mm_s" in assignments:
            base["drill_boring_speed_mm_s"] = self._as_float(assignments.get("drill_boring_speed_mm_s"), 0.0)
        else:
            base["drill_boring_speed_mm_s"] = self._as_float(assignments.get("drill_boring_speed_mm_min"), 0.0) / 60.0

        return base

    @staticmethod
    def _as_float(value, default: float = 0.0) -> float:  # noqa: ANN001
        try:
            return float(value)
        except Exception:
            return float(default)

    @staticmethod
    def _as_bool(value, default: bool = False) -> bool:  # noqa: ANN001
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        if isinstance(value, (int, float)):
            return bool(value)
        return bool(default)

    @staticmethod
    def _tool_label(tool: ToolDefinition) -> str:
        number = str(tool.tool_number).strip() or str(tool.slot)
        name = str(tool.name).strip() or "Unnamed"
        core = f"{number}> {name}, {float(tool.diameter_mm):.4f} mm, {tool.profile}"
        if str(tool.profile).strip().lower() == "conical":
            core += f", {float(tool.angle_deg):.2f} deg"
        return core
