from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from app.core.project import ToolDefinition


class SurfacingToolpathDialog(QDialog):
    def __init__(
        self,
        *,
        default_bounds: tuple[float, float, float, float],
        tools: list[ToolDefinition],
        default_tool_slot: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Generate Surfacing Toolpath")
        self.resize(560, 380)
        self._tools_by_slot: dict[int, ToolDefinition] = {}

        min_x, min_y, max_x, max_y = default_bounds
        start_x = float(min_x)
        start_y = float(min_y)
        width = max(0.001, float(max_x) - float(min_x))
        height = max(0.001, float(max_y) - float(min_y))

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        intro = QLabel(
            (
                "Choose a tool and define the surfacing pattern area in workspace coordinates.\n"
                "Z depth is controlled on the machine (RoutePro runtime)."
            ),
            self,
        )
        intro.setWordWrap(True)
        intro.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        root.addWidget(intro)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)
        root.addLayout(form)

        self.tool_combo = QComboBox(self)
        self.tool_combo.addItem("Manual diameter", None)
        for tool in tools:
            if not bool(getattr(tool, "defined", False)):
                continue
            slot = int(tool.slot)
            self._tools_by_slot[slot] = tool
            number = str(getattr(tool, "tool_number", "") or "").strip() or str(slot)
            name = str(getattr(tool, "name", "") or "").strip() or "Unnamed"
            dia = max(0.001, float(getattr(tool, "diameter_mm", 0.0) or 0.0))
            self.tool_combo.addItem(f"{number}> {name}, {dia:.4f} mm", slot)
        self._set_tool_combo_default(default_tool_slot)
        form.addRow("Tool (library)", self.tool_combo)

        default_tool_dia = self._selected_tool_diameter_mm(default=1.0)
        self.tool_dia_spin = _mm_spin(self, value=default_tool_dia, minimum=0.001, maximum=1000.0)
        self.stepover_mm_spin = _mm_spin(self, value=0.0, minimum=0.0, maximum=1000.0)
        self.stepover_pct_spin = _float_spin(self, value=70.0, minimum=1.0, maximum=100.0, decimals=1, suffix=" %")
        self.margin_spin = _mm_spin(self, value=0.0, minimum=0.0, maximum=1000.0)
        form.addRow("Tool diameter", self.tool_dia_spin)

        self.start_x_spin = _mm_spin(self, value=start_x, minimum=-1_000_000.0, maximum=1_000_000.0)
        self.start_y_spin = _mm_spin(self, value=start_y, minimum=-1_000_000.0, maximum=1_000_000.0)
        self.width_spin = _mm_spin(self, value=width, minimum=0.001, maximum=1_000_000.0)
        self.height_spin = _mm_spin(self, value=height, minimum=0.001, maximum=1_000_000.0)
        form.addRow("Area start X", self.start_x_spin)
        form.addRow("Area start Y", self.start_y_spin)
        form.addRow("Area width", self.width_spin)
        form.addRow("Area height", self.height_spin)

        form.addRow("Stepover (mm, 0=auto)", self.stepover_mm_spin)
        form.addRow("Auto stepover (%)", self.stepover_pct_spin)
        form.addRow("Inset margin", self.margin_spin)

        self.sweep_axis_combo = QComboBox(self)
        self.sweep_axis_combo.addItem("Rows Along X (step in Y)", "x")
        self.sweep_axis_combo.addItem("Rows Along Y (step in X)", "y")
        form.addRow("Pass direction", self.sweep_axis_combo)
        self.tool_combo.currentIndexChanged.connect(self._sync_tool_controls)
        self._sync_tool_controls()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def params_payload(self) -> dict[str, float | str | int | None]:
        start_x = float(self.start_x_spin.value())
        start_y = float(self.start_y_spin.value())
        width = max(0.001, float(self.width_spin.value()))
        height = max(0.001, float(self.height_spin.value()))
        selected_slot = self.selected_tool_slot()
        tool_dia = self._selected_tool_diameter_mm(default=float(self.tool_dia_spin.value()))
        return {
            "tool_diameter_mm": tool_dia,
            "bounds_min_x_mm": start_x,
            "bounds_max_x_mm": start_x + width,
            "bounds_min_y_mm": start_y,
            "bounds_max_y_mm": start_y + height,
            "stepover_mm": float(self.stepover_mm_spin.value()),
            "stepover_pct": float(self.stepover_pct_spin.value()),
            "margin_mm": float(self.margin_spin.value()),
            "sweep_axis": str(self.sweep_axis_combo.currentData() or "x"),
            "selected_tool_slot": selected_slot,
        }

    def selected_tool_slot(self) -> int | None:
        data = self.tool_combo.currentData()
        if isinstance(data, int):
            return data
        return None

    def _sync_tool_controls(self) -> None:
        slot = self.selected_tool_slot()
        if slot is None:
            self.tool_dia_spin.setEnabled(True)
            return
        self.tool_dia_spin.setEnabled(False)
        self.tool_dia_spin.setValue(self._selected_tool_diameter_mm(default=float(self.tool_dia_spin.value())))

    def _selected_tool_diameter_mm(self, *, default: float) -> float:
        slot = self.selected_tool_slot()
        if slot is None:
            return max(0.001, float(default))
        tool = self._tools_by_slot.get(int(slot))
        if tool is None:
            return max(0.001, float(default))
        return max(0.001, float(getattr(tool, "diameter_mm", 0.0) or 0.0))

    def _set_tool_combo_default(self, slot: int | None) -> None:
        if isinstance(slot, int):
            for i in range(self.tool_combo.count()):
                data = self.tool_combo.itemData(i)
                if isinstance(data, int) and int(data) == int(slot):
                    self.tool_combo.setCurrentIndex(i)
                    return
        # Prefer first defined tool if no explicit default slot is available.
        if self.tool_combo.count() > 1:
            self.tool_combo.setCurrentIndex(1)


def _float_spin(
    parent: QWidget,
    *,
    value: float,
    minimum: float,
    maximum: float,
    decimals: int,
    suffix: str = "",
) -> QDoubleSpinBox:
    spin = QDoubleSpinBox(parent)
    spin.setRange(float(minimum), float(maximum))
    spin.setDecimals(int(decimals))
    spin.setValue(float(value))
    if suffix:
        spin.setSuffix(suffix)
    return spin


def _mm_spin(parent: QWidget, *, value: float, minimum: float, maximum: float) -> QDoubleSpinBox:
    spin = _float_spin(
        parent,
        value=value,
        minimum=minimum,
        maximum=maximum,
        decimals=4,
        suffix=" mm",
    )
    return spin
