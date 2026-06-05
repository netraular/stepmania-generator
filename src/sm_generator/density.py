"""Density strategies: turn a timing analysis + difficulty into a note list.

The original generator only had a *subtractive* density model: it quantized the
detected onsets and dropped the weakest ones down to the target notes-per-second
(NPS). That works for busy songs, but on calmer tracks the detected onset count
is lower than the target for Medium/Hard/Challenge, so every difficulty hits the
same ceiling and produces an identical, flat chart (e.g. 2.602 NPS for all
three). Real games grade difficulty by *adding* notes on musical subdivisions,
not just by removing them.

This module exposes three interchangeable, comparable strategies so different
ideas can be A/B-tested and kept as versions:

  * ``subtractive`` (v1) - original behaviour. Quantize onsets, thin to target.
        Cannot exceed the detected onset count, so calm songs flatten out.
  * ``adaptive``    (v2) - onset *anchors* (which carry the sync) plus a musical
        grid-fill weighted by audio energy, so every difficulty actually reaches
        its target density while still landing notes where the music is loudest.
  * ``energy``      (v3) - like adaptive, but the *local* target follows the
        song's energy envelope: streams build up in loud sections (choruses) and
        thin out in calm ones, while the average density still matches the
        difficulty. This is the most musical / best-graded option.

All strategies return ``list[QuantizedNote]`` sorted by beat, ready for FootGraph.
"""

from __future__ import annotations

import math

import numpy as np

from .footgraph import DifficultyConfig
from .timing import QuantizedNote, TimingAnalysis, quantize, thin_to_density

STRATEGIES: tuple[str, ...] = ("subtractive", "adaptive", "energy")


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def _sample_env(analysis: TimingAnalysis, times: np.ndarray) -> np.ndarray:
    """Sample the (normalized) onset-strength envelope at arbitrary times."""
    env = analysis.onset_env
    et = analysis.onset_env_times
    if env is None or et is None or len(env) == 0:
        return np.zeros_like(times, dtype=float)
    e = np.interp(times, et, env)
    m = float(e.max())
    return e / m if m > 0 else e


def _grid_rank(quant: int) -> float:
    """Musical weight of a subdivision: coarser positions feel stronger."""
    return {4: 1.0, 8: 0.7, 12: 0.55, 16: 0.45, 24: 0.30}.get(quant, 0.4)


def _grid_candidates(
    analysis: TimingAnalysis, cfg: DifficultyConfig
) -> list[QuantizedNote]:
    """Every allowed subdivision position across the song as a QuantizedNote.

    Each position is named by its *coarsest* allowed subdivision and carries an
    energy-sampled strength so fills can be ranked by how loud the music is
    there.
    """
    beat_period = 60.0 / analysis.bpm
    total_beats = analysis.duration / beat_period

    named: dict[float, int] = {}
    for q in sorted(cfg.allowed_quants):  # coarsest first names the position
        spacing = 4.0 / q  # beats between positions at this subdivision
        k = 0
        b = 0.0
        while b <= total_beats:
            key = round(b, 4)
            if key not in named:
                named[key] = q
            k += 1
            b = k * spacing

    beats = np.array(sorted(named.keys()), dtype=float)
    if beats.size == 0:
        return []
    times = analysis.offset + beats * beat_period
    strengths = _sample_env(analysis, times)
    return [
        QuantizedNote(
            beat=float(b), time=float(t), strength=float(s), quant=named[round(b, 4)]
        )
        for b, t, s in zip(beats, times, strengths)
    ]


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------

def _subtractive(analysis: TimingAnalysis, cfg: DifficultyConfig) -> list[QuantizedNote]:
    notes = quantize(analysis, cfg.allowed_quants)
    return thin_to_density(notes, cfg.target_nps, analysis.duration)


