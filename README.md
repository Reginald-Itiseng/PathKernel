# PathKernel

PathKernel is a Python desktop CAM/viewer scaffold for PCB fabrication files. This milestone supports importing and viewing Gerber RS-274X and Excellon drill files.

## Features (v0.1)

- PySide6 desktop UI
- Import Gerber files (`.GTL`, `.GBL`, `.GTO`, `.GBO`, `.GTS`, `.GBS`, `.GKO`, `.GM1`, `.GBR`, `.ART`, `.PHO`)
- Import Excellon drill files (`.DRL`, `.TXT`, `.XLN`, `.DRD`)
- Open individual files or scan a folder
- Layer list with visibility toggles and per-layer color
- Canvas with zoom (mouse wheel), pan (middle mouse drag), and fit-to-view
- Status bar cursor coordinates (mm, approximate) and zoom level
- Per-layer metadata panel
- Render cache in `.cache/renders/`

## Requirements

- Python 3.11+
- Qt runtime support (provided through `PySide6` wheel)

## Install

```bash
pip install -e .
```

For tests:

```bash
pip install -e .[dev]
```

## Run

```bash
python -m app.main
```

Or via script entrypoint:

```bash
pathkernel
```

## Quickstart

1. Launch the app.
2. Use `File > Open Gerber(s)` to load one or more Gerber layers.
3. Use `File > Open Excellon` for drill files.
4. Use `File > Open Folder` to auto-import known CAM extensions.
5. Toggle layer visibility in the left dock.
6. Middle-drag to pan, wheel to zoom, click `Fit` to frame all visible items.

## Project Structure

```text
PathKernel/
  app/
    __init__.py
    main.py
    ui/
      __init__.py
      main_window.py
      widgets.py
    core/
      __init__.py
      io.py
      project.py
      render.py
      units.py
    assets/
  tests/
    test_io.py
    test_project.py
  pyproject.toml
  README.md
```

## Notes and Limitations

- This is an import/view milestone only. No toolpath generation or CAM operations yet.
- Rendering relies on `pcb-tools` + Cairo backend support; if SVG export fails, the app falls back to PNG artifacts.
- Coordinate display is scene-based and currently approximate in mm.
- No bundled sample CAM files are included. Use your own test Gerber/Excellon outputs.

## Logging

Logging is enabled at INFO level in `app/main.py`.
Import/render failures are surfaced with Qt error dialogs and logs.
