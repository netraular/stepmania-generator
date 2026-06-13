"""End-to-end generation: audio file -> dance-single Simfile.

Combines TempoSync (real BPM/offset + quantized onsets) with FootGraph
(lowest-cost foot placement) to produce one chart per requested difficulty.
"""

from __future__ import annotations

import os

from . import density, footgraph
from .footgraph import DifficultyConfig, FootPlacer
from .simfile_io import Chart, Simfile
from .timing import (
    QuantizedNote,
    TimingAnalysis,
    analyze_audio,
)

ROWS_PER_MEASURE = 48  # divisible by 4,8,12,16,24 subdivisions

# Default density strategy and the set of named, comparable generator versions.
# See density.py for what each strategy does. Keeping them all lets us A/B-test
# and ship whichever scores best while preserving the others as alternatives.
#
# v4 (musical) is the default: song-adaptive target density (calm songs charted
# sparser, busy ones denser, instead of a fixed value) plus coherent layered
# fills so streams feel intentional. v2/v3 keep a fixed target for comparison;
# v1 is the original subtractive baseline (flat-NPS bug on calm songs).
DEFAULT_STRATEGY = "musical"
VARIANTS: dict[str, tuple[str, str]] = {
    # id -> (display name, density strategy)
    "v1-onset": ("TempoSync v1 (onset-thin)", "subtractive"),
    "v2-grid": ("TempoSync v2 (adaptive grid)", "adaptive"),
    "v3-energy": ("TempoSync v3 (energy-aware)", "energy"),
    "v4-musical": ("TempoSync v4 (song-adaptive)", "musical"),
}
DEFAULT_VARIANT = "v4-musical"


def _select_jumps(notes: list[QuantizedNote], cfg: DifficultyConfig) -> set[int]:
    """Pick note indices to render as jumps (strong, well-spaced onsets)."""
    if cfg.jump_rate <= 0 or len(notes) < 3:
        return set()
    budget = int(cfg.jump_rate * len(notes))
    if budget <= 0:
        return set()
    # Rank by strength but require breathing room around the note.
    candidates = []
    for i, n in enumerate(notes):
        prev_dt = n.time - notes[i - 1].time if i > 0 else 1.0
        next_dt = notes[i + 1].time - n.time if i < len(notes) - 1 else 1.0
        if min(prev_dt, next_dt) >= 0.25:  # not inside a fast stream
            candidates.append((n.strength, i))
    candidates.sort(reverse=True)
    return {i for _, i in candidates[:budget]}


