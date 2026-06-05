"""Objective quality metrics for dance-single charts (validation harness).

These let us compare any chart - ours, AutoStepper's, or DDC's - on the same
footing. Metrics fall into four groups (see the project research notes):

  * density / structure : NPS, jumps, holds, quantization mix, entropy
  * lane balance        : per-panel distribution vs the ideal 25% each
  * biomechanics        : crossover / double-step / candle rates, foot travel
  * audio alignment     : timing error of steps vs detected onsets (optional)

Feet are inferred for any chart with a small Viterbi pass so the biomechanical
metrics work even on charts authored by other tools.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, asdict

from . import geometry as geo
from .simfile_io import Chart, Simfile


def _bpm_at(sim: Simfile, beat: float) -> float:
    bpm = sim.bpms[0][1]
    for b, v in sim.bpms:
        if b <= beat:
            bpm = v
        else:
            break
    return bpm


def timed_steps(sim: Simfile, chart: Chart) -> list[tuple[float, list[int]]]:
    """Return (time_seconds, pressed_panels) for each active row."""
    steps = []
    for beat, row in chart.note_rows():
        panels = geo.row_to_panels(row)
        if not panels:
            continue
        bpm = _bpm_at(sim, beat)
        # offset in StepMania is negative of the audio start; time = -offset + beat*60/bpm
        time = -sim.offset + beat * 60.0 / bpm
        steps.append((time, panels))
    return steps


# --------------------------------------------------------------------------
# Foot inference (Viterbi over fixed panels)
# --------------------------------------------------------------------------

def _foot_move_cost(prev_panel, new_panel, other_panel, last_foot, foot, dt):
    cost = 0.0
    if last_foot == foot and prev_panel is not None and prev_panel != new_panel:
        cost += 8.0  # double-step
    # crossover
    if foot == "L":
        crossed = geo.is_crossover(new_panel, other_panel)
    else:
        crossed = geo.is_crossover(other_panel, new_panel)
    if crossed:
        cost += 6.0
    if prev_panel is not None:
        dist = geo.distance(prev_panel, new_panel)
        cost += 0.5 * dist
        if geo.is_candle(prev_panel, new_panel):
            cost += 3.0
    return cost


def infer_feet(steps: list[tuple[float, list[int]]]) -> list[tuple[str, ...]]:
    """Assign a foot ('L'/'R') to each pressed panel via lowest-cost path."""
    from .geometry import L, D, U, R

    State = tuple  # (left_panel, right_panel, last_foot)
    starts = [(L, R, None), (D, U, None), (L, U, None), (D, R, None)]
    prev_costs = {s: 0.0 for s in starts}
    back_chain = []
    assign_choice = []
    prev_time = steps[0][0] if steps else 0.0

    for idx, (t, panels) in enumerate(steps):
        dt = max(t - prev_time, 1e-3) if idx > 0 else 1.0
        new_costs, back, choice = {}, {}, {}
        for (lp, rp, last), base in prev_costs.items():
            if len(panels) >= 2:
                p0, p1 = panels[0], panels[1]
                for la, ra in ((p0, p1), (p1, p0)):
                    c = _foot_move_cost(lp, la, rp, last, "L", dt) \
                        + _foot_move_cost(rp, ra, lp, last, "R", dt)
                    ns = (la, ra, None)
                    tot = base + c
                    if ns not in new_costs or tot < new_costs[ns]:
                        new_costs[ns] = tot
                        back[ns] = (lp, rp, last)
                        choice[ns] = (("L", la), ("R", ra))
            else:
                p = panels[0]
                cL = _foot_move_cost(lp, p, rp, last, "L", dt)
                nsL = (p, rp, "L")
                totL = base + cL
                if nsL not in new_costs or totL < new_costs[nsL]:
                    new_costs[nsL] = totL
                    back[nsL] = (lp, rp, last)
                    choice[nsL] = (("L", p),)
                cR = _foot_move_cost(rp, p, lp, last, "R", dt)
                nsR = (lp, p, "R")
                totR = base + cR
                if nsR not in new_costs or totR < new_costs[nsR]:
                    new_costs[nsR] = totR
                    back[nsR] = (lp, rp, last)
                    choice[nsR] = (("R", p),)
        prev_costs = new_costs
        back_chain.append(back)
        assign_choice.append(choice)
        prev_time = t

    if not prev_costs:
        return []
    state = min(prev_costs, key=prev_costs.get)
    out_rev = []
    for i in range(len(steps) - 1, -1, -1):
        out_rev.append(assign_choice[i][state])
        state = back_chain[i][state]
    out_rev.reverse()
    return out_rev


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

@dataclass
class ChartMetrics:
    difficulty: str
    meter: int
    note_count: int
    jumps: int
    holds: int
    mines: int
    duration: float
    nps: float
    peak_nps: float
    lane_distribution: tuple[float, float, float, float]
    lane_imbalance: float          # max |lane% - 25%|
    crossover_rate: float
    double_step_rate: float
    candle_rate: float
    mean_foot_travel: float
    step_entropy: float            # bits, over 2-step transitions
    quant_mix: dict
    onset_align_ms: float | None   # mean abs timing error to onsets
    onset_recall: float | None     # fraction of onsets covered by a step

    def as_dict(self):
        return asdict(self)


def _entropy(seq: list) -> float:
    if len(seq) < 2:
        return 0.0
    bigrams = Counter(zip(seq, seq[1:]))
    total = sum(bigrams.values())
    return -sum((c / total) * math.log2(c / total) for c in bigrams.values())


def _quant_of_beat(beat: float) -> int:
    frac = beat - math.floor(beat)
    for q in (4, 8, 12, 16, 24):
        npb = q / 4.0
        if abs(round(frac * npb) / npb - frac) < 1e-3:
            return q
    return 48


def compute_metrics(
    sim: Simfile,
    chart: Chart,
    onset_times=None,
) -> ChartMetrics:
    steps = timed_steps(sim, chart)
    note_count = sum(len(p) for _, p in steps)
    active = len(steps)
    jumps = sum(1 for _, p in steps if len(p) >= 2)

    # holds / mines from raw rows
    holds = mines = 0
    quant_counter: Counter = Counter()
    for beat, row in chart.note_rows():
        holds += row.count("2")
        mines += row.count("M")
        if geo.row_to_panels(row):
            quant_counter[_quant_of_beat(beat)] += 1

    duration = steps[-1][0] - steps[0][0] if active > 1 else 1.0
    nps = active / duration if duration > 0 else 0.0

    # peak NPS over a 1-second sliding window.
    peak = 0
    times = [t for t, _ in steps]
    j = 0
    for i in range(len(times)):
        while times[i] - times[j] > 1.0:
            j += 1
        peak = max(peak, i - j + 1)
    peak_nps = float(peak)

    # lane distribution
    lane_counts = [0, 0, 0, 0]
    for _, panels in steps:
        for p in panels:
            lane_counts[p] += 1
    tot = sum(lane_counts) or 1
    lane_dist = tuple(c / tot for c in lane_counts)
    lane_imbalance = max(abs(x - 0.25) for x in lane_dist)

    # biomechanics via inferred feet
    feet = infer_feet(steps)
    crossovers = double_steps = candles = 0
    travel_sum = 0.0
    travel_n = 0
    last_panel = {"L": None, "R": None}
    last_foot = None
    foot_seq = []
    for assignment in feet:
        # assignment is a tuple of (foot, panel)
        moved_feet = [f for f, _ in assignment]
        # crossover check on resulting stance
        lp = next((p for f, p in assignment if f == "L"), last_panel["L"])
        rp = next((p for f, p in assignment if f == "R"), last_panel["R"])
        if geo.is_crossover(lp, rp):
            crossovers += 1
        for f, p in assignment:
            if last_panel[f] is not None:
                travel_sum += geo.distance(last_panel[f], p)
                travel_n += 1
                if geo.is_candle(last_panel[f], p):
                    candles += 1
            if last_foot == f and len(assignment) == 1 and last_panel[f] is not None and last_panel[f] != p:
                double_steps += 1
            last_panel[f] = p
            foot_seq.append((f, p))
        if len(moved_feet) == 1:
            last_foot = moved_feet[0]
        else:
            last_foot = None

    crossover_rate = crossovers / active if active else 0.0
    double_step_rate = double_steps / active if active else 0.0
    candle_rate = candles / (duration / 60.0) if duration > 0 else 0.0  # per minute
    mean_travel = travel_sum / travel_n if travel_n else 0.0
    entropy = _entropy([p for _, p in foot_seq])

    # audio alignment
    align_ms = recall = None
    if onset_times is not None and len(onset_times) and active:
        import numpy as np
        onset_arr = np.asarray(onset_times, dtype=float)
        errs = []
        for t, _ in steps:
            errs.append(float(np.min(np.abs(onset_arr - t))))
        align_ms = float(np.mean(errs) * 1000.0)
        # recall: fraction of onsets within 70ms of some step
        step_arr = np.asarray([t for t, _ in steps])
        covered = 0
        for ot in onset_arr:
            if np.min(np.abs(step_arr - ot)) <= 0.07:
                covered += 1
        recall = covered / len(onset_arr)

    return ChartMetrics(
        difficulty=chart.difficulty,
        meter=chart.meter,
        note_count=note_count,
        jumps=jumps,
        holds=holds,
        mines=mines,
        duration=round(duration, 2),
        nps=round(nps, 3),
        peak_nps=peak_nps,
        lane_distribution=tuple(round(x, 3) for x in lane_dist),
        lane_imbalance=round(lane_imbalance, 3),
        crossover_rate=round(crossover_rate, 4),
        double_step_rate=round(double_step_rate, 4),
        candle_rate=round(candle_rate, 2),
        mean_foot_travel=round(mean_travel, 3),
        step_entropy=round(entropy, 3),
        quant_mix=dict(sorted(quant_counter.items())),
        onset_align_ms=round(align_ms, 1) if align_ms is not None else None,
        onset_recall=round(recall, 3) if recall is not None else None,
    )
