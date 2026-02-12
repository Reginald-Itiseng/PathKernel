from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import itertools
import math


BBox = tuple[float, float, float, float]


DEFAULT_LAYER_COLORS = [
    "#d32f2f",
    "#1976d2",
    "#388e3c",
    "#f57c00",
    "#7b1fa2",
    "#00796b",
    "#5d4037",
    "#455a64",
]

LAYER_ROLES = ("unassigned", "top", "bottom", "holes", "cutout")


@dataclass(slots=True)
class ManualEdit:
    """User-authored geometry overlay stored per layer."""

    kind: str  # "pad" | "track" | "hole"
    x_mm: float = 0.0
    y_mm: float = 0.0
    x2_mm: float = 0.0
    y2_mm: float = 0.0
    diameter_mm: float = 0.0
    width_mm: float = 0.0
    height_mm: float = 0.0
    shape: str = "circle"  # for pad: circle|rect|obround|roundrect
    corner_radius_mm: float = 0.0
    hole_diameter_mm: float = 0.0


@dataclass(slots=True)
class Layer:
    name: str
    path: Path
    kind: str
    source: Any
    visible: bool = True
    color: str = "#1976d2"
    opacity: float = 1.0
    # Layer-local transform in project/work coordinates.
    offset_x_mm: float = 0.0
    offset_y_mm: float = 0.0
    rotation_deg: float = 0.0
    mirror_x: bool = False
    mirror_y: bool = False
    role: str = "unassigned"
    # Imported primitive edits keyed by primitive index.
    primitive_overrides: dict[int, dict[str, float | str]] = field(default_factory=dict)
    manual_edits: list[ManualEdit] = field(default_factory=list)
    bbox: BBox | None = None
    rendered_artifact_path: Path | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class WorkspaceTransform:
    """Global transform for project/work space.

    Coordinate policy:
    - File space: raw coordinates parsed from CAM files.
    - Project/work space: file coordinates after global workspace transform.
    - Machine space: future mapping from project/work to machine setup.
    """

    origin_x_mm: float = 0.0
    origin_y_mm: float = 0.0


@dataclass(slots=True)
class Project:
    layers: list[Layer] = field(default_factory=list)
    workspace: WorkspaceTransform = field(default_factory=WorkspaceTransform)
    _palette_cycle: Any = field(default_factory=lambda: itertools.cycle(DEFAULT_LAYER_COLORS), init=False)

    def clear(self) -> None:
        self.layers.clear()
        self._palette_cycle = itertools.cycle(DEFAULT_LAYER_COLORS)

    def add_layer(self, layer: Layer) -> Layer:
        if not layer.color:
            layer.color = self.next_color()
        self.layers.append(layer)
        return layer

    def next_color(self) -> str:
        return next(self._palette_cycle)

    def set_visibility(self, index: int, visible: bool) -> None:
        self.layers[index].visible = visible

    def set_opacity(self, index: int, opacity: float) -> None:
        self.layers[index].opacity = max(0.0, min(1.0, opacity))

    def set_layer_color(self, index: int, color: str) -> None:
        self.layers[index].color = color

    def set_layer_role(self, index: int, role: str) -> None:
        if role not in LAYER_ROLES:
            role = "unassigned"
        self.layers[index].role = role

    def set_workspace_origin(self, x_mm: float, y_mm: float) -> None:
        self.workspace.origin_x_mm = float(x_mm)
        self.workspace.origin_y_mm = float(y_mm)

    def nudge_workspace_origin(self, dx_mm: float, dy_mm: float) -> None:
        self.workspace.origin_x_mm += float(dx_mm)
        self.workspace.origin_y_mm += float(dy_mm)

    def set_layer_offset(self, index: int, x_mm: float, y_mm: float) -> None:
        layer = self.layers[index]
        layer.offset_x_mm = float(x_mm)
        layer.offset_y_mm = float(y_mm)

    def set_layer_rotation(self, index: int, rotation_deg: float) -> None:
        # Keep values bounded for cleaner serialization and comparisons.
        self.layers[index].rotation_deg = float(math.fmod(rotation_deg, 360.0))

    def set_layer_mirror(self, index: int, mirror_x: bool, mirror_y: bool) -> None:
        layer = self.layers[index]
        layer.mirror_x = bool(mirror_x)
        layer.mirror_y = bool(mirror_y)

    def move_layer(self, index: int, direction: int) -> int:
        new_index = index + direction
        if new_index < 0 or new_index >= len(self.layers):
            return index
        layer = self.layers.pop(index)
        self.layers.insert(new_index, layer)
        return new_index

    def visible_layers(self) -> list[Layer]:
        return [layer for layer in self.layers if layer.visible]
