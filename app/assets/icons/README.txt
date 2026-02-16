PCB CAM Icon Pack (Monochrome SVG)
---------------------------------
Format: SVG, stroke = currentColor, no fills (except the 'play' triangle is filled via polygon outline only).
Each file is named: <icon_id>_<size>.svg

Recommended usage (Qt/PySide):
- If you use QtSvg, QIcon can load SVGs directly:
    QIcon("icons/view_zoom_in_16.svg")
- If you prefer raster PNGs, you can batch-convert these SVGs with a tool like:
    rsvg-convert, inkscape, or ImageMagick.

Sizes included:
- 16px: all menu/tool icons
- 20px and 24px: activity sidebar icons
- 12px/14px: small close/delete icons
- 14px/16px: small row actions (delete/add/remove tool)
