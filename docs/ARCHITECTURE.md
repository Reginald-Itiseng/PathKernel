# Architecture

## Overview
PathKernel uses a lightweight desktop architecture focused on fast iteration:

- `app/core/io.py`: parse/import CAM files into layer model objects
- `app/core/project.py`: in-memory project/layer state
- `app/ui/main_window.py`: workflow orchestration and UI composition
- `app/ui/widgets.py`: interactive canvas and direct primitive rendering

## Data Flow
1. User imports files from menu actions.
2. `io.parse_file()` parses Gerber/Excellon into a `Layer`.
3. `Project` stores all layers and visibility state.
4. `MainWindow._rebuild_scene()` rebuilds scene items from `Project`.
5. `CAMLayerItem` draws normalized primitive shapes directly in Qt.

## Why Direct Rendering
The project currently avoids an SVG/PNG artifact pipeline for viewport drawing.
Direct shape rendering removes repeated file I/O and expensive SVG item setup,
which keeps pan/zoom interactions responsive for typical boards.

## Coordinate System
- Gerber coordinates are treated as board-space units (usually mm).
- Qt scene uses +Y downward, so geometry conversion flips Y (`-y`) on import.
- Bounding rectangles are computed from generated shapes to reduce clipping.

## Extension Points
- Add missing primitive handlers in `widgets._append_primitive_shapes()`.
- Add renderer options by swapping `GraphicsCanvas.add_layer()` strategy.
- Add project persistence by serializing `Project.layers` metadata.

