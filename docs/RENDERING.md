# Rendering Pipeline

## Shape Normalization
All renderers consume the same `DrawShape` output from `app/core/geometry.py`.

`DrawShape` fields:

- `kind`: `line` or `fill`
- `path`: `QPainterPath` in scene-space convention
- `line_width`: physical width for line strokes
- `clear`: polarity flag (`True` means erase/cutout behavior)
- `info`: primitive metadata for hit-inspection

## Renderer Backends

### PyQtGraph (`app/ui/pyqtgraph_canvas.py`, default)
- Uses `PlotCurveItem` for flattened line-heavy toolpaths.
- Uses fill/path items for solid geometry.
- Maintains toolpath visual groups to switch between:
  - `width` view (effective cut width),
  - `centerline` view (single-line + arrows).
- Builds a lightweight spatial hit grid per layer for fast click-inspection.

### Qt Fallback (`app/ui/widgets.py`)
- Uses `CAMLayerItem` (`QGraphicsItem`) and painter-based draw loop.
- Shares view-mode semantics and click-inspection behavior with PyQtGraph backend.

## Flattening Policy
Only generated toolpath layers are flattened aggressively (`isolation`, `cutout_toolpath`,
`drill_toolpath`), and only when geometry is flatten-safe.

Disconnected segments are preserved using NaN separators in curve arrays to avoid
"spaghetti joins" between independent paths.

## Arc / Curve Handling
Curved primitives are tessellated to short segments before rendering.
Chord tolerance is controlled by constants in `app/core/geometry.py`:

- `SEGMENT_CHORD_MM`
- `MIN_ARC_SEGMENTS`
- `MAX_ARC_SEGMENTS`

Lower chord tolerance increases smoothness, but increases render and export point count.

## Inspection Metadata
Both renderers attach primitive info (type/index/group/polarity/dimensions) to hit entries.
This powers metadata panel click-inspection and debug dump output.
