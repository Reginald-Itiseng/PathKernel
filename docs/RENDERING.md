# Rendering Pipeline

## Current Strategy
The viewport uses direct primitive rendering:

- Each layer is parsed into pcb-tools primitives.
- Primitives are converted into `_DrawShape` objects:
  - `kind`: `fill` or `line`
  - `path`: `QPainterPath` in scene coordinates
  - `line_width`
  - `clear` polarity flag
- `CAMLayerItem.paint()` iterates shapes and draws with Qt painter APIs.

## Primitive Handling Notes
- `Line`/`Slot`: stroked line path.
- `Arc`: tessellated with `_arc_points()` then stroked.
- `Circle`/`Drill`: tessellated polygon fill (FlatCAM-like behavior).
- `Region`: converted to closed fill path from sub-primitives.
- `Outline`: converted to closed fill path.
- `AMGroup`: processes sub-primitives and unions fills to reduce seams.

## Arc Sweep Selection
Arc direction can be ambiguous across files. The implementation:

1. computes both candidate sweeps (CW/CCW),
2. handles full-circle encoding (`start == end`),
3. selects sweep that best matches the primitive-reported bounding box.

This is why arc conversion includes `_arc_bbox_error()`.

## Tuning Constants
Defined in `app/ui/widgets.py`:

- `SEGMENT_CHORD_MM`
- `MIN_ARC_SEGMENTS`
- `MAX_ARC_SEGMENTS`

Lower chord = smoother curves, higher CPU cost.

