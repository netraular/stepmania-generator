"""Pad geometry and foot-placement primitives for `dance-single`.

Panels are laid out on a small coordinate grid (x = lateral, y = longitudinal):

        U (1,2)
    L (0,1)   R (2,1)
        D (1,0)

These coordinates let us compute realistic foot-travel distances, detect
crossovers, and measure how far the body is twisted ("facing"). They are the
shared foundation for both the generator (`footgraph`) and the validation
metrics (`metrics`).
"""

from __future__ import annotations

import math

# Panel indices (match simfile row order: Left, Down, Up, Right).
L, D, U, R = 0, 1, 2, 3
PANEL_NAMES = ("L", "D", "U", "R")

# (x, y) coordinates per panel. x is lateral (left->right), y is longitudinal.
PANEL_XY: dict[int, tuple[float, float]] = {
    L: (0.0, 1.0),
    D: (1.0, 0.0),
    U: (1.0, 2.0),
    R: (2.0, 1.0),
}

LEFT = "L"   # left foot
RIGHT = "R"  # right foot


def distance(a: int, b: int) -> float:
    """Euclidean distance in panel-widths between two panels."""
    ax, ay = PANEL_XY[a]
    bx, by = PANEL_XY[b]
    return math.hypot(ax - bx, ay - by)


def is_crossover(left_panel: int | None, right_panel: int | None) -> bool:
    """True if the feet are crossed (right foot left of left foot).

    Returns False if either foot is unplaced or both share the center column.
    """
    if left_panel is None or right_panel is None:
        return False
    lx = PANEL_XY[left_panel][0]
    rx = PANEL_XY[right_panel][0]
    return rx < lx


def facing(left_panel: int | None, right_panel: int | None) -> float:
    """Signed lateral orientation: positive = normal, negative = crossed.

    Equals right_foot.x - left_foot.x. ~0 means feet stacked in the center
    column (neutral, e.g. both on the U/D axis).
    """
    if left_panel is None or right_panel is None:
        return 0.0
    return PANEL_XY[right_panel][0] - PANEL_XY[left_panel][0]


def is_candle(prev_panel: int | None, new_panel: int) -> bool:
    """True if the same foot moves U<->D (a full longitudinal sweep)."""
    if prev_panel is None:
        return False
    return {prev_panel, new_panel} == {U, D}


def row_to_panels(row: str) -> list[int]:
    """Return the panel indices that are pressed (tap or hold head) in a row."""
    out = []
    for i, ch in enumerate(row):
        if ch in ("1", "2", "4"):  # tap, hold head, roll head
            out.append(i)
    return out
