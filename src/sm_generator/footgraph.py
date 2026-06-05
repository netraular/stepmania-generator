"""FootGraph: foot-aware step selection by lowest-cost path.

Given a fixed sequence of step *times* (and how many arrows to press at each
time), decide *which* panels to use so the result is comfortable to play on a
pad. We model the two feet as a state and search for the lowest-cost sequence
of foot placements with a Viterbi / dynamic-programming pass.

This is the core improvement over:
  * DDC  -> has no foot model at all (pure sequence statistics).
  * AutoStepper -> selects patterns largely at random.

The cost terms (distance/speed tightening, crossover/double-step/candle
penalties, facing, lane balance) are adapted from the StepManiaLibrary cost
cascade and gated per difficulty.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from . import geometry as geo
from .geometry import L, D, U, R


@dataclass
class DifficultyConfig:
    """Per-difficulty knobs controlling density and allowed techniques."""

    name: str
    meter: int
    target_nps: float           # desired sustained notes per second
    allowed_quants: tuple[int, ...]  # subdivisions allowed (4,8,12,16,24)
    jump_rate: float            # fraction of steps that may become jumps
    hold_rate: float            # fraction of sustained notes turned into holds
    allow_crossovers: bool
    crossover_penalty: float
    double_step_penalty: float
    candle_penalty: float
    jack_penalty: float = 14.0     # same foot hits the same arrow again
    repeat_penalty: float = 4.0    # a foot that stays put (causes lane collapse)

# Sensible defaults spanning Beginner -> Challenge (ITG-ish meters).
DIFFICULTIES: dict[str, DifficultyConfig] = {
    "Beginner": DifficultyConfig("Beginner", 2, 1.2, (4,), 0.0, 0.0,
                                 False, 1e6, 50.0, 30.0),
    "Easy": DifficultyConfig("Easy", 4, 2.2, (4, 8), 0.02, 0.15,
                             False, 1e6, 30.0, 20.0),
    "Medium": DifficultyConfig("Medium", 6, 3.5, (4, 8, 16), 0.06, 0.20,
                               False, 200.0, 18.0, 10.0),
    "Hard": DifficultyConfig("Hard", 8, 5.0, (4, 8, 16), 0.10, 0.15,
                             True, 40.0, 10.0, 6.0),
    "Challenge": DifficultyConfig("Challenge", 10, 7.0, (4, 8, 12, 16, 24),
                                  0.12, 0.10, True, 12.0, 6.0, 3.0),
}

DIFFICULTY_ORDER = ["Beginner", "Easy", "Medium", "Hard", "Challenge"]


# Distance/speed tightening knobs (panel-widths and seconds).
_DIST_MIN = 1.4
_DIST_MAX = 2.3333
_SPEED_HI = 0.24     # seconds between feet where speed cost begins
_SPEED_LO = 0.1765   # seconds between feet where speed cost is maxed
_SPEED_MIN_DIST = 1.0  # moves shorter than this are exempt (allows candles)


def _ramp(value: float, hi: float, lo: float) -> float:
    """Return 0 at >=hi, 1 at <=lo, linear in between (hi > lo)."""
    if value >= hi:
        return 0.0
    if value <= lo:
        return 1.0
    return (hi - value) / (hi - lo)


@dataclass(frozen=True)
class _State:
    left: int | None   # panel under the left foot
    right: int | None  # panel under the right foot
    last: str | None   # which foot moved last ('L', 'R', or None)


def _initial_states() -> list[_State]:
    """Reasonable neutral starting positions (left on L, right on R, etc.)."""
    return [
        _State(L, R, None),
        _State(D, U, None),
        _State(L, U, None),
        _State(D, R, None),
    ]


class FootPlacer:
    """Assigns feet to a timed note sequence via lowest-cost DP.

    `events` is a list of (time_seconds, n_arrows) where n_arrows is 1 for a
    single step or 2 for a jump. Returns a list of rows (4-char strings).
    """

    def __init__(self, cfg: DifficultyConfig, seed: int = 0):
        self.cfg = cfg
        self.rng = random.Random(seed)

    # -- cost of a single foot move --------------------------------------
    def _move_cost(self, foot: str, prev: _State, new_panel: int,
                   dt: float) -> float:
        cfg = self.cfg
        cost = 0.0
        cur_panel = prev.left if foot == "L" else prev.right
        other_panel = prev.right if foot == "L" else prev.left

        # Double-step: same foot moves twice in a row to a different panel.
        if prev.last == foot and cur_panel is not None and cur_panel != new_panel:
            cost += cfg.double_step_penalty

        # Jack: same foot hits the same arrow again (collapses to one lane).
        if prev.last == foot and cur_panel is not None and cur_panel == new_panel:
            cost += cfg.jack_penalty

        # Static foot: a foot that does not move encourages lane collapse even
        # when feet alternate; nudge each foot to roam its comfortable panels.
        if cur_panel is not None and cur_panel == new_panel and prev.last != foot:
            cost += cfg.repeat_penalty

        # Crossover: stepping onto the other foot's side.
        if foot == "L":
            crossed = geo.is_crossover(new_panel, other_panel)
        else:
            crossed = geo.is_crossover(other_panel, new_panel)
        if crossed:
            if not cfg.allow_crossovers:
                cost += cfg.crossover_penalty
            else:
                cost += cfg.crossover_penalty * 0.25

        # Distance tightening: penalize large single-foot travel.
        if cur_panel is not None:
            dist = geo.distance(cur_panel, new_panel)
            if dist > _DIST_MIN:
                cost += 6.0 * _ramp(_DIST_MAX - (dist - _DIST_MIN),
                                    _DIST_MAX, _DIST_MIN)
            # Speed tightening: penalize fast *and* far moves.
            if dist > _SPEED_MIN_DIST and dt > 0:
                cost += 8.0 * _ramp(dt, _SPEED_HI, _SPEED_LO) * (dist - _SPEED_MIN_DIST)
            # Candle: same foot sweeps U<->D.
            if geo.is_candle(cur_panel, new_panel):
                cost += cfg.candle_penalty

        # Facing: discourage ending in a crossed/twisted stance.
        nl = new_panel if foot == "L" else prev.left
        nr = new_panel if foot == "R" else prev.right
        f = geo.facing(nl, nr)
        if f < 0:
            cost += 4.0 * (-f)

        return cost

    def place(self, events: list[tuple[float, int]]) -> list[str]:
        if not events:
            return []

        # DP over foot states. For each step we record, per resulting state,
        # the cheapest cost, the previous state, and the row that was played.
        prev_costs: dict[_State, float] = {s: 0.0 for s in _initial_states()}
        back_chain: list[dict[_State, _State]] = []
        row_choice: list[dict[_State, str]] = []
        prev_time = events[0][0]

        for idx, (t, n_arrows) in enumerate(events):
            dt = max(t - prev_time, 1e-3) if idx > 0 else 1.0
            new_costs: dict[_State, float] = {}
            back: dict[_State, _State] = {}
            rowc: dict[_State, str] = {}

            for state, base in prev_costs.items():
                for nstate, move_cost, row in self._transitions(state, n_arrows, dt):
                    total = base + move_cost
                    if nstate not in new_costs or total < new_costs[nstate]:
                        new_costs[nstate] = total
                        back[nstate] = state
                        rowc[nstate] = row

            prev_costs = new_costs
            back_chain.append(back)
            row_choice.append(rowc)
            prev_time = t

        # Reconstruct the cheapest path; collect the row chosen at each step.
        state = min(prev_costs, key=prev_costs.get)
        rows_rev: list[str] = []
        for i in range(len(events) - 1, -1, -1):
            rows_rev.append(row_choice[i][state])
            state = back_chain[i][state]
        rows_rev.reverse()
        return rows_rev

    def _transitions(self, state: _State, n_arrows: int, dt: float):
        """Yield (new_state, cost, row) options for one event."""
        out = []
        if n_arrows >= 2:
            # Jump: both feet land. Choose the lowest-cost foot->panel pairing
            # among comfortable two-panel combos.
            for lp, rp in _JUMP_PAIRS:
                cost = self._move_cost("L", state, lp, dt) \
                    + self._move_cost("R", state, rp, dt)
                cost += self.rng.random() * 0.01  # tie-break for variety
                row = _row_from_panels([lp, rp])
                out.append((_State(lp, rp, None), cost, row))
        else:
            for panel in (L, D, U, R):
                # Option A: left foot steps here.
                cost_l = self._move_cost("L", state, panel, dt)
                cost_l += self.rng.random() * 0.01
                out.append((_State(panel, state.right, "L"), cost_l,
                            _row_from_panels([panel])))
                # Option B: right foot steps here.
                cost_r = self._move_cost("R", state, panel, dt)
                cost_r += self.rng.random() * 0.01
                out.append((_State(state.left, panel, "R"), cost_r,
                            _row_from_panels([panel])))
        return out


# Comfortable jump pairings (left panel, right panel).
_JUMP_PAIRS = [(L, R), (D, U), (L, U), (D, R), (L, D), (U, R)]


def _row_from_panels(panels: list[int]) -> str:
    chars = ["0", "0", "0", "0"]
    for p in panels:
        chars[p] = "1"
    return "".join(chars)


def make_difficulty(name: str) -> DifficultyConfig:
    return DIFFICULTIES[name]
