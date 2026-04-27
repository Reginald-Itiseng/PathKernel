from __future__ import annotations

from dataclasses import asdict
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

    if task_name == "cutout_extract_loops":
        source_layer = deserialize_layer(dict(payload["source_layer"]))
        loops = extract_cutout_loops(source_layer, log=emit_log)
        return {"loops": [_polygon_payload(poly) for poly in loops]}

    if task_name == "cutout_generate":
        source_layer = deserialize_layer(dict(payload["source_layer"]))
        params = CutoutParams(**dict(payload["params"]))
        layer = build_cutout_toolpath_layer(source_layer, Project(), params, log=emit_log)
        return {"generated_layer": serialize_layer(layer)}

    if task_name == "drill_generate":
        source_layer = deserialize_layer(dict(payload["source_layer"]))
        params = DrillToolpathParams(**dict(payload["params"]))
        layer = build_drill_toolpath_layer(source_layer, Project(), params, log=emit_log)
        return {"generated_layer": serialize_layer(layer)}

    if task_name == "hatching_generate":
        source_layer = deserialize_layer(dict(payload["source_layer"]))
        keepout_layers = [deserialize_layer(dict(item)) for item in list(payload.get("keepout_layers", []))]
        board_layers = [deserialize_layer(dict(item)) for item in list(payload.get("board_layers", []))]
        params = HatchingParams(**dict(payload["params"]))
        layer = build_hatching_toolpath_layer(
            source_layer,
            Project(),
            params,
            keepout_layers=keepout_layers,
            board_layers=board_layers,
            log=emit_log,
        )
        return {"generated_layer": serialize_layer(layer)}

    if task_name == "surfacing_generate":
        source_layer = deserialize_layer(dict(payload["source_layer"]))
        params = SurfacingParams(**dict(payload["params"]))
        layer = build_surfacing_toolpath_layer(source_layer, Project(), params, log=emit_log)
        return {"generated_layer": serialize_layer(layer)}

    raise ValueError(f"Unknown task: {task_name}")


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
