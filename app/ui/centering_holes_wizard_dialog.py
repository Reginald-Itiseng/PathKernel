"""Wizard UI for selecting and previewing centering-hole placement on board bounds."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)


class CenteringHolesWizardDialog(QDialog):
    """Wizard for centering-hole orientation and board-to-hole offset."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        hole_diameter_mm: float = 1.0,
        initial_distance_mm: float = 12.0,
        board_bounds_mm: tuple[float, float, float, float] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Centering Holes Wizard")
        # Slightly larger default with explicit bounds so the dialog does not
        # grow unreasonably large on high-resolution displays.
        self.resize(760, 620)
        self.setMinimumSize(700, 560)
        self.setMaximumSize(1040, 820)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        intro = QLabel(
            "Create two fixing holes for double-sided PCB alignment.\n"
            "Set the distance from board outline to hole center and choose orientation.",
            self,
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        options_row = QHBoxLayout()
        options_row.setSpacing(10)
        root.addLayout(options_row)

        self.horizontal_radio = QRadioButton("Horizontal fixing holes", self)
        self.vertical_radio = QRadioButton("Vertical fixing holes", self)
        self._orientation_group = QButtonGroup(self)
        self._orientation_group.setExclusive(True)
        self._orientation_group.addButton(self.horizontal_radio)
        self._orientation_group.addButton(self.vertical_radio)
        self.horizontal_radio.setChecked(True)
        options_row.addWidget(
            self._option_card(
                "Horizontal fixing holes",
                "One hole on the left and one on the right, centered on Y.",
                self.horizontal_radio,
            ),
            stretch=1,
        )
        options_row.addWidget(
            self._option_card(
                "Vertical fixing holes",
                "One hole on the top and one on the bottom, centered on X.",
                self.vertical_radio,
            ),
            stretch=1,
        )

        controls_row = QHBoxLayout()
        controls_row.setSpacing(10)
        root.addLayout(controls_row)

        self.distance_spin = QDoubleSpinBox(self)
        self.distance_spin.setRange(0.1, 500.0)
        self.distance_spin.setDecimals(3)
        self.distance_spin.setValue(max(0.1, float(initial_distance_mm)))
        self.distance_spin.setSuffix(" mm")
        self.distance_spin.setPrefix("Outline to hole: ")
        controls_row.addWidget(self.distance_spin, stretch=0)

        self.hole_dia_label = QLabel(self)
        self.hole_dia_label.setText(f"Hole diameter from Selected tools: {float(hole_diameter_mm):.3f} mm")
        self.hole_dia_label.setStyleSheet("color: #9fb0c4;")
        controls_row.addWidget(self.hole_dia_label, stretch=1)

        self.preview_source_label = QLabel(self)
        self.preview_source_label.setStyleSheet("color: #7f8c9a;")
        self.preview_source_label.setWordWrap(True)
        root.addWidget(self.preview_source_label)

        self.preview = _CenteringPreviewWidget(self)
        self.preview.setMinimumHeight(340)
        self.preview.set_board_bounds_mm(board_bounds_mm)
        self.preview.set_hole_diameter_mm(max(0.05, float(hole_diameter_mm)))
        self.preview.set_distance_mm(float(self.distance_spin.value()))
        self.preview.set_orientation(self.selected_orientation())
        root.addWidget(self.preview, stretch=1)

        if board_bounds_mm and len(board_bounds_mm) == 4:
            try:
                min_x, min_y, max_x, max_y = [float(v) for v in board_bounds_mm]
                w_mm = max(0.0, max_x - min_x)
                h_mm = max(0.0, max_y - min_y)
                self.preview_source_label.setText(
                    f"Preview rectangle: imported layers only (top/bottom/cutout/artwork), {w_mm:.3f} x {h_mm:.3f} mm."
                )
            except Exception:
                self.preview_source_label.setText(
                    "Preview rectangle: imported layers only (top/bottom/cutout/artwork)."
                )
        else:
            self.preview_source_label.setText(
                "Preview rectangle: imported layers only (top/bottom/cutout/artwork)."
            )

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.horizontal_radio.toggled.connect(self._sync_preview)
        self.vertical_radio.toggled.connect(self._sync_preview)
        self.distance_spin.valueChanged.connect(self._sync_preview)

    @staticmethod
    def _option_card(title: str, detail: str, radio: QRadioButton) -> QFrame:
        card = QFrame()
        card.setFrameShape(QFrame.StyledPanel)
        card.setObjectName("centeringOptionCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        layout.addWidget(radio)

        title_label = QLabel(title, card)
        title_label.setStyleSheet("font-weight: 600;")
        title_label.setWordWrap(True)
        detail_label = QLabel(detail, card)
        detail_label.setWordWrap(True)
        detail_label.setStyleSheet("color: #7f8c9a;")
        layout.addWidget(title_label)
        layout.addWidget(detail_label)
        layout.addStretch(1)
        return card

    def _sync_preview(self) -> None:
        self.preview.set_orientation(self.selected_orientation())
        self.preview.set_distance_mm(float(self.distance_spin.value()))

    def selected_orientation(self) -> str:
        return "vertical" if self.vertical_radio.isChecked() else "horizontal"

    def selected_distance_mm(self) -> float:
        return float(self.distance_spin.value())


class _CenteringPreviewWidget(QWidget):
    """Painter-only board sketch that updates live as wizard values change."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._orientation = "horizontal"
        self._distance_mm = 12.0
        self._hole_diameter_mm = 1.0
        self._board_width_mm = 100.0
        self._board_height_mm = 70.0
        self.setAutoFillBackground(False)

    def set_orientation(self, orientation: str) -> None:
        normalized = str(orientation or "").strip().lower()
        if normalized not in {"horizontal", "vertical"}:
            normalized = "horizontal"
        if normalized == self._orientation:
            return
        self._orientation = normalized
        self.update()

    def set_distance_mm(self, value: float) -> None:
        dist = max(0.1, float(value))
        if abs(dist - self._distance_mm) < 1e-9:
            return
        self._distance_mm = dist
        self.update()

    def set_hole_diameter_mm(self, value: float) -> None:
        dia = max(0.05, float(value))
        if abs(dia - self._hole_diameter_mm) < 1e-9:
            return
        self._hole_diameter_mm = dia
        self.update()

    def set_board_bounds_mm(self, bounds: tuple[float, float, float, float] | None) -> None:
        if not bounds or len(bounds) != 4:
            return
        try:
            min_x, min_y, max_x, max_y = [float(v) for v in bounds]
        except Exception:
            return
        w = max(1e-6, abs(max_x - min_x))
        h = max(1e-6, abs(max_y - min_y))
        if abs(w - self._board_width_mm) < 1e-9 and abs(h - self._board_height_mm) < 1e-9:
            return
        self._board_width_mm = w
        self._board_height_mm = h
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.fillRect(self.rect(), QColor("#151a20"))

            frame = self.rect().adjusted(14, 14, -14, -14)
            if frame.width() <= 10 or frame.height() <= 10:
                return

            board, horizontal, vertical, mm_to_px = self._layout_preview_geometry(frame)
            hole_radius = max(2.8, (self._hole_diameter_mm * 0.5) * mm_to_px * 1.35)

            board_fill = QColor("#4D7FC4")
            board_fill.setAlpha(150)
            painter.setPen(QPen(QColor("#88B4F0"), 1.5))
            painter.setBrush(board_fill)
            painter.drawRoundedRect(board, 4.0, 4.0)

            selected_color = QColor("#E6F1FF")
            selected_outline = QColor("#D4E6FF")
            muted_color = QColor("#7D8795")
            muted_color.setAlpha(130)
            muted_outline = QColor("#6C7380")
            muted_outline.setAlpha(150)

            self._draw_hole_pair(
                painter,
                points=horizontal,
                radius=hole_radius,
                active=(self._orientation == "horizontal"),
                active_fill=selected_color,
                active_outline=selected_outline,
                muted_fill=muted_color,
                muted_outline=muted_outline,
            )
            self._draw_hole_pair(
                painter,
                points=vertical,
                radius=hole_radius,
                active=(self._orientation == "vertical"),
                active_fill=selected_color,
                active_outline=selected_outline,
                muted_fill=muted_color,
                muted_outline=muted_outline,
            )

            if self._orientation == "horizontal":
                for pt in horizontal:
                    self._draw_hole_diameter_annotation(
                        painter,
                        board=board,
                        center=pt,
                        radius=hole_radius,
                        text=f"{self._hole_diameter_mm:.3f} mm",
                        color=QColor("#F0F6FF"),
                    )
            else:
                for pt in vertical:
                    self._draw_hole_diameter_annotation(
                        painter,
                        board=board,
                        center=pt,
                        radius=hole_radius,
                        text=f"{self._hole_diameter_mm:.3f} mm",
                        color=QColor("#F0F6FF"),
                    )

            if self._orientation == "horizontal":
                self._draw_horizontal_dimension(
                    painter,
                    board,
                    horizontal[1],
                    text=f"{self._distance_mm:.3f} mm",
                    color=QColor("#F0F6FF"),
                )
            else:
                self._draw_vertical_dimension(
                    painter,
                    board,
                    vertical[0],
                    text=f"{self._distance_mm:.3f} mm",
                    color=QColor("#F0F6FF"),
                )

            font = QFont(painter.font())
            font.setPointSize(max(8, font.pointSize()))
            painter.setFont(font)
            painter.setPen(QColor("#a9b7c6"))
            painter.drawText(
                frame.adjusted(2, 2, -2, -2),
                Qt.AlignTop | Qt.AlignLeft,
                f"Hole dia: {self._hole_diameter_mm:.3f} mm",
            )
        finally:
            painter.end()

    def _layout_preview_geometry(
        self,
        frame: QRectF,
    ) -> tuple[QRectF, list[QPointF], list[QPointF], float]:
        """Fit board + centering-hole world geometry into preview with locked aspect.

        This mirrors viewport behavior: one uniform scale for X/Y so geometry
        proportions remain stable regardless of dialog resize.
        """
        board_w = max(1e-6, float(self._board_width_mm))
        board_h = max(1e-6, float(self._board_height_mm))
        dist = max(0.0, float(self._distance_mm))
        hole_r_mm = max(0.025, float(self._hole_diameter_mm) * 0.5)

        # World coordinates in mm, centered at origin.
        bx0 = -0.5 * board_w
        by0 = -0.5 * board_h
        bx1 = 0.5 * board_w
        by1 = 0.5 * board_h

        hx = 0.5 * board_w + dist
        hy = 0.5 * board_h + dist
        horizontal_world = [(-hx, 0.0), (hx, 0.0)]
        vertical_world = [(0.0, -hy), (0.0, hy)]

        # Fit both board and all possible holes (horizontal + vertical) so
        # toggling option does not rescale/stretch the preview.
        min_x = min(bx0, -hx - hole_r_mm, -hole_r_mm)
        max_x = max(bx1, hx + hole_r_mm, hole_r_mm)
        min_y = min(by0, -hy - hole_r_mm, -hole_r_mm)
        max_y = max(by1, hy + hole_r_mm, hole_r_mm)
        world_w = max(1e-6, max_x - min_x)
        world_h = max(1e-6, max_y - min_y)

        # Reserve space for dimension annotations so all preview contents stay visible.
        if self._orientation == "horizontal":
            left_reserve = 150.0
            right_reserve = 200.0
            top_reserve = 95.0
            bottom_reserve = 80.0
        else:
            left_reserve = 180.0
            right_reserve = 170.0
            top_reserve = 130.0
            bottom_reserve = 120.0

        # Keep reserves bounded relative to current preview size.
        left_reserve = min(left_reserve, frame.width() * 0.34)
        right_reserve = min(right_reserve, frame.width() * 0.34)
        top_reserve = min(top_reserve, frame.height() * 0.34)
        bottom_reserve = min(bottom_reserve, frame.height() * 0.34)

        safe_rect = frame.adjusted(left_reserve, top_reserve, -right_reserve, -bottom_reserve)
        if safe_rect.width() < 40.0 or safe_rect.height() < 40.0:
            safe_rect = frame.adjusted(20.0, 20.0, -20.0, -20.0)

        avail_w = max(1.0, safe_rect.width())
        avail_h = max(1.0, safe_rect.height())
        scale = max(0.1, min(avail_w / world_w, avail_h / world_h))

        cx = safe_rect.center().x()
        cy = safe_rect.center().y()

        def _map_xy(x_mm: float, y_mm: float) -> QPointF:
            # Keep Y-down orientation to match the main viewport convention.
            return QPointF(cx + (x_mm * scale), cy + (y_mm * scale))

        top_left = _map_xy(bx0, by0)
        bottom_right = _map_xy(bx1, by1)
        board_rect = QRectF(top_left, bottom_right).normalized()
        horizontal = [_map_xy(x, y) for x, y in horizontal_world]
        vertical = [_map_xy(x, y) for x, y in vertical_world]
        return board_rect, horizontal, vertical, scale

    @staticmethod
    def _draw_hole_pair(
        painter: QPainter,
        *,
        points: list[QPointF],
        radius: float,
        active: bool,
        active_fill: QColor,
        active_outline: QColor,
        muted_fill: QColor,
        muted_outline: QColor,
    ) -> None:
        fill = active_fill if active else muted_fill
        outline = active_outline if active else muted_outline
        painter.setBrush(fill)
        painter.setPen(QPen(outline, 1.2))
        for pt in points:
            painter.drawEllipse(pt, radius, radius)

    @staticmethod
    def _draw_arrow_head(
        painter: QPainter,
        *,
        tip: QPointF,
        direction: QPointF,
        size: float = 6.0,
        color: QColor,
    ) -> None:
        painter.save()
        try:
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            length = math.hypot(direction.x(), direction.y())
            if length < 1e-9:
                return
            dx = direction.x() / length
            dy = direction.y() / length
            nx = -dy
            ny = dx
            back = QPointF(tip.x() - (dx * size), tip.y() - (dy * size))
            p1 = QPointF(back.x() + (nx * (size * 0.45)), back.y() + (ny * (size * 0.45)))
            p2 = QPointF(back.x() - (nx * (size * 0.45)), back.y() - (ny * (size * 0.45)))
            painter.drawPolygon(QPolygonF([tip, p1, p2]))
        finally:
            painter.restore()

    def _draw_horizontal_dimension(
        self,
        painter: QPainter,
        board: QRectF,
        hole: QPointF,
        *,
        text: str,
        color: QColor,
    ) -> None:
        y = board.bottom() + 26.0
        x0 = board.right()
        x1 = hole.x()
        pen = QPen(color, 1.1)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawLine(QPointF(x0, y), QPointF(x1, y))
        painter.drawLine(QPointF(x0, board.bottom()), QPointF(x0, y))
        painter.drawLine(QPointF(x1, hole.y()), QPointF(x1, y))
        # Opposite-facing arrows, pointing inward toward measured span.
        self._draw_arrow_head(painter, tip=QPointF(x0, y), direction=QPointF(1.0, 0.0), color=color)
        self._draw_arrow_head(painter, tip=QPointF(x1, y), direction=QPointF(-1.0, 0.0), color=color)
        painter.drawText(
            QRectF(x1 + 8.0, y - 14.0, 110.0, 18.0),
            Qt.AlignLeft | Qt.AlignVCenter,
            text,
        )

    def _draw_vertical_dimension(
        self,
        painter: QPainter,
        board: QRectF,
        hole: QPointF,
        *,
        text: str,
        color: QColor,
    ) -> None:
        x = board.left() - 26.0
        y0 = board.top()
        y1 = hole.y()
        pen = QPen(color, 1.1)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawLine(QPointF(x, y0), QPointF(x, y1))
        painter.drawLine(QPointF(board.left(), y0), QPointF(x, y0))
        painter.drawLine(QPointF(hole.x(), y1), QPointF(x, y1))
        # Opposite-facing arrows, pointing inward toward measured span.
        self._draw_arrow_head(painter, tip=QPointF(x, y0), direction=QPointF(0.0, 1.0), color=color)
        self._draw_arrow_head(painter, tip=QPointF(x, y1), direction=QPointF(0.0, -1.0), color=color)
        painter.drawText(
            QRectF(x - 100.0, y1 - 20.0, 96.0, 18.0),
            Qt.AlignRight | Qt.AlignVCenter,
            text,
        )

    def _draw_hole_diameter_annotation(
        self,
        painter: QPainter,
        *,
        board: QRectF,
        center: QPointF,
        radius: float,
        text: str,
        color: QColor,
    ) -> None:
        board_center = board.center()
        dx = center.x() - board_center.x()
        dy = center.y() - board_center.y()
        horizontal_mode = abs(dx) >= abs(dy)

        # Diameter dimension line goes across the circle itself.
        x0 = center.x() - radius
        x1 = center.x() + radius
        y = center.y()

        painter.save()
        try:
            painter.setPen(QPen(color, 1.0))
            painter.setBrush(Qt.NoBrush)
            painter.drawLine(QPointF(x0, y), QPointF(x1, y))
            # Opposite-facing inward arrows on diameter line.
            self._draw_arrow_head(painter, tip=QPointF(x0, y), direction=QPointF(1.0, 0.0), color=color)
            self._draw_arrow_head(painter, tip=QPointF(x1, y), direction=QPointF(-1.0, 0.0), color=color)

            # Text is placed away from the board+hole cluster with a leader.
            standoff = max(34.0, radius + 24.0)
            if horizontal_mode:
                side_sign = 1.0 if dx >= 0.0 else -1.0
                leader_start = QPointF(center.x() + (side_sign * radius), center.y())
                label_anchor = QPointF(
                    center.x() + (side_sign * (radius + standoff)),
                    center.y() - max(12.0, radius + 8.0),
                )
                painter.drawLine(leader_start, label_anchor)
                if side_sign > 0.0:
                    text_rect = QRectF(label_anchor.x() + 8.0, label_anchor.y() - 9.0, 108.0, 18.0)
                    text_align = Qt.AlignLeft | Qt.AlignVCenter
                else:
                    text_rect = QRectF(label_anchor.x() - 116.0, label_anchor.y() - 9.0, 108.0, 18.0)
                    text_align = Qt.AlignRight | Qt.AlignVCenter
            else:
                side_sign = 1.0 if dy >= 0.0 else -1.0
                leader_start = QPointF(center.x(), center.y() + (side_sign * radius))
                label_anchor = QPointF(
                    center.x() + radius + 14.0,
                    center.y() + (side_sign * (radius + standoff)),
                )
                painter.drawLine(leader_start, label_anchor)
                if side_sign < 0.0:
                    text_rect = QRectF(label_anchor.x() + 8.0, label_anchor.y() - 22.0, 108.0, 18.0)
                else:
                    text_rect = QRectF(label_anchor.x() + 8.0, label_anchor.y() + 4.0, 108.0, 18.0)
                text_align = Qt.AlignLeft | Qt.AlignVCenter

            painter.drawText(text_rect, text_align, f"Dia {text}")
        finally:
            painter.restore()

