# Architecture

## Overview
PathKernel uses a model-driven desktop architecture with renderer swappability and
process-isolated geometry generation.

Primary modules:

- `app/core/project.py`: project model (`Project`, `Layer`, transforms, tool library)
- `app/core/io.py`: CAM file detection/import (Gerber + Excellon)
- `app/core/geometry.py`: primitive -> draw-shape normalization
- `app/core/isolation.py`: isolation toolpath generation
- `app/core/cutout.py`: cutout loop detection and compensated toolpath generation
- `app/core/drilling.py`: drill strategy A generation (plunge/boring)
- `app/core/hpgl.py`: centerline HPGL export
- `app/core/background_tasks.py`: multiprocessing task entrypoint + queue protocol
- `app/ui/main_window.py`: workflow orchestration, menu actions, docks, task lifecycle
- `app/ui/pyqtgraph_canvas.py`: high-performance default renderer
- `app/ui/widgets.py`: Qt fallback renderer (`QGraphicsView`)

## Runtime Data Flow
1. User imports Gerber/Excellon files from File menu.
2. `io.import_files(...)` creates `Layer` objects with parsed primitives and metadata.
3. `Project.layers` holds source layers and derived/generated layers.
4. Renderer requests geometry via `build_layer_geometry(layer)` on demand.
5. Click-inspection uses renderer-side hit structures mapped back to layer/primitive metadata.

## Task Isolation Model
Long-running generation tasks run in worker processes:

- UI starts task through `MainWindow._run_process_task(...)`.
- Worker calls `run_task_process_entry(...)`.
- Queue messages stream `log`, `result`, or `error`.
- UI applies decoded result on main thread only (safe for Qt objects).

This keeps viewport interactions responsive during heavy geometry calculations.

## Renderer Model
Two renderers share the same geometry model:

- `PyQtGraphCanvas` (default): flattened toolpath rendering for fast pan/zoom.
- `GraphicsCanvas` (fallback): direct `QGraphicsItem` rendering for compatibility.

Both expose compatible API methods (`add_layer`, `remove_layer`, `fit_scene`,
`set_*_view_mode`, `inspect_at`), so `MainWindow` can switch renderers without changing
workflow logic.

## Transform Policy
Source primitives are immutable. Transforms are applied at draw/export/generation boundaries:

- Layer transform fields: `offset_x_mm`, `offset_y_mm`, `rotation_deg`, `mirror_x`, `mirror_y`
- Order: `mirror -> rotate -> translate`
- Same transform convention is used by cutout/isolation/drill generation and HPGL export.

## Tooling Model
Tool definitions are global (not project-specific), while selected tool assignments are
process-specific:

- Tool library persistence: app settings (global)
- Selected tools persistence: app settings (global defaults)
- Generated layers capture effective parameters in layer metadata for traceability
  (tool diameter, speed, depth, strategy, compensation, etc.).
