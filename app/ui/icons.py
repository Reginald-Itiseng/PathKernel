"""Icon loading and theme-aware SVG recoloring utilities used across the UI."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap

try:
    from PySide6.QtSvg import QSvgRenderer
except Exception:  # pragma: no cover - optional module on some installs
    QSvgRenderer = None


_ICONS_DIR = Path(__file__).resolve().parent.parent / "assets" / "icons"
_ICON_MANIFEST_PATH = _ICONS_DIR / "icon_manifest.json"


def _parse_icon_size_from_name(path: Path) -> int | None:
    stem = path.stem
    tail = stem.rsplit("_", 1)
    if len(tail) != 2:
        return None
    try:
        return int(tail[1])
    except Exception:
        return None


@lru_cache(maxsize=1)
def _icon_index() -> dict[str, dict[int, Path]]:
    index: dict[str, dict[int, Path]] = {}
    if _ICON_MANIFEST_PATH.exists():
        try:
            raw = json.loads(_ICON_MANIFEST_PATH.read_text(encoding="utf-8"))
        except Exception:
            raw = []
        if isinstance(raw, list):
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                icon_id = str(entry.get("id", "")).strip()
                filename = str(entry.get("file", "")).strip()
                if not icon_id or not filename:
                    continue
                path = _ICONS_DIR / filename
                if not path.exists():
                    continue
                size = entry.get("size")
                try:
                    size_val = int(size)
                except Exception:
                    size_val = _parse_icon_size_from_name(path) or 16
                index.setdefault(icon_id, {})[size_val] = path
    if index:
        return index

    for path in _ICONS_DIR.glob("*.svg"):
        stem = path.stem
        parts = stem.rsplit("_", 1)
        if len(parts) != 2:
            continue
        icon_id = parts[0]
        size = _parse_icon_size_from_name(path)
        if size is None:
            continue
        index.setdefault(icon_id, {})[size] = path
    return index


def _normalize_color_hex(color: str | QColor | None) -> str | None:
    if color is None:
        return None
    qcolor = QColor(color)
    if not qcolor.isValid():
        return None
    return qcolor.name(QColor.HexRgb).lower()


def _scale_stroke_widths(svg_text: str, stroke_scale: float) -> str:
    scale = float(stroke_scale)
    if abs(scale - 1.0) < 1e-6:
        return svg_text

    def _fmt(value: float) -> str:
        text = f"{max(0.05, value):.4f}"
        return text.rstrip("0").rstrip(".")

    def _attr_repl(match: re.Match[str]) -> str:
        prefix, num_text, unit, suffix = match.group(1), match.group(2), match.group(3), match.group(4)
        try:
            num = float(num_text)
        except Exception:
            return match.group(0)
        return f"{prefix}{_fmt(num * scale)}{unit}{suffix}"

    def _style_repl(match: re.Match[str]) -> str:
        prefix, num_text, unit, suffix = match.group(1), match.group(2), match.group(3), match.group(4)
        try:
            num = float(num_text)
        except Exception:
            return match.group(0)
        return f"{prefix}{_fmt(num * scale)}{unit}{suffix}"

    svg_text = re.sub(
        r'(stroke-width\s*=\s*["\']\s*)([0-9]*\.?[0-9]+)([a-zA-Z%]*)(\s*["\'])',
        _attr_repl,
        svg_text,
    )
    svg_text = re.sub(
        r'(stroke-width\s*:\s*)([0-9]*\.?[0-9]+)([a-zA-Z%]*)(\s*;)',
        _style_repl,
        svg_text,
    )
    return svg_text


@lru_cache(maxsize=768)
def _svg_icon_variant(path_str: str, size: int, color_hex: str, stroke_scale: float) -> QIcon:
    if QSvgRenderer is None:
        return QIcon(path_str)
    try:
        path = Path(path_str)
        svg_text = path.read_text(encoding="utf-8")
    except Exception:
        return QIcon(path_str)

    if color_hex:
        # Icon pack uses currentColor; replacing it gives predictable contrast.
        svg_text = svg_text.replace("currentColor", color_hex)
    svg_text = _scale_stroke_widths(svg_text, stroke_scale)
    renderer = QSvgRenderer(bytes(svg_text, encoding="utf-8"))
    if not renderer.isValid():
        return QIcon(path_str)

    px = max(1, int(size))
    pixmap = QPixmap(px, px)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    try:
        renderer.render(painter)
    finally:
        painter.end()
    return QIcon(pixmap)


def icon_for(
    icon_id: str,
    size: int | None = None,
    color: str | QColor | None = None,
    stroke_scale: float = 1.0,
) -> QIcon:
    """Resolve an icon from the manifest and optionally recolor/retune stroke width."""
    icon_id = str(icon_id or "").strip()
    if not icon_id:
        return QIcon()
    versions = _icon_index().get(icon_id)
    if not versions:
        return QIcon()
    if size is None:
        chosen_size = sorted(versions.keys())[0]
    else:
        chosen_size = min(versions.keys(), key=lambda s: abs(s - int(size)))
    path = versions[chosen_size]
    color_hex = _normalize_color_hex(color)
    scale = float(stroke_scale)
    if color_hex or abs(scale - 1.0) > 1e-6:
        return _svg_icon_variant(
            str(path),
            int(size or chosen_size),
            color_hex or "",
            round(scale, 4),
        )
    return QIcon(str(path))