def generate_chart(
    analysis: TimingAnalysis,
    cfg: DifficultyConfig,
    seed: int = 0,
    strategy: str = DEFAULT_STRATEGY,
) -> Chart:
    """Build a single difficulty chart from a timing analysis."""
    notes = density.build_notes(analysis, cfg, strategy=strategy)
    if not notes:
        return Chart(difficulty=cfg.name, meter=cfg.meter,
                     description="sm_generator", measures=[])

    jump_idx = _select_jumps(notes, cfg)
    events = [(n.time, 2 if i in jump_idx else 1) for i, n in enumerate(notes)]

    placer = FootPlacer(cfg, seed=seed)
    rows = placer.place(events)

    # Lay rows onto a measure grid.
    n_measures = int(notes[-1].beat // 4) + 1
    measures: list[list[str]] = [
        ["0000"] * ROWS_PER_MEASURE for _ in range(n_measures)
    ]
    for n, row in zip(notes, rows):
        mi = int(n.beat // 4)
        pos_in_measure = n.beat - mi * 4.0           # 0..4 beats
        ri = int(round(pos_in_measure / 4.0 * ROWS_PER_MEASURE)) % ROWS_PER_MEASURE
        measures[mi][ri] = row

    measures = _apply_holds(measures, cfg)
    measures = [_trim_measure(m) for m in measures]
    return Chart(
        difficulty=cfg.name,
        meter=cfg.meter,
        description="sm_generator",
        measures=measures,
    )


def _apply_holds(measures: list[list[str]], cfg: DifficultyConfig):
    """Turn a fraction of well-isolated taps into short holds.

    A tap becomes a hold head (2) if the same panel is free for a while after
    it; a tail (3) is written shortly before the next event on that panel.

    A hold pins one foot for its entire span, so it must be counted as a note
    that occupies a foot: while a hold is held, at most ONE other panel may be
    pressed at any instant (the free foot), otherwise the chart asks for three
    simultaneous presses, which is impossible with two feet. We therefore only
    commit a hold when the resulting simultaneous-press count stays within two
    feet across its whole span.
    """
    if cfg.hold_rate <= 0:
        return measures

    # Flatten to a global row timeline for easy lookahead.
    total_rows = len(measures) * ROWS_PER_MEASURE

    def get(idx):
        return measures[idx // ROWS_PER_MEASURE][idx % ROWS_PER_MEASURE]

    def setc(idx, panel, ch):
        m = measures[idx // ROWS_PER_MEASURE]
        r = list(m[idx % ROWS_PER_MEASURE])
        r[panel] = ch
        m[idx % ROWS_PER_MEASURE] = "".join(r)

    def pressed_count(row: str) -> int:
        # Panels physically pressed in this row: tap, hold head or roll head.
        return sum(1 for ch in row if ch in "124")

    # Spans of already-committed holds, so their (invisible) middle rows are
    # also counted toward the two-foot limit when placing later holds.
    held_spans: list[tuple[int, int]] = []

    def occupancy(j: int) -> int:
        """How many feet are busy at row j (taps/heads here + holds spanning).

        A hold's head is already counted by ``pressed_count`` (the '2'); its
        tail row still has the foot down at the moment of release, so a hold
        occupies a foot across ``head < j <= tail``.
        """
        occ = pressed_count(get(j))
        for s, e in held_spans:
            if s < j <= e:  # inside an earlier hold, incl. its release row
                occ += 1
        return occ

    import random
    rng = random.Random(1234)
    for idx in range(total_rows):
        row = get(idx)
        for panel in range(4):
            if row[panel] != "1":
                continue
            if rng.random() > cfg.hold_rate:
                continue
            # Find the next event on this panel.
            nxt = None
            for j in range(idx + 1, min(idx + ROWS_PER_MEASURE, total_rows)):
                if get(j)[panel] != "0":
                    nxt = j
                    break
            span = (nxt - idx) if nxt else ROWS_PER_MEASURE // 2
            if span < 6:  # only hold if there is real room
                continue
            tail = idx + span - 2
            if not (tail > idx and tail < total_rows and get(tail)[panel] == "0"):
                continue
            # Reject if holding here would ever require a third simultaneous
            # press: this hold occupies one foot across [idx, tail] (head is
            # already in the grid, tail row still has the foot down), so every
            # other row in that span may use at most one more foot.
            feasible = True
            for j in range(idx, tail + 1):
                occ = occupancy(j)
                if idx < j <= tail:
                    occ += 1  # this hold's own foot is down through the release
                if occ > 2:
                    feasible = False
                    break
            if not feasible:
                continue
            setc(idx, panel, "2")
            setc(tail, panel, "3")
            held_spans.append((idx, tail))
    return measures


def _trim_measure(measure: list[str]) -> list[str]:
    """Reduce a 48-row measure to the coarsest resolution that preserves notes."""
    for res in (4, 8, 12, 16, 24, 48):
        step = ROWS_PER_MEASURE // res
        ok = all(
            measure[i] == "0000"
            for i in range(ROWS_PER_MEASURE)
            if i % step != 0
        )
        if ok:
            return [measure[i] for i in range(0, ROWS_PER_MEASURE, step)]
    return measure


def generate_simfile(
    audio_path: str,
    title: str,
    artist: str,
    difficulties: list[str] | None = None,
    max_seconds: float | None = None,
    strategy: str = DEFAULT_STRATEGY,
    analysis: TimingAnalysis | None = None,
) -> Simfile:
    """Analyze audio once and generate charts for each requested difficulty."""
    if difficulties is None:
        difficulties = footgraph.DIFFICULTY_ORDER

    if analysis is None:
        analysis = analyze_audio(audio_path, max_seconds=max_seconds)

    sim = Simfile(
        title=title,
        artist=artist,
        music=os.path.basename(audio_path),
        offset=-analysis.offset,
        bpms=[(0.0, round(analysis.bpm, 3))],
        sample_start=min(30.0, analysis.duration / 3),
        sample_length=20.0,
    )
    for i, name in enumerate(difficulties):
        cfg = footgraph.make_difficulty(name)
        sim.charts.append(generate_chart(analysis, cfg, seed=i, strategy=strategy))
    return sim
