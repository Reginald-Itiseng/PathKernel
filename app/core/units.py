from __future__ import annotations

MM_PER_INCH = 25.4


def inch_to_mm(value: float) -> float:
    return value * MM_PER_INCH


def mm_to_inch(value: float) -> float:
    return value / MM_PER_INCH

