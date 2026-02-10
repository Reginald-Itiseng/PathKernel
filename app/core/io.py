from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import logging

from app.core.project import Layer, Project


LOGGER = logging.getLogger(__name__)

GERBER_EXTENSIONS = {
    ".gtl",
    ".gbl",
    ".gto",
    ".gbo",
    ".gts",
    ".gbs",
    ".gko",
    ".gm1",
    ".gbr",
    ".art",
    ".pho",
}

EXCELLON_EXTENSIONS = {
    ".drl",
    ".txt",
    ".xln",
    ".drd",
}


@dataclass(slots=True)
class ParsedFile:
    layer: Layer


def detect_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in GERBER_EXTENSIONS:
        return "gerber"
    if ext in EXCELLON_EXTENSIONS:
        return "excellon"
    return "unknown"


def _extract_bbox(obj: Any) -> tuple[float, float, float, float] | None:
    bounds = getattr(obj, "bounds", None)
    if not bounds:
        return None
    try:
        (min_x, max_x), (min_y, max_y) = bounds
        return float(min_x), float(min_y), float(max_x), float(max_y)
    except Exception:
        return None


def _metadata(path: Path, kind: str, parsed: Any) -> dict[str, str]:
    return {
        "name": path.name,
        "kind": kind,
        "path": str(path),
        "units": str(getattr(parsed, "units", "unknown")),
        "primitives": str(len(getattr(parsed, "primitives", []))),
    }


def parse_file(path: Path, project: Project) -> ParsedFile:
    kind = detect_kind(path)
    try:
        import gerber

        # pcb-tools' gerber.read() uses deprecated 'rU' mode, which fails on Python 3.11+.
        # Read file content ourselves and parse through loads() instead.
        data = path.read_bytes().decode("latin-1")
        parsed = gerber.loads(data, filename=str(path))
    except Exception as exc:
        LOGGER.exception("Failed to parse file: %s", path)
        raise ValueError(f"Failed to parse '{path.name}': {exc}") from exc

    layer = Layer(
        name=path.stem,
        path=path,
        kind=kind,
        source=parsed,
        color=project.next_color(),
        bbox=_extract_bbox(parsed),
        metadata=_metadata(path, kind, parsed),
    )
    return ParsedFile(layer=layer)


def import_files(paths: list[Path], project: Project) -> list[Layer]:
    layers: list[Layer] = []
    for path in paths:
        parsed = parse_file(path, project)
        layers.append(project.add_layer(parsed.layer))
    return layers


def scan_folder(folder: Path) -> list[Path]:
    files: list[Path] = []
    for child in folder.iterdir():
        if child.is_file() and detect_kind(child) != "unknown":
            files.append(child)
    return sorted(files, key=lambda p: p.name.lower())
