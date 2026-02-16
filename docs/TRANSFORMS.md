# Transform And Origin Model

## Coordinate Spaces

- File space: raw values parsed from Gerber/Excellon.
- Project/work space: file space after workspace origin and layer transform.
- Machine space: reserved for future post-processing/CAM setup mapping.

## Current Implementation

- `Project.workspace.origin_x_mm` / `origin_y_mm` define global work origin.
- Each `Layer` stores local transform:
  - `offset_x_mm`, `offset_y_mm`
  - `rotation_deg`
  - `mirror_x`, `mirror_y`
- Source geometry stays immutable; transforms are applied at render/generation/export boundaries.

This keeps rendering fast and avoids geometry mutation bugs.

## Transform Order (Important)

Every geometry path that consumes layer transforms uses:

1. mirror (`mirror_x`, `mirror_y`)
2. rotate (`rotation_deg`)
3. translate (`offset_x_mm`, `offset_y_mm`)

Using one canonical order avoids layer misalignment between imported geometry and derived
toolpaths.

## Where Transforms Are Applied

- Rendering:
  - `app/ui/pyqtgraph_canvas.py::_apply_layer_item_transform`
  - `app/ui/widgets.py::CAMLayerItem.apply_layer_transform`
- Toolpath generation:
  - Isolation/cutout derive geometry from transformed source shapes.
  - Drill generation transforms hole centers before creating plunge/boring paths.
- Export:
  - HPGL export transforms source primitives to project-space coordinates before emitting moves.

## Undo/Redo Coverage

Undoable commands currently include:

- `SetWorkspaceOriginCommand`
- `SetLayerColorCommand`
- `SetLayerOpacityCommand`

Layer reorder and visibility toggles are still direct model edits and can be moved
to commands later if needed.

## Persistence

Project save/load now stores:

- viewport (`center_x`, `center_y`, `zoom`)
- workspace origin
- per-layer color/opacity/visibility/order
- per-layer transform values
- tool library + selected tools are persisted in app settings (global), not project file.

File format remains JSON (`*.pkproj.json`) and is versioned with `"version": 1`.
