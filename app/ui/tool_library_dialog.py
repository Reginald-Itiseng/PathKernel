from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.core.project import ToolDefinition, default_tool_library
from app.ui.icons import icon_for


class ToolLibraryDialog(QDialog):
    def __init__(self, tools: list[ToolDefinition], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Tool Library")
        self.resize(980, 560)
        self._tools = self._normalize_tools(tools)
        self._current_row = -1

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        content = QHBoxLayout()
        content.setSpacing(10)
        root.addLayout(content, stretch=1)

        self.tool_list = QListWidget(self)
        self.tool_list.setAlternatingRowColors(True)
        self.tool_list.setMinimumWidth(360)
        content.addWidget(self.tool_list, stretch=2)

        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)
        content.addWidget(right, stretch=3)

        ident_group = QGroupBox("Identification", right)
        ident_form = QFormLayout(ident_group)
        self.tool_number_edit = QLineEdit(ident_group)
        self.tool_name_edit = QLineEdit(ident_group)
        ident_form.addRow("Tool number", self.tool_number_edit)
        ident_form.addRow("Tool name", self.tool_name_edit)
        right_layout.addWidget(ident_group)

        spec_group = QGroupBox("Specifications", right)
        spec_form = QFormLayout(spec_group)
        self.diameter_spin = QDoubleSpinBox(spec_group)
        self.diameter_spin.setRange(0.0, 1000.0)
        self.diameter_spin.setDecimals(4)
        self.diameter_spin.setSuffix(" mm")

        self.profile_combo = QComboBox(spec_group)
        self.profile_combo.addItems(["cylindrical/flute", "conical"])

        self.angle_spin = QDoubleSpinBox(spec_group)
        self.angle_spin.setRange(0.0, 180.0)
        self.angle_spin.setDecimals(2)
        self.angle_spin.setSuffix(" deg")

        self.tip_diameter_spin = QDoubleSpinBox(spec_group)
        self.tip_diameter_spin.setRange(0.0, 1000.0)
        self.tip_diameter_spin.setDecimals(4)
        self.tip_diameter_spin.setSuffix(" mm")

        self.max_rpm_spin = QSpinBox(spec_group)
        self.max_rpm_spin.setRange(0, 1_000_000)

        self.plunge_spin = QDoubleSpinBox(spec_group)
        self.plunge_spin.setRange(0.0, 100_000.0)
        self.plunge_spin.setDecimals(2)
        self.plunge_spin.setSuffix(" mm/s")

        self.max_depth_spin = QDoubleSpinBox(spec_group)
        self.max_depth_spin.setRange(0.0, 1000.0)
        self.max_depth_spin.setDecimals(4)
        self.max_depth_spin.setSuffix(" mm")

        spec_form.addRow("Tool diameter", self.diameter_spin)
        spec_form.addRow("Profile", self.profile_combo)
        spec_form.addRow("Tool angle", self.angle_spin)
        spec_form.addRow("Tip diameter", self.tip_diameter_spin)
        spec_form.addRow("Max RPM", self.max_rpm_spin)
        spec_form.addRow("Plunge speed", self.plunge_spin)
        spec_form.addRow("Max depth / pass", self.max_depth_spin)
        right_layout.addWidget(spec_group)

        help_label = QLabel(
            "Select a slot, edit values, then click Save Tool.\n"
            "Conical profile enables angle and tip diameter.",
            right,
        )
        help_label.setStyleSheet("color: #7f8c9a;")
        right_layout.addWidget(help_label)
        right_layout.addStretch(1)

        actions = QHBoxLayout()
        root.addLayout(actions)
        self.save_button = QPushButton("Save Tool", self)
        self.undefined_button = QPushButton("Mark Undefined", self)
        self.done_button = QPushButton("Done", self)
        self.done_button.setDefault(True)
        save_icon = icon_for("tool_save", size=16)
        if not save_icon.isNull():
            self.save_button.setIcon(save_icon)
            self.save_button.setIconSize(QSize(16, 16))
        undefined_icon = icon_for("tool_mark_undefined", size=16)
        if not undefined_icon.isNull():
            self.undefined_button.setIcon(undefined_icon)
            self.undefined_button.setIconSize(QSize(16, 16))
        done_icon = icon_for("tool_done", size=16)
        if not done_icon.isNull():
            self.done_button.setIcon(done_icon)
            self.done_button.setIconSize(QSize(16, 16))
        actions.addWidget(self.save_button)
        actions.addWidget(self.undefined_button)
        actions.addStretch(1)
        actions.addWidget(self.done_button)

        self.profile_combo.currentTextChanged.connect(self._update_profile_fields)
        self.tool_list.currentRowChanged.connect(self._on_row_changed)
        self.save_button.clicked.connect(self._save_current_tool)
        self.undefined_button.clicked.connect(self._mark_current_undefined)
        self.done_button.clicked.connect(self._accept)

        self._populate_list()
        if self.tool_list.count() > 0:
            self.tool_list.setCurrentRow(0)
        self._update_profile_fields()

    def tool_library(self) -> list[ToolDefinition]:
        return [replace(tool) for tool in self._tools]

    def _normalize_tools(self, tools: list[ToolDefinition]) -> list[ToolDefinition]:
        defaults = default_tool_library()
        out: list[ToolDefinition] = []
        for i in range(50):
            slot = i + 1
            if i < len(tools) and isinstance(tools[i], ToolDefinition):
                src = tools[i]
                out.append(
                    ToolDefinition(
                        slot=slot,
                        defined=bool(src.defined),
                        tool_number=str(src.tool_number or slot),
                        name=str(src.name),
                        diameter_mm=float(src.diameter_mm),
                        profile=str(src.profile),
                        angle_deg=float(src.angle_deg),
                        tip_diameter_mm=float(src.tip_diameter_mm),
                        max_rpm=int(src.max_rpm),
                        plunge_mm_s=float(src.plunge_mm_s),
                        max_depth_per_pass_mm=float(src.max_depth_per_pass_mm),
                    )
                )
            else:
                out.append(replace(defaults[i]))
        return out

    def _populate_list(self) -> None:
        self.tool_list.clear()
        for i in range(50):
            self.tool_list.addItem(self._tool_row_text(i))

    def _tool_row_text(self, idx: int) -> str:
        tool = self._tools[idx]
        if not tool.defined:
            return f"{tool.slot}> undefined"
        number = tool.tool_number.strip() or str(tool.slot)
        name = tool.name.strip() or "Unnamed"
        text = f"{number}> {name}, {tool.diameter_mm:.4f} mm, {tool.profile}"
        if tool.profile == "conical":
            text += f", {tool.angle_deg:.2f} deg"
        return text

    def _on_row_changed(self, row: int) -> None:
        self._current_row = int(row)
        if row < 0 or row >= len(self._tools):
            return
        tool = self._tools[row]
        self.tool_number_edit.setText(tool.tool_number or str(tool.slot))
        self.tool_name_edit.setText(tool.name)
        self.diameter_spin.setValue(float(tool.diameter_mm))
        profile = str(tool.profile).strip().lower()
        if profile not in {"cylindrical/flute", "conical"}:
            profile = "cylindrical/flute"
        self.profile_combo.setCurrentText(profile)
        self.angle_spin.setValue(float(tool.angle_deg))
        self.tip_diameter_spin.setValue(float(tool.tip_diameter_mm))
        self.max_rpm_spin.setValue(int(tool.max_rpm))
        self.plunge_spin.setValue(float(tool.plunge_mm_s))
        self.max_depth_spin.setValue(float(tool.max_depth_per_pass_mm))
        self._update_profile_fields()

    def _update_profile_fields(self) -> None:
        is_conical = self.profile_combo.currentText() == "conical"
        self.angle_spin.setEnabled(is_conical)
        self.tip_diameter_spin.setEnabled(is_conical)

    def _save_current_tool(self) -> None:
        row = self._current_row
        if row < 0 or row >= len(self._tools):
            QMessageBox.information(self, "No Tool Selected", "Select a tool slot first.")
            return
        profile = self.profile_combo.currentText().strip().lower()
        if profile not in {"cylindrical/flute", "conical"}:
            profile = "cylindrical/flute"
        slot = row + 1
        tool_number = self.tool_number_edit.text().strip() or str(slot)
        tool = self._tools[row]
        tool.slot = slot
        tool.defined = True
        tool.tool_number = tool_number
        tool.name = self.tool_name_edit.text().strip()
        tool.diameter_mm = float(self.diameter_spin.value())
        tool.profile = profile
        if profile == "conical":
            tool.angle_deg = float(self.angle_spin.value())
            tool.tip_diameter_mm = float(self.tip_diameter_spin.value())
        else:
            tool.angle_deg = 0.0
            tool.tip_diameter_mm = 0.0
        tool.max_rpm = int(self.max_rpm_spin.value())
        tool.plunge_mm_s = float(self.plunge_spin.value())
        tool.max_depth_per_pass_mm = float(self.max_depth_spin.value())
        self.tool_list.item(row).setText(self._tool_row_text(row))

    def _mark_current_undefined(self) -> None:
        row = self._current_row
        if row < 0 or row >= len(self._tools):
            QMessageBox.information(self, "No Tool Selected", "Select a tool slot first.")
            return
        slot = row + 1
        self._tools[row] = ToolDefinition(slot=slot, defined=False, tool_number=str(slot))
        self.tool_list.item(row).setText(self._tool_row_text(row))
        self._on_row_changed(row)

    def _accept(self) -> None:
        self.accept()
