from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import logging
from html import escape
import os
import sys

from app.core.project import Layer


LOGGER = logging.getLogger(__name__)
_DLL_SEARCH_CONFIGURED = False
_DLL_HANDLES: list[object] = []


@dataclass(slots=True)
class RenderResult:
    artifact: Path
    bbox: tuple[float, float, float, float] | None
    format: str


class LayerRenderer:
    def __init__(self, cache_root: Path | None = None) -> None:
        self.cache_root = cache_root or Path(".cache") / "renders"
        self.cache_root.mkdir(parents=True, exist_ok=True)

    def render(self, layer: Layer) -> RenderResult:
        stem = self._safe_stem(layer.path)
        svg_path = self.cache_root / f"{stem}.svg"
        fallback_svg_path = self.cache_root / f"{stem}.fallback.svg"

        try:
            self._render_with_pcb_tools(layer, svg_path)
            layer.rendered_artifact_path = svg_path
            return RenderResult(artifact=svg_path, bbox=layer.bbox, format="svg")
        except Exception as exc:
            LOGGER.warning(
                "Cairo render unavailable for %s. Using placeholder SVG fallback. Reason: %s",
                layer.path,
                exc,
            )
            self._render_placeholder_svg(layer, fallback_svg_path)
            layer.rendered_artifact_path = fallback_svg_path
            return RenderResult(artifact=fallback_svg_path, bbox=layer.bbox, format="svg")

    @staticmethod
    def _render_with_pcb_tools(layer: Layer, output_path: Path) -> None:
        ctx = _create_cairo_context()
        # pcb-tools paints an opaque layer background by default.
        # Mark background as already painted so only copper/traces/outlines are drawn.
        ctx.has_bg = True
        layer.source.render(ctx)
        ctx.dump(str(output_path))

    @staticmethod
    def _render_placeholder_svg(layer: Layer, output_path: Path) -> None:
        min_x = 0.0
        min_y = 0.0
        max_x = 100.0
        max_y = 80.0
        if layer.bbox:
            min_x, min_y, max_x, max_y = layer.bbox
            if max_x <= min_x:
                max_x = min_x + 1.0
            if max_y <= min_y:
                max_y = min_y + 1.0

        width = max_x - min_x
        height = max_y - min_y

        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="{min_x} {-max_y} {width} {height}">
  <rect x="{min_x}" y="{-max_y}" width="{width}" height="{height}" fill="none" stroke="{layer.color}" stroke-width="{max(width, height) * 0.01:.4f}"/>
  <text x="{min_x + width * 0.03}" y="{-max_y + height * 0.12}" font-size="{max(width, height) * 0.08:.4f}" fill="{layer.color}">
    {escape(layer.name)} (fallback)
  </text>
</svg>
"""
        output_path.write_text(svg, encoding="utf-8")

    @staticmethod
    def _safe_stem(path: Path) -> str:
        return path.name.replace(".", "_")


def _create_cairo_context():
    """Load cairo backend lazily so UI can start even if cairo is unavailable."""
    _configure_windows_cairo_dll_search()

    try:
        from gerber.render.cairo_backend import GerberCairoContext
    except Exception as exc:
        raise RuntimeError(f"pcb-tools cairo backend is unavailable: {exc}") from exc
    return GerberCairoContext()


def _configure_windows_cairo_dll_search() -> None:
    """Attempt to expose cairo DLLs on Windows without requiring manual PATH tweaks."""
    global _DLL_SEARCH_CONFIGURED, _DLL_HANDLES
    if _DLL_SEARCH_CONFIGURED:
        return
    _DLL_SEARCH_CONFIGURED = True

    if sys.platform != "win32":
        return

    candidates: list[Path] = []
    env_keys = ("GTK_BIN", "GTK3_BIN", "GTK_HOME", "GTK_ROOT", "MSYS2_ROOT")
    for key in env_keys:
        value = os.environ.get(key)
        if not value:
            continue
        root = Path(value)
        if key.endswith("_BIN"):
            candidates.append(root)
        else:
            candidates.append(root / "bin")
            candidates.append(root / "mingw64" / "bin")
            candidates.append(root / "ucrt64" / "bin")

    candidates.extend(
        [
            Path(r"C:\Program Files\GTK3-Runtime Win64\bin"),
            Path(r"C:\Program Files\GTK3-Runtime Win64\lib"),
            Path(r"C:\msys64\mingw64\bin"),
            Path(r"C:\msys64\ucrt64\bin"),
        ]
    )

    for directory in candidates:
        if not directory.exists():
            continue
        try:
            _DLL_HANDLES.append(os.add_dll_directory(str(directory)))
        except Exception:
            continue
