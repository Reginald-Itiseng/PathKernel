# Debugging Guide

## Layer Metadata Diagnostics
Select a layer in the UI metadata dock and inspect:

- `primitive_count`
- `shape_count`
- `unsupported_primitives`
- `unsupported_samples`

These values are populated by `widgets._extract_shapes()`.

## Typical Issues

### Missing Geometry
- Check `unsupported_primitives`.
- Add handlers in `_append_primitive_shapes()` for reported classes.

### Arc Looks Wrong
- Verify `Arc` exists in primitive list.
- Check sweep selection logic in `_arc_points()`.
- Compare selected shape bounds with primitive `bounding_box`.

### Pad Artifacts
- Focus on `AMGroup` decomposition.
- Inspect whether helper primitives should be merged, ignored, or treated as cutouts.

## Repro Workflow
1. Close app.
2. Apply code change.
3. Relaunch with `python -m app.main`.
4. Re-import the same files.
5. Capture metadata + screenshot for regression triage.

