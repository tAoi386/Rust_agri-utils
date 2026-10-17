"""Stable donor placement for a large planter's central recipient.

Positions are zero based, reading the planter from left to right, top to
bottom. Placement affects the planting instructions, not gene vote weights.
"""

from __future__ import annotations


CORNER_FIRST = (0, 8, 2, 6, 1, 7, 3, 5)
LEGACY_CROSS_FIRST = (1, 3, 5, 7, 0, 2, 6, 8)


def positions_for(count: int, layout: str = "auto") -> tuple[int, ...]:
    """Return stable peripheral slots while keeping the recipient in slot 4."""
    if type(count) is not int or not 0 <= count <= 8:
        raise ValueError("供体株数必须是 0–8 的整数。")
    if not isinstance(layout, str) or layout not in ("auto", "corners", "cross"):
        raise ValueError("种植布局只能为 auto、corners 或 cross。")
    if layout == "auto":
        if count <= 2:
            return (1, 7)[:count]
        return CORNER_FIRST[:count]
    positions = CORNER_FIRST if layout == "corners" else LEGACY_CROSS_FIRST
    return positions[:count]
