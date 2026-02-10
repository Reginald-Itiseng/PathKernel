from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import itertools


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


@dataclass(slots=True)
class Layer:
    name: str
    path: Path
    kind: str
    source: Any
    visible: bool = True
    color: str = "#1976d2"
    bbox: BBox | None = None
    rendered_artifact_path: Path | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Project:
    layers: list[Layer] = field(default_factory=list)
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

    def visible_layers(self) -> list[Layer]:
        return [layer for layer in self.layers if layer.visible]

