from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.core.project import Layer, ManualEdit, Project, WorkspaceTransform


PROJECT_FILE_VERSION = 1

_PRIMITIVE_ATTRS = (
    "start",
    "end",
    "center",
    "position",
    "vertices",
    "primitives",
    "diameter",
    "width",
    "height",
    "radius",
    "direction",
    "level_polarity",
    "flashed",
    "aperture",
    "bounds",
    "units",
)

_TYPE_CACHE: dict[str, type] = {}


def save_project(path: Path, project: Project) -> None:
    payload = {
        "version": PROJECT_FILE_VERSION,
        "workspace": {
            "origin_x_mm": float(project.workspace.origin_x_mm),
            "origin_y_mm": float(project.workspace.origin_y_mm),
        },
        "layers": [_encode_layer(layer) for layer in project.layers],
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_project(path: Path) -> Project:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if int(data.get("version", 0)) != PROJECT_FILE_VERSION:
        raise ValueError(f"Unsupported project file version: {data.get('version')}")

    workspace_data = data.get("workspace", {}) or {}
    project = Project(
        workspace=WorkspaceTransform(
            origin_x_mm=float(workspace_data.get("origin_x_mm", 0.0)),
            origin_y_mm=float(workspace_data.get("origin_y_mm", 0.0)),
        )
    )
    for layer_data in data.get("layers", []) or []:
        project.layers.append(_decode_layer(layer_data))
    return project


def serialize_layer(layer: Layer) -> dict[str, Any]:
    return _encode_layer(layer)


def deserialize_layer(data: dict[str, Any]) -> Layer:
    return _decode_layer(data)


def _encode_layer(layer: Layer) -> dict[str, Any]:
    file_blob = _embedded_file_payload(layer)
    return {
        "name": layer.name,
        "path": str(layer.path),
        "kind": layer.kind,
        "visible": bool(layer.visible),
        "color": str(layer.color),
        "opacity": float(layer.opacity),
        "offset_x_mm": float(layer.offset_x_mm),
        "offset_y_mm": float(layer.offset_y_mm),
        "rotation_deg": float(layer.rotation_deg),
        "mirror_x": bool(layer.mirror_x),
        "mirror_y": bool(layer.mirror_y),
        "role": str(layer.role),
        "primitive_overrides": layer.primitive_overrides,
        "manual_edits": [_encode_manual_edit(edit) for edit in layer.manual_edits],
        "bbox": list(layer.bbox) if layer.bbox is not None else None,
        "rendered_artifact_path": str(layer.rendered_artifact_path) if layer.rendered_artifact_path else None,
        "metadata": dict(layer.metadata),
        "source": _encode_source(layer.source),
        "embedded_file": file_blob,
    }


def _decode_layer(data: dict[str, Any]) -> Layer:
    source = _decode_source(data.get("source", {}))
    embedded_file = data.get("embedded_file")
    if isinstance(embedded_file, dict):
        setattr(source, "_embedded_file", embedded_file)
    bbox_raw = data.get("bbox")
    bbox = None
    if isinstance(bbox_raw, list) and len(bbox_raw) == 4:
        bbox = (float(bbox_raw[0]), float(bbox_raw[1]), float(bbox_raw[2]), float(bbox_raw[3]))
    edits = [_decode_manual_edit(item) for item in (data.get("manual_edits", []) or [])]
    primitive_overrides: dict[int, dict[str, float | str]] = {}
    for key, value in dict(data.get("primitive_overrides", {})).items():
        try:
            idx = int(key)
        except Exception:
            continue
        if isinstance(value, dict):
            primitive_overrides[idx] = {str(k): v for k, v in value.items()}

    return Layer(
        name=str(data.get("name", "layer")),
        path=Path(str(data.get("path", ""))),
        kind=str(data.get("kind", "unknown")),
        source=source,
        visible=bool(data.get("visible", True)),
        color=str(data.get("color", "#1976d2")),
        opacity=float(data.get("opacity", 1.0)),
        offset_x_mm=float(data.get("offset_x_mm", 0.0)),
        offset_y_mm=float(data.get("offset_y_mm", 0.0)),
        rotation_deg=float(data.get("rotation_deg", 0.0)),
        mirror_x=bool(data.get("mirror_x", False)),
        mirror_y=bool(data.get("mirror_y", False)),
        role=str(data.get("role", "unassigned")),
        primitive_overrides=primitive_overrides,
        manual_edits=edits,
        bbox=bbox,
        rendered_artifact_path=(
            Path(str(data.get("rendered_artifact_path")))
            if data.get("rendered_artifact_path")
            else None
        ),
        metadata={str(k): str(v) for k, v in dict(data.get("metadata", {})).items()},
    )


def _encode_manual_edit(edit: ManualEdit) -> dict[str, Any]:
    return {
        "kind": edit.kind,
        "x_mm": edit.x_mm,
        "y_mm": edit.y_mm,
        "x2_mm": edit.x2_mm,
        "y2_mm": edit.y2_mm,
        "diameter_mm": edit.diameter_mm,
        "width_mm": edit.width_mm,
        "height_mm": edit.height_mm,
        "shape": edit.shape,
        "corner_radius_mm": edit.corner_radius_mm,
        "hole_diameter_mm": edit.hole_diameter_mm,
    }


def _decode_manual_edit(data: dict[str, Any]) -> ManualEdit:
    return ManualEdit(
        kind=str(data.get("kind", "pad")),
        x_mm=float(data.get("x_mm", 0.0)),
        y_mm=float(data.get("y_mm", 0.0)),
        x2_mm=float(data.get("x2_mm", 0.0)),
        y2_mm=float(data.get("y2_mm", 0.0)),
        diameter_mm=float(data.get("diameter_mm", 0.0)),
        width_mm=float(data.get("width_mm", 0.0)),
        height_mm=float(data.get("height_mm", 0.0)),
        shape=str(data.get("shape", "circle")),
        corner_radius_mm=float(data.get("corner_radius_mm", 0.0)),
        hole_diameter_mm=float(data.get("hole_diameter_mm", 0.0)),
    )


def _encode_source(source: Any) -> dict[str, Any]:
    return {
        "units": str(getattr(source, "units", "mm")),
        "bounds": _encode_value(getattr(source, "bounds", None)),
        "primitives": [_encode_primitive(p) for p in (getattr(source, "primitives", []) or [])],
    }


def _decode_source(data: dict[str, Any]) -> Any:
    return SimpleNamespace(
        units=str(data.get("units", "mm")),
        bounds=_decode_value(data.get("bounds")),
        primitives=[_decode_primitive(p) for p in (data.get("primitives", []) or [])],
    )


def _encode_primitive(primitive: Any) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for attr in _iter_attr_names(primitive):
        if not hasattr(primitive, attr):
            continue
        try:
            value = getattr(primitive, attr)
        except Exception:
            continue
        if callable(value):
            continue
        attrs[attr] = _encode_value(value)
    return {
        "class_name": primitive.__class__.__name__,
        "attrs": attrs,
    }


def _decode_primitive(data: dict[str, Any]) -> Any:
    class_name = str(data.get("class_name", "Primitive"))
    cls = _dynamic_type(class_name)
    obj = cls()
    for key, value in dict(data.get("attrs", {})).items():
        setattr(obj, str(key), _decode_value(value))
    return obj


def _iter_attr_names(obj: Any) -> list[str]:
    names: set[str] = set(_PRIMITIVE_ATTRS)
    slots = getattr(obj, "__slots__", ())
    if isinstance(slots, str):
        names.add(slots)
    else:
        names.update(str(s) for s in slots if s)
    obj_dict = getattr(obj, "__dict__", {})
    if isinstance(obj_dict, dict):
        names.update(str(k) for k in obj_dict.keys())
    return sorted(n for n in names if n and not n.startswith("_"))


def _encode_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return {"__path__": str(value)}
    if isinstance(value, complex):
        return {"__complex__": [float(value.real), float(value.imag)]}
    if isinstance(value, dict):
        return {"__dict__": {str(k): _encode_value(v) for k, v in value.items()}}
    if isinstance(value, (list, tuple, set)):
        return {"__list__": [_encode_value(v) for v in value]}
    return {"__object__": _encode_primitive(value)}


def _decode_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if "__path__" in value:
        return Path(str(value["__path__"]))
    if "__complex__" in value:
        pair = value["__complex__"]
        return complex(float(pair[0]), float(pair[1]))
    if "__dict__" in value:
        return {str(k): _decode_value(v) for k, v in dict(value["__dict__"]).items()}
    if "__list__" in value:
        return [_decode_value(v) for v in list(value["__list__"])]
    if "__object__" in value:
        return _decode_primitive(value["__object__"])
    return {str(k): _decode_value(v) for k, v in value.items()}


def _dynamic_type(class_name: str) -> type:
    cls = _TYPE_CACHE.get(class_name)
    if cls is not None:
        return cls
    cls = type(class_name, (), {})
    _TYPE_CACHE[class_name] = cls
    return cls


def _embedded_file_payload(layer: Layer) -> dict[str, Any] | None:
    if layer.kind not in {"gerber", "excellon"}:
        return None
    path = Path(layer.path)
    if path.exists() and path.is_file():
        raw = path.read_bytes()
        return {
            "name": path.name,
            "path": str(path),
            "data_b64": base64.b64encode(raw).decode("ascii"),
        }
    cached = getattr(layer.source, "_embedded_file", None)
    if isinstance(cached, dict):
        return cached
    return None
