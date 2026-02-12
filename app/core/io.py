from __future__ import annotations

"""File import/parsing helpers for Gerber and Excellon inputs."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import logging
import math

from app.core.project import LAYER_ROLES, Layer, Project


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


@dataclass(slots=True)
class _CompatSource:
    units: str
    primitives: list[Any]
    bounds: tuple[tuple[float, float], tuple[float, float]] | None = None


@dataclass(slots=True)
class _CompatLine:
    start: tuple[float, float]
    end: tuple[float, float]
    diameter: float = 0.0
    level_polarity: str = "dark"


@dataclass(slots=True)
class _CompatArc:
    start: tuple[float, float]
    end: tuple[float, float]
    center: tuple[float, float]
    direction: str = "counterclockwise"
    diameter: float = 0.0
    level_polarity: str = "dark"


@dataclass(slots=True)
class _CompatCircle:
    position: tuple[float, float]
    diameter: float
    level_polarity: str = "dark"
    flashed: bool = True


@dataclass(slots=True)
class Line:
    start: tuple[float, float]
    end: tuple[float, float]
    diameter: float = 0.0
    level_polarity: str = "dark"
    flashed: bool = False


@dataclass(slots=True)
class Arc:
    start: tuple[float, float]
    end: tuple[float, float]
    center: tuple[float, float]
    direction: str = "counterclockwise"
    diameter: float = 0.0
    level_polarity: str = "dark"
    flashed: bool = False


@dataclass(slots=True)
class Circle:
    position: tuple[float, float]
    diameter: float
    level_polarity: str = "dark"
    flashed: bool = True


@dataclass(slots=True)
class Obround:
    position: tuple[float, float]
    width: float
    height: float
    level_polarity: str = "dark"
    flashed: bool = True


@dataclass(slots=True)
class RoundRectangle:
    position: tuple[float, float]
    width: float
    height: float
    radius: float
    level_polarity: str = "dark"
    flashed: bool = True


@dataclass(slots=True)
class Polygon:
    vertices: list[tuple[float, float]]
    flashed: bool = True
    level_polarity: str = "dark"


@dataclass(slots=True)
class Drill:
    position: tuple[float, float]
    diameter: float
    level_polarity: str = "dark"
    flashed: bool = True


@dataclass(slots=True)
class Slot:
    start: tuple[float, float]
    end: tuple[float, float]
    diameter: float = 0.0
    level_polarity: str = "dark"
    flashed: bool = False


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


def _guess_role(path: Path, kind: str) -> str:
    name = path.stem.lower()
    ext = path.suffix.lower()
    if kind == "excellon" or ext in {".drl", ".xln", ".drd"}:
        return "holes"
    if "edge" in name or "cuts" in name or "outline" in name or ext in {".gko", ".gm1"}:
        return "cutout"
    if "f_cu" in name or "top" in name or ext in {".gtl"}:
        return "top"
    if "b_cu" in name or "bottom" in name or ext in {".gbl"}:
        return "bottom"
    return "unassigned"


def parse_file(path: Path, project: Project) -> ParsedFile:
    """Parse a CAM file and return a populated Layer model entry."""
    kind = detect_kind(path)
    try:
        parsed, backend = _parse_with_best_backend(path, kind)
    except Exception as exc:
        LOGGER.exception("Failed to parse file: %s", path)
        raise ValueError(f"Failed to parse '{path.name}': {exc}") from exc

    role = _guess_role(path, kind)
    meta = _metadata(path, kind, parsed)
    meta["parser_backend"] = backend
    layer = Layer(
        name=path.stem,
        path=path,
        kind=kind,
        source=parsed,
        color=project.next_color(),
        role=role if role in LAYER_ROLES else "unassigned",
        bbox=_extract_bbox(parsed),
        metadata=meta,
    )
    return ParsedFile(layer=layer)


def _parse_with_best_backend(path: Path, kind: str) -> tuple[Any, str]:
    # Preferred backend: gerbonara (actively maintained).
    try:
        parsed = _parse_with_gerbonara(path, kind)
        if parsed is not None:
            return parsed, "gerbonara"
    except Exception:
        LOGGER.exception("gerbonara parse backend failed for %s", path)

    # Fallback backend: pcb-tools legacy parser.
    parsed = _parse_with_pcb_tools(path)
    return parsed, "pcb-tools"


def _parse_with_pcb_tools(path: Path) -> Any:
    import gerber

    # pcb-tools' gerber.read() uses deprecated 'rU' mode, which fails on Python 3.11+.
    # Read file content ourselves and parse through loads() instead.
    data = path.read_bytes().decode("latin-1")
    return gerber.loads(data, filename=str(path))


def _parse_with_gerbonara(path: Path, kind: str) -> Any | None:
    try:
        import gerbonara  # type: ignore[import-not-found]
    except Exception:
        return None

    cam: Any | None = None
    if kind == "gerber":
        gerber_file = getattr(gerbonara, "GerberFile", None)
        if gerber_file is not None and hasattr(gerber_file, "open"):
            cam = gerber_file.open(str(path))
    elif kind == "excellon":
        excellon_file = getattr(gerbonara, "ExcellonFile", None)
        if excellon_file is not None and hasattr(excellon_file, "open"):
            cam = excellon_file.open(str(path))
    if cam is None:
        return None
    return _adapt_gerbonara_cam(cam, kind=kind)


def _adapt_gerbonara_cam(cam: Any, *, kind: str = "gerber") -> _CompatSource:
    objects = list(getattr(cam, "objects", []) or [])
    primitives: list[Any] = []
    min_x = float("inf")
    min_y = float("inf")
    max_x = float("-inf")
    max_y = float("-inf")

    for obj in objects:
        adapted = _adapt_gerbonara_object(obj, kind=kind)
        if adapted is None:
            continue
        items = adapted if isinstance(adapted, list) else [adapted]
        for primitive in items:
            primitives.append(primitive)
            bbox = _primitive_bbox(primitive)
            if bbox is None:
                continue
            x0, y0, x1, y1 = bbox
            min_x = min(min_x, x0)
            min_y = min(min_y, y0)
            max_x = max(max_x, x1)
            max_y = max(max_y, y1)

    bounds = None
    if min_x != float("inf"):
        bounds = ((min_x, max_x), (min_y, max_y))

    units = str(getattr(cam, "unit", getattr(cam, "units", "mm"))).lower()
    return _CompatSource(units=units, primitives=primitives, bounds=bounds)


def _adapt_gerbonara_object(obj: Any, *, kind: str = "gerber") -> Any | list[Any] | None:
    cls_name = obj.__class__.__name__.lower()
    polarity = _gerbonara_polarity(obj)

    if cls_name == "region" and hasattr(obj, "outline"):
        try:
            outline = list(getattr(obj, "outline") or [])
            verts: list[tuple[float, float]] = []
            for pt in outline:
                if isinstance(pt, (tuple, list)) and len(pt) >= 2:
                    verts.append((float(pt[0]), float(pt[1])))
            if len(verts) >= 3:
                if abs(verts[0][0] - verts[-1][0]) > 1e-9 or abs(verts[0][1] - verts[-1][1]) > 1e-9:
                    verts.append(verts[0])
                return Polygon(vertices=verts, flashed=True, level_polarity=polarity)
        except Exception:
            return None

    if "line" in cls_name:
        s = _pick_point(obj, ["start", "p1", "from_", "a"])
        e = _pick_point(obj, ["end", "p2", "to", "b"])
        if s is None and all(hasattr(obj, name) for name in ("x1", "y1")):
            s = (float(getattr(obj, "x1")), float(getattr(obj, "y1")))
        if e is None and all(hasattr(obj, name) for name in ("x2", "y2")):
            e = (float(getattr(obj, "x2")), float(getattr(obj, "y2")))
        if s is None or e is None:
            return None
        line_dia = _obj_width(obj)
        if kind == "excellon":
            return Slot(start=s, end=e, diameter=line_dia, level_polarity=polarity)
        return Line(start=s, end=e, diameter=line_dia, level_polarity=polarity)

    if "arc" in cls_name:
        s = _pick_point(obj, ["start", "p1", "from_", "a"])
        e = _pick_point(obj, ["end", "p2", "to", "b"])
        c = _pick_point(obj, ["center", "c", "origin"])
        if s is None and all(hasattr(obj, name) for name in ("x1", "y1")):
            s = (float(getattr(obj, "x1")), float(getattr(obj, "y1")))
        if e is None and all(hasattr(obj, name) for name in ("x2", "y2")):
            e = (float(getattr(obj, "x2")), float(getattr(obj, "y2")))
        if s is None or e is None or c is None:
            return None
        direction = "clockwise" if _obj_clockwise(obj) else "counterclockwise"
        return Arc(
            start=s,
            end=e,
            center=c,
            direction=direction,
            diameter=_obj_width(obj),
            level_polarity=polarity,
        )

    if "flash" in cls_name or "circle" in cls_name or "drill" in cls_name:
        pos = _pick_point(obj, ["position", "center", "point", "p"])
        if pos is None and all(hasattr(obj, name) for name in ("x", "y")):
            pos = (float(getattr(obj, "x")), float(getattr(obj, "y")))
        if pos is None:
            return None
        aperture = getattr(obj, "aperture", None)
        apt_name = aperture.__class__.__name__.lower() if aperture is not None else ""
        if "aperturemacroinstance" in apt_name and aperture is not None and hasattr(aperture, "flash"):
            try:
                graphic = list(aperture.flash(pos[0], pos[1]))
                out: list[Any] = []
                for gp in graphic:
                    ap = _adapt_gerbonara_graphic_primitive(gp, polarity)
                    if ap is None:
                        continue
                    if isinstance(ap, list):
                        out.extend(ap)
                    else:
                        out.append(ap)
                if out:
                    collapsed = _collapse_simple_macro_primitives(out, flash_pos=pos, polarity=polarity)
                    return collapsed if collapsed is not None else out
            except Exception:
                pass
        if "rectangle" in apt_name and hasattr(aperture, "w") and hasattr(aperture, "h"):
            w = float(getattr(aperture, "w"))
            h = float(getattr(aperture, "h"))
            if w > 0.0 and h > 0.0:
                return RoundRectangle(position=pos, width=w, height=h, radius=0.0, level_polarity=polarity)
        if "obround" in apt_name and hasattr(aperture, "w") and hasattr(aperture, "h"):
            w = float(getattr(aperture, "w"))
            h = float(getattr(aperture, "h"))
            if w > 0.0 and h > 0.0:
                return Obround(position=pos, width=w, height=h, level_polarity=polarity)

        diameter = _obj_diameter(obj)
        if diameter <= 0.0:
            return None
        if kind == "excellon":
            return Drill(position=pos, diameter=diameter, level_polarity=polarity)
        return Circle(position=pos, diameter=diameter, level_polarity=polarity)

    return None


def _adapt_gerbonara_graphic_primitive(obj: Any, fallback_polarity: str) -> Any | list[Any] | None:
    cls = obj.__class__.__name__.lower()
    polarity = fallback_polarity
    if hasattr(obj, "polarity_dark"):
        try:
            polarity = "dark" if bool(getattr(obj, "polarity_dark")) else "clear"
        except Exception:
            polarity = fallback_polarity

    if "circle" in cls and hasattr(obj, "x") and hasattr(obj, "y") and hasattr(obj, "r"):
        try:
            x = float(getattr(obj, "x"))
            y = float(getattr(obj, "y"))
            r = float(getattr(obj, "r"))
            if r > 0.0:
                return Circle(position=(x, y), diameter=r * 2.0, level_polarity=polarity)
        except Exception:
            return None

    if "rectangle" in cls and all(hasattr(obj, name) for name in ("x", "y", "w", "h")):
        try:
            x = float(getattr(obj, "x"))
            y = float(getattr(obj, "y"))
            w = float(getattr(obj, "w"))
            h = float(getattr(obj, "h"))
            rot = float(getattr(obj, "rotation", 0.0) or 0.0)
            if abs(rot) <= 1e-9:
                return RoundRectangle(position=(x, y), width=w, height=h, radius=0.0, level_polarity=polarity)
            verts = _rotated_rect_vertices(x, y, w, h, rot)
            return Polygon(vertices=verts, flashed=True, level_polarity=polarity)
        except Exception:
            return None

    if "arcpoly" in cls and hasattr(obj, "outline"):
        try:
            outline = list(getattr(obj, "outline") or [])
            verts: list[tuple[float, float]] = []
            for pt in outline:
                if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    verts.append((float(pt[0]), float(pt[1])))
            if len(verts) >= 3:
                return Polygon(vertices=verts, flashed=True, level_polarity=polarity)
        except Exception:
            return None
    return None


def _rotated_rect_vertices(cx: float, cy: float, w: float, h: float, rotation: float) -> list[tuple[float, float]]:
    hw = w * 0.5
    hh = h * 0.5
    corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    if abs(rotation) <= 1e-12:
        verts = [(cx + dx, cy + dy) for dx, dy in corners]
    else:
        # gerbonara graphic primitives expose rectangle rotation in radians.
        ang = rotation if abs(rotation) <= (2.0 * math.pi + 1e-6) else math.radians(rotation)
        ca = math.cos(ang)
        sa = math.sin(ang)
        verts = []
        for dx, dy in corners:
            rx = (dx * ca) - (dy * sa)
            ry = (dx * sa) + (dy * ca)
            verts.append((cx + rx, cy + ry))
    verts.append(verts[0])
    return verts


def _collapse_simple_macro_primitives(
    primitives: list[Any],
    *,
    flash_pos: tuple[float, float] | None,
    polarity: str,
) -> Any | None:
    if not primitives:
        return None
    if len(primitives) == 1:
        return primitives[0]

    classes = {p.__class__.__name__.lower() for p in primitives}
    if not classes.issubset({"circle", "roundrectangle"}):
        return None

    rects = [
        p
        for p in primitives
        if p.__class__.__name__.lower() == "roundrectangle"
        and abs(float(getattr(p, "radius", 0.0))) <= 1e-9
    ]
    circles = [p for p in primitives if p.__class__.__name__.lower() == "circle"]
    if len(rects) != 1 or not circles:
        return None

    rect = rects[0]
    try:
        cx, cy = float(rect.position[0]), float(rect.position[1])
        w = float(rect.width)
        h = float(rect.height)
    except Exception:
        return None
    if w <= 0.0 or h <= 0.0:
        return None

    if flash_pos is not None:
        if not _point_close((cx, cy), (float(flash_pos[0]), float(flash_pos[1])), tol=1e-3):
            return None

    try:
        diameters = [float(c.diameter) for c in circles]
        circle_pts = [(float(c.position[0]), float(c.position[1])) for c in circles]
    except Exception:
        return None
    if any(d <= 0.0 for d in diameters):
        return None
    d0 = diameters[0]
    if any(abs(d - d0) > 1e-4 for d in diameters[1:]):
        return None
    r = d0 * 0.5
    tol = max(1e-3, r * 0.02)

    obround_expected = _expected_obround_centers(cx, cy, w, h, r, tol=tol)
    if obround_expected is not None and len(circle_pts) == 2:
        if _same_point_set(circle_pts, obround_expected, tol):
            return Obround(position=(cx, cy), width=w, height=h, level_polarity=polarity)

    if len(circle_pts) == 4:
        roundrect_expected = [
            (cx - (w * 0.5 - r), cy - (h * 0.5 - r)),
            (cx + (w * 0.5 - r), cy - (h * 0.5 - r)),
            (cx + (w * 0.5 - r), cy + (h * 0.5 - r)),
            (cx - (w * 0.5 - r), cy + (h * 0.5 - r)),
        ]
        if _same_point_set(circle_pts, roundrect_expected, tol):
            return RoundRectangle(position=(cx, cy), width=w, height=h, radius=r, level_polarity=polarity)

    return None


def _expected_obround_centers(
    cx: float,
    cy: float,
    w: float,
    h: float,
    r: float,
    *,
    tol: float,
) -> list[tuple[float, float]] | None:
    if abs(w - (2.0 * r)) <= tol and h >= (2.0 * r - tol):
        dy = (h * 0.5) - r
        return [(cx, cy - dy), (cx, cy + dy)]
    if abs(h - (2.0 * r)) <= tol and w >= (2.0 * r - tol):
        dx = (w * 0.5) - r
        return [(cx - dx, cy), (cx + dx, cy)]
    return None


def _same_point_set(a: list[tuple[float, float]], b: list[tuple[float, float]], tol: float) -> bool:
    if len(a) != len(b):
        return False
    unused = list(b)
    for p in a:
        match_idx = -1
        for idx, q in enumerate(unused):
            if _point_close(p, q, tol=tol):
                match_idx = idx
                break
        if match_idx < 0:
            return False
        unused.pop(match_idx)
    return not unused


def _point_close(a: tuple[float, float], b: tuple[float, float], *, tol: float) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def _pick_point(obj: Any, names: list[str]) -> tuple[float, float] | None:
    for name in names:
        if not hasattr(obj, name):
            continue
        value = getattr(obj, name)
        point = _to_point_tuple(value)
        if point is not None:
            return point
    return None


def _to_point_tuple(value: Any) -> tuple[float, float] | None:
    if value is None:
        return None
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        try:
            return float(value[0]), float(value[1])
        except Exception:
            return None
    for x_name, y_name in [("x", "y"), ("real", "imag"), ("x_mm", "y_mm")]:
        if hasattr(value, x_name) and hasattr(value, y_name):
            try:
                return float(getattr(value, x_name)), float(getattr(value, y_name))
            except Exception:
                continue
    return None


def _obj_width(obj: Any) -> float:
    for name in ("width", "diameter"):
        if hasattr(obj, name):
            try:
                value = float(getattr(obj, name))
                if value > 0.0:
                    return value
            except Exception:
                pass
    aperture = getattr(obj, "aperture", None)
    if aperture is not None:
        for name in ("diameter", "width"):
            if hasattr(aperture, name):
                try:
                    value = float(getattr(aperture, name))
                    if value > 0.0:
                        return value
                except Exception:
                    pass
        if hasattr(aperture, "w") and hasattr(aperture, "h"):
            try:
                w = float(getattr(aperture, "w"))
                h = float(getattr(aperture, "h"))
                if w > 0.0 and h > 0.0:
                    return min(w, h)
            except Exception:
                pass
        if hasattr(aperture, "equivalent_width"):
            fn = getattr(aperture, "equivalent_width")
            if callable(fn):
                try:
                    v = float(fn())
                    if v > 0.0:
                        return v
                except Exception:
                    pass
    return 0.0


def _obj_diameter(obj: Any) -> float:
    width = _obj_width(obj)
    if width > 0.0:
        return width
    for name in ("diameter", "drill_dia"):
        if hasattr(obj, name):
            try:
                value = float(getattr(obj, name))
                if value > 0.0:
                    return value
            except Exception:
                pass
    return 0.0


def _obj_clockwise(obj: Any) -> bool:
    if hasattr(obj, "clockwise"):
        try:
            return bool(getattr(obj, "clockwise"))
        except Exception:
            pass
    direction = str(getattr(obj, "direction", "")).lower()
    return direction.startswith("cw") or direction == "clockwise"


def _gerbonara_polarity(obj: Any) -> str:
    polarity = str(getattr(obj, "polarity_dark", "")).lower()
    if polarity in {"true", "1"}:
        return "dark"
    if polarity in {"false", "0"}:
        return "clear"
    raw = str(getattr(obj, "polarity", getattr(obj, "level_polarity", "dark"))).lower()
    if "clear" in raw:
        return "clear"
    return "dark"


def _primitive_bbox(primitive: Any) -> tuple[float, float, float, float] | None:
    if hasattr(primitive, "vertices"):
        try:
            verts = list(getattr(primitive, "vertices") or [])
            if len(verts) >= 3:
                xs = [float(v[0]) for v in verts]
                ys = [float(v[1]) for v in verts]
                return (min(xs), min(ys), max(xs), max(ys))
        except Exception:
            pass
    if hasattr(primitive, "position") and hasattr(primitive, "diameter"):
        px, py = primitive.position
        r = float(primitive.diameter) * 0.5
        return (px - r, py - r, px + r, py + r)
    if hasattr(primitive, "start") and hasattr(primitive, "end"):
        sx, sy = primitive.start
        ex, ey = primitive.end
        pad = float(getattr(primitive, "diameter", 0.0)) * 0.5
        return (
            min(sx, ex) - pad,
            min(sy, ey) - pad,
            max(sx, ex) + pad,
            max(sy, ey) + pad,
        )
    if hasattr(primitive, "center") and hasattr(primitive, "start"):
        cx, cy = primitive.center
        sx, sy = primitive.start
        r = math.hypot(sx - cx, sy - cy) + (float(getattr(primitive, "diameter", 0.0)) * 0.5)
        return (cx - r, cy - r, cx + r, cy + r)
    return None


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
