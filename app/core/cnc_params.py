from __future__ import annotations

import math


def calculate_coppercam_params(T_dia: float, T_angle: float, D_cut: float) -> dict[str, float]:
    """Compute effective V-bit cutting parameters for PCB isolation.

    Args:
        T_dia: Tool tip diameter (flat end width), in mm.
        T_angle: Full included V-bit angle, in degrees.
        D_cut: Cutting depth, in mm.

    Returns:
        A dict with:
        - effective_radius: radius at the material surface at the given depth.
        - total_path_width: effective diameter (single-pass cut width).
        - hatching_margin: recommended step-over for 50% overlap.
        - trace_compensation: centerline offset compensation to preserve trace width.
    """

    tip_dia = _clean_non_negative_finite(T_dia, "T_dia")
    tool_angle = _clean_non_negative_finite(T_angle, "T_angle")
    cut_depth = _clean_non_negative_finite(D_cut, "D_cut")

    theta = math.radians(tool_angle / 2.0) if tool_angle > 0.0 else 0.0
    base_radius = tip_dia / 2.0
    flare_radius = cut_depth * math.tan(theta) if theta > 0.0 else 0.0
    effective_radius = base_radius + flare_radius
    total_path_width = effective_radius * 2.0

    # 50% overlap means step-over equals radius (half of effective diameter).
    hatching_margin = effective_radius
    # Offset compensation equals effective tool radius.
    trace_compensation = effective_radius

    return {
        "effective_radius": float(effective_radius),
        "total_path_width": float(total_path_width),
        "hatching_margin": float(hatching_margin),
        "trace_compensation": float(trace_compensation),
    }


def _clean_non_negative_finite(value: float, name: str) -> float:
    try:
        v = float(value)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{name} must be a numeric value.") from exc
    if not math.isfinite(v):
        raise ValueError(f"{name} must be finite.")
    if v < 0.0:
        return 0.0
    return v

