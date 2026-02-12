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
- Source geometry stays immutable; transforms are applied at `CAMLayerItem` draw-item level.

This keeps rendering fast and avoids geometry mutation bugs.

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

File format remains JSON (`*.pkproj.json`) and is versioned with `"version": 1`.