def _adaptive(
    analysis: TimingAnalysis,
    cfg: DifficultyConfig,
    w_energy: float = 1.0,
    w_grid: float = 0.6,
    min_fill_energy: float = 0.06,
) -> list[QuantizedNote]:
    anchors = quantize(analysis, cfg.allowed_quants)
    duration = analysis.duration
    target = int(round(cfg.target_nps * duration))
    if target <= 0:
        return anchors

    # Enough real onsets already: fall back to thinning (keeps the sync).
    if len(anchors) >= target:
        return thin_to_density(anchors, cfg.target_nps, duration)

    occupied = {round(n.beat, 3) for n in anchors}
    pool = [
        c
        for c in _grid_candidates(analysis, cfg)
        if round(c.beat, 3) not in occupied and c.strength >= min_fill_energy
    ]
    pool.sort(
        key=lambda c: w_energy * c.strength + w_grid * _grid_rank(c.quant),
        reverse=True,
    )
    need = target - len(anchors)
    chosen = anchors + pool[: max(0, need)]
    chosen.sort(key=lambda n: n.beat)
    return chosen


def _energy(
    analysis: TimingAnalysis,
    cfg: DifficultyConfig,
    lo: float = 0.40,
    hi: float = 1.80,
    window_beats: float = 4.0,
    min_fill_energy: float = 0.04,
) -> list[QuantizedNote]:
    duration = analysis.duration
    beat_period = 60.0 / analysis.bpm
    total_beats = duration / beat_period
    target_total = cfg.target_nps * duration
    if target_total <= 0:
        return quantize(analysis, cfg.allowed_quants)

    anchors = quantize(analysis, cfg.allowed_quants)
    occupied = {round(n.beat, 3) for n in anchors}
    pool = list(anchors)
    for c in _grid_candidates(analysis, cfg):
        if round(c.beat, 3) not in occupied:
            pool.append(c)

    nwin = max(1, int(math.ceil(total_beats / window_beats)))
    buckets: list[list[QuantizedNote]] = [[] for _ in range(nwin)]
    for n in pool:
        wi = min(nwin - 1, int(n.beat // window_beats))
        buckets[wi].append(n)

    # Window energy -> local density scale (loud sections denser).
    raw = np.array(
        [sum(x.strength for x in b) / len(b) if b else 0.0 for b in buckets]
    )
    rmax = float(raw.max()) if raw.size else 0.0
    enorm = raw / rmax if rmax > 0 else raw
    scale = lo + (hi - lo) * enorm

    win_dur = window_beats * beat_period
    win_durs = np.full(nwin, win_dur, dtype=float)
    win_durs[-1] = max(1e-3, duration - win_dur * (nwin - 1))

    # Normalize so the average density still matches the difficulty target.
    denom = float(np.sum(scale * win_durs))
    base = target_total / denom if denom > 0 else 0.0

    out: list[QuantizedNote] = []
    for i, bucket in enumerate(buckets):
        local_target = int(round(base * scale[i] * win_durs[i]))
        if local_target <= 0 or not bucket:
            continue
        # Anchors (real onsets) first, then strongest grid fills.
        ordered = sorted(
            bucket,
            key=lambda n: (round(n.beat, 3) in occupied, n.strength),
            reverse=True,
        )
        for n in ordered[:local_target]:
            if round(n.beat, 3) in occupied or n.strength >= min_fill_energy:
                out.append(n)
    out.sort(key=lambda n: n.beat)
    return out


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

_DISPATCH = {
    "subtractive": _subtractive,
    "adaptive": _adaptive,
    "energy": _energy,
}


def build_notes(
    analysis: TimingAnalysis,
    cfg: DifficultyConfig,
    strategy: str = "energy",
) -> list[QuantizedNote]:
    """Build the note list for one difficulty using the chosen strategy."""
    try:
        fn = _DISPATCH[strategy]
    except KeyError:
        raise ValueError(
            f"unknown density strategy {strategy!r}; choose from {STRATEGIES}"
        ) from None
    return fn(analysis, cfg)
