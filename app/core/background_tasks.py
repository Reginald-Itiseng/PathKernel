from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
import traceback
from typing import Any, Callable

from app.core.cutout import CutoutParams, build_cutout_toolpath_layer, extract_cutout_loops
from app.core.drilling import DrillToolpathParams, build_drill_toolpath_layer
from app.core.hatching import HatchingParams, build_hatching_toolpath_layer
from app.core.isolation import IsolationParams, build_isolation_layer
from app.core.surfacing import SurfacingParams, build_surfacing_toolpath_layer
from app.core.project import Project
from app.core.project_store import deserialize_layer, serialize_layer


def run_task_process_entry(task_name: str, payload: dict[str, Any], out_queue) -> None:
    """Process entrypoint for long-running geometry tasks.

    Queue protocol (dict messages):
    - {"type": "log", "text": "..."} for progress streaming.
    - {"type": "result", "result": {...}} for success payload.
    - {"type": "error", "message": "...", "traceback": "..."} for failures.

    The UI side (`MainWindow`) relies on this exact shape when polling.
    """

    def emit_log(text: str) -> None:
        out_queue.put({"type": "log", "text": str(text)})

    try:
        result = _run_task(task_name, payload, emit_log)
    except Exception as exc:
        out_queue.put(
            {
                "type": "error",
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
        return
    out_queue.put({"type": "result", "result": result})


def _run_task(task_name: str, payload: dict[str, Any], emit_log: Callable[[str], None]) -> dict[str, Any]:
    # Task handlers are intentionally stateless: payload in, serializable payload out.
    # This keeps multiprocessing boundaries simple and avoids sharing Qt/Shapely objects
    # between processes.
    if task_name == "isolation_generate":
        source_layer = deserialize_layer(dict(payload["source_layer"]))
        params = IsolationParams(**dict(payload["params"]))
        layer = build_isolation_layer(source_layer, Project(), params, log=emit_log)
        return {"generated_layer": serialize_layer(layer)}

    if task_name == "isolation_generate_multi":
        source_layers = [deserialize_layer(dict(item)) for item in list(payload.get("source_layers", []))]
        params = IsolationParams(**dict(payload["params"]))
        layers = [
            build_isolation_layer(layer, Project(), params, log=emit_log)
            for layer in source_layers
        ]
        layer = _merge_generated_layers(layers, name_suffix="iso", meta_kind="isolation")
        return {"generated_layer": serialize_layer(layer)}

    if task_name == "cutout_extract_loops":
        source_layer = deserialize_layer(dict(payload["source_layer"]))
        loops = extract_cutout_loops(source_layer, log=emit_log)
        return {"loops": [_polygon_payload(poly) for poly in loops]}

    if task_name == "cutout_extract_loops_multi":
        source_layers = [deserialize_layer(dict(item)) for item in list(payload.get("source_layers", []))]
        out = []
        for source_pos, source_layer in enumerate(source_layers):
            loops = extract_cutout_loops(source_layer, log=emit_log)
            for poly in loops:
                item = _polygon_payload(poly)
                item["source_pos"] = int(source_pos)
                item["source_name"] = str(getattr(source_layer, "name", ""))
                out.append(item)
        return {"loops": out}

    if task_name == "cutout_generate":
        source_layer = deserialize_layer(dict(payload["source_layer"]))
        params = CutoutParams(**dict(payload["params"]))
        layer = build_cutout_toolpath_layer(source_layer, Project(), params, log=emit_log)
        return {"generated_layer": serialize_layer(layer)}

    if task_name == "cutout_generate_multi":
        source_layers = [deserialize_layer(dict(item)) for item in list(payload.get("source_layers", []))]
        params_payload = dict(payload["params"])
        comps_by_layer = list(payload.get("loop_compensations_by_layer", []))
        layers = []
        for source_pos, source_layer in enumerate(source_layers):
            layer_params_payload = dict(params_payload)
            if source_pos < len(comps_by_layer) and isinstance(comps_by_layer[source_pos], list):
                if not comps_by_layer[source_pos]:
                    continue
                layer_params_payload["loop_compensations"] = list(comps_by_layer[source_pos])
            params = CutoutParams(**layer_params_payload)
            layers.append(build_cutout_toolpath_layer(source_layer, Project(), params, log=emit_log))
        layer = _merge_generated_layers(layers, name_suffix="cutout", meta_kind="cutout_toolpath")
        return {"generated_layer": serialize_layer(layer)}

    if task_name == "drill_generate":
        source_layer = deserialize_layer(dict(payload["source_layer"]))
        params = DrillToolpathParams(**dict(payload["params"]))
        layer = build_drill_toolpath_layer(source_layer, Project(), params, log=emit_log)
        return {"generated_layer": serialize_layer(layer)}

    if task_name == "drill_generate_multi":
        source_layers = [deserialize_layer(dict(item)) for item in list(payload.get("source_layers", []))]
        params = DrillToolpathParams(**dict(payload["params"]))
        layers = [
            build_drill_toolpath_layer(layer, Project(), params, log=emit_log)
            for layer in source_layers
        ]
        layer = _merge_generated_layers(layers, name_suffix="drill", meta_kind="drill_toolpath")
        return {"generated_layer": serialize_layer(layer)}

    if task_name == "hatching_generate":
        source_layer = deserialize_layer(dict(payload["source_layer"]))
        keepout_layers = [deserialize_layer(dict(item)) for item in list(payload.get("keepout_layers", []))]
        board_layers = [deserialize_layer(dict(item)) for item in list(payload.get("board_layers", []))]
        copper_keepout_layers = [
            deserialize_layer(dict(item))
            for item in list(payload.get("copper_keepout_layers", []))
        ]
        params = HatchingParams(**dict(payload["params"]))
        layer = build_hatching_toolpath_layer(
            source_layer,
            Project(),
            params,
            keepout_layers=keepout_layers,
            board_layers=board_layers,
            copper_keepout_layers=copper_keepout_layers,
            log=emit_log,
        )
        return {"generated_layer": serialize_layer(layer)}

    if task_name == "hatching_generate_multi":
        source_layers = [deserialize_layer(dict(item)) for item in list(payload.get("source_layers", []))]
        keepout_layers = [deserialize_layer(dict(item)) for item in list(payload.get("keepout_layers", []))]
        board_layers = [deserialize_layer(dict(item)) for item in list(payload.get("board_layers", []))]
        copper_keepout_layers = [
            deserialize_layer(dict(item))
            for item in list(payload.get("copper_keepout_layers", []))
        ]
        params = HatchingParams(**dict(payload["params"]))
        layers = [
            build_hatching_toolpath_layer(
                layer,
                Project(),
                params,
                keepout_layers=keepout_layers,
                board_layers=board_layers,
                copper_keepout_layers=copper_keepout_layers,
                log=emit_log,
            )
            for layer in source_layers
        ]
        layer = _merge_generated_layers(layers, name_suffix="hatch", meta_kind="hatching_toolpath")
        return {"generated_layer": serialize_layer(layer)}

    if task_name == "surfacing_generate":
        source_layer = deserialize_layer(dict(payload["source_layer"]))
        params = SurfacingParams(**dict(payload["params"]))
        layer = build_surfacing_toolpath_layer(source_layer, Project(), params, log=emit_log)
        return {"generated_layer": serialize_layer(layer)}

    raise ValueError(f"Unknown task: {task_name}")


def _merge_generated_layers(layers: list[Any], *, name_suffix: str, meta_kind: str):
    valid_layers = [layer for layer in layers if layer is not None]
    if not valid_layers:
        raise ValueError("No generated layers to merge.")

    primitives = []
    source_names: list[str] = []
    min_x = float("inf")
    min_y = float("inf")
    max_x = float("-inf")
    max_y = float("-inf")
    found_bounds = False

    for layer in valid_layers:
        primitives.extend(list(getattr(getattr(layer, "source", None), "primitives", []) or []))
        source_name = str(getattr(layer, "metadata", {}).get("derived_from", "") or getattr(layer, "name", ""))
        if source_name:
            source_names.append(source_name)
        bbox = getattr(layer, "bbox", None)
        if isinstance(bbox, tuple) and len(bbox) == 4:
            try:
                bx0, by0, bx1, by1 = (float(v) for v in bbox)
            except Exception:
                continue
            min_x = min(min_x, bx0)
            min_y = min(min_y, by0)
            max_x = max(max_x, bx1)
            max_y = max(max_y, by1)
            found_bounds = True

    if not primitives:
        raise ValueError("Generated multi-layer toolpath is empty.")

    bounds = ((min_x, max_x), (min_y, max_y)) if found_bounds else None
    bbox = (min_x, min_y, max_x, max_y) if found_bounds else None
    first = valid_layers[0]
    base_name = _shared_name_prefix(source_names) or str(getattr(first, "metadata", {}).get("derived_from", "")) or "multi"
    name = f"{base_name}_{name_suffix}_multi"
    metadata = dict(getattr(first, "metadata", {}) or {})
    metadata.update(
        {
            "name": name,
            "kind": meta_kind,
            "derived_from": ",".join(source_names),
            "source_layer_count": str(len(valid_layers)),
            "source_layer_names": ",".join(source_names),
        }
    )
    return type(first)(
        name=name,
        path=Path(str(getattr(first, "path", ""))),
        kind="geometry",
        source=SimpleNamespace(units="mm", primitives=primitives, bounds=bounds),
        color=str(getattr(first, "color", "#FFFFFF")),
        opacity=float(getattr(first, "opacity", 1.0)),
        role=str(getattr(first, "role", "unassigned")),
        bbox=bbox,
        metadata=metadata,
    )


def _shared_name_prefix(names: list[str]) -> str:
    clean = [str(name).strip() for name in names if str(name).strip()]
    if not clean:
        return ""
    prefix = clean[0]
    for name in clean[1:]:
        limit = min(len(prefix), len(name))
        pos = 0
        while pos < limit and prefix[pos] == name[pos]:
            pos += 1
        prefix = prefix[:pos]
        if not prefix:
            break
    return prefix.rstrip(" _-.") or clean[0]


def _polygon_payload(poly) -> dict[str, Any]:
    exterior = [[float(x), float(y)] for x, y in list(poly.exterior.coords)]
    interiors = []
    for ring in getattr(poly, "interiors", []):
        interiors.append([[float(x), float(y)] for x, y in list(ring.coords)])
    return {"exterior": exterior, "interiors": interiors}


def cutout_params_payload(params: CutoutParams) -> dict[str, Any]:
    return asdict(params)


def isolation_params_payload(params: IsolationParams) -> dict[str, Any]:
    return asdict(params)


def drill_params_payload(params: DrillToolpathParams) -> dict[str, Any]:
    return asdict(params)


def hatching_params_payload(params: HatchingParams) -> dict[str, Any]:
    return asdict(params)


def surfacing_params_payload(params: SurfacingParams) -> dict[str, Any]:
    return asdict(params)
