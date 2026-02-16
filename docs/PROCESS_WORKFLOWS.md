# Process Workflows

This document summarizes the key production workflows in PathKernel and points to the
responsible code paths.

## 1) Import And Layer Creation

Entry points:

- `File > Open Gerber(s)`
- `File > Open Excellon`
- `File > Open Folder`

Flow:

1. `app/core/io.py::import_files(...)` parses files and creates `Layer` objects.
2. `MainWindow._import_paths(...)` appends layers to `Project`.
3. `MainWindow._rebuild_scene(...)` repaints renderer and rebuilds layer tree.

Important metadata:

- `kind` (`gerber` / `excellon` / `geometry`)
- `role` (`top`, `bottom`, `holes`, `cutout`, etc.)
- parser-derived primitive counts and bounds.

## 2) Viewport Rendering

Common shape pipeline:

1. `build_layer_geometry(layer)` converts parser primitives to normalized draw shapes.
2. Active renderer (`PyQtGraphCanvas` or `GraphicsCanvas`) draws those shapes.
3. Click-inspection uses renderer-side hit maps and attached primitive metadata.

Performance notes:

- PyQtGraph flattens generated toolpath layers into compact curve items.
- Non-flatten-safe geometry stays in path-based rendering to preserve fidelity.

## 3) Isolation Generation

Entry:

- `Tools > Generate Isolation Geometry`

Flow:

1. UI collects parameters from Selected Tools + prompts.
2. `MainWindow._run_process_task(...)` starts `isolation_generate` worker task.
3. `app/core/isolation.py::build_isolation_layer(...)` computes buffered toolpaths.
4. Generated layer (`metadata.kind = isolation`) is appended to project and displayed.

Important logic:

- Dynamic effective diameter for conical tools (`calculate_coppercam_params`).
- Pass spacing/hatching margin management.
- Extra pad contours with keepout awareness.

## 4) Cutout Planner And Cutout Generation

Entry:

- `Tools > Generate Cutout Toolpath` (or cutout planner dock toggle)

Flow:

1. Planner extracts loops via `cutout_extract_loops`.
2. Loop compensation (inside/outside/on-path) is configured per loop.
3. Preview generation runs asynchronously (`cutout_generate`) and is shown as preview layer.
4. Final Generate writes a real cutout toolpath layer.

Preview rules:

- Preview is visible only while planner is open.
- Preview is locked when a generated cutout layer exists.
- Reopen planner to re-enable preview after lock.

## 5) Drill Generation (Strategy A)

Entry:

- `Tools > Generate Drill Toolpath`

Current strategy:

- One selected drill tool for all holes.
- If hole ~= tool diameter: center plunge only.
- If hole > tool diameter: center plunge + concentric boring arcs.
- If hole < tool diameter: skipped with metadata warning count.

Implementation:

- `app/core/drilling.py::build_drill_toolpath_layer(...)`
- Result layer tagged `metadata.kind = drill_toolpath`.

## 6) Tool Library And Selected Tools

Menus:

- `Parameters > Tool library...`
- `Parameters > Selected tools...`

Behavior:

- Tool library is global and persists across projects.
- Selected process tools (engraving/hatching/cutout/drill/etc.) are also persisted globally.
- Generated layers store applied values in metadata for auditability.

## 7) HPGL Export

Entry:

- `File > Export Toolpaths to HPGL...`

Flow:

1. Collect generated toolpath layers (`is_toolpath_layer`).
2. Convert primitives to centerline polylines in project coordinates.
3. Emit Bungard-compatible HPGL (`IN`, `PA`, `PU/PD`, `SP`, `VS`, `AA`, footer).
4. Export each toolpath layer to its own `.plt` file.

Key rules:

- Coordinate scaling: `int(round(mm * units_per_mm))` (`40` by default).
- Optional coordinate normalization to origin.
- Batched PD coordinate emission for smoother controller look-ahead.

## 8) Project Save/Load

Project file:

- Saves imported/generated layers, transforms, metadata, and viewport-related state.
- Reopening should reconstruct layer positions and derived geometry alignment.

Global settings (outside project):

- Tool library and selected tool assignments.
- Last opened project path.

## 9) Debugging And Validation

Geometry inspection:

- Click to inspect primitive metadata in Metadata panel.
- Enable `View > Debug: Dump Clicked Geometry` to append detailed logs to `debug/geometry_dump.log`.

Recommended validation order:

1. Import alignment check.
2. Isolation generation and view-mode toggles.
3. Drill and cutout generation.
4. HPGL export and machine dry-run.
