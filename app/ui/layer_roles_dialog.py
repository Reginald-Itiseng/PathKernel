"""Dialog for assigning semantic roles (top/bottom/cutout/etc.) to imported layers."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


ROLE_OPTIONS: list[tuple[str, str]] = [
    ("Top Layer", "top"),
    ("Bottom Layer", "bottom"),
    ("Drills", "drills"),
    ("Cutout", "cutout"),
    ("Artwork", "artwork"),
]


@dataclass(slots=True)
class LayerRoleEntry:
    """One table row describing an imported layer and its currently assigned role."""

    layer_index: int
    name: str
    kind: str
    path: str
    role: str


class LayerRolesDialog(QDialog):
    """Table-based role assignment dialog used during import and role re-mapping."""

    def __init__(
        self,
        entries: list[LayerRoleEntry],
        parent: QWidget | None = None,
        *,
        title: str = "Assign Layer Roles",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 420)
        self._entries = list(entries)
        self._row_combos: list[QComboBox] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        intro = QLabel(
            "Review and confirm roles for imported layers. "
            "These roles control workflow behavior (isolation, drills, cutout).",
            self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        table = QTableWidget(self)
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(["Layer", "Kind", "File", "Role"])
        table.verticalHeader().setVisible(False)
        table.setRowCount(len(self._entries))
        table.setSelectionMode(QTableWidget.NoSelection)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        table.setAlternatingRowColors(True)

        for row, entry in enumerate(self._entries):
            name_item = QTableWidgetItem(entry.name)
            kind_item = QTableWidgetItem(entry.kind)
            file_item = QTableWidgetItem(entry.path)
            for item in (name_item, kind_item, file_item):
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            table.setItem(row, 0, name_item)
            table.setItem(row, 1, kind_item)
            table.setItem(row, 2, file_item)

            combo = QComboBox(table)
            for label, role in ROLE_OPTIONS:
                combo.addItem(label, role)
            combo.setCurrentIndex(self._role_index(entry.role))
            table.setCellWidget(row, 3, combo)
            self._row_combos.append(combo)

        layout.addWidget(table, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(buttons)
        layout.addLayout(btn_row)

    def selected_roles(self) -> dict[int, str]:
        out: dict[int, str] = {}
        for entry, combo in zip(self._entries, self._row_combos):
            role = str(combo.currentData() or "artwork")
            out[int(entry.layer_index)] = role
        return out

    @staticmethod
    def _role_index(role: str) -> int:
        normalized = str(role or "").strip().lower()
        if normalized == "holes":
            normalized = "drills"
        if normalized not in {r for _, r in ROLE_OPTIONS}:
            normalized = "artwork"
        for idx, (_label, key) in enumerate(ROLE_OPTIONS):
            if key == normalized:
                return idx
        return 0
