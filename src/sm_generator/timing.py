"""TempoSync: real tempo / onset analysis and rhythm quantization.

This module turns raw audio into a list of musically-quantized note times,
synced to the song's *real* BPM and offset. This is the key fix over DDC,
which charts everything on a fixed 125 BPM grid and never syncs to the song.

Pipeline:
  1. Estimate BPM + beat grid (librosa).
  2. Derive offset from the first detected beat.
  3. Detect onsets and their strengths.
  4. Quantize each onset to the coarsest musical subdivision that fits,
     gated by the difficulty's allowed subdivisions.
  5. Thin / keep notes to hit a target notes-per-second density.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class TimingAnalysis:
    bpm: float
    offset: float            # seconds; StepMania OFFSET is the negative of this
    duration: float
    onset_times: np.ndarray  # seconds
    onset_strengths: np.ndarray


def analyze_audio(path: str, max_seconds: float | None = None) -> TimingAnalysis:
    """Run BPM, beat and onset analysis on an audio file."""
    import librosa

    y, sr = librosa.load(path, sr=44100, mono=True, duration=max_seconds)
    duration = len(y) / sr

    # Tempo + beat grid.
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units="time")
    bpm = float(np.atleast_1d(tempo)[0])
    if not np.isfinite(bpm) or bpm <= 0:
        bpm = 120.0

    # Offset: phase of the beat grid. Use the first beat time, folded into one
    # beat period so OFFSET stays small.
    beat_period = 60.0 / bpm
    first_beat = float(beats[0]) if len(beats) else 0.0
    offset = first_beat % beat_period

    # Onset detection with strengths.
    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    onset_frames = librosa.onset.onset_detect(
        onset_envelope=onset_env, sr=sr, backtrack=True, units="frames"
    )
    onset_times = librosa.frames_to_time(onset_frames, sr=sr)
    # Sample the strength envelope at each onset.
    strengths = onset_env[np.clip(onset_frames, 0, len(onset_env) - 1)]
    if strengths.size:
        strengths = strengths / (strengths.max() + 1e-9)

    return TimingAnalysis(
        bpm=bpm,
        offset=offset,
        duration=duration,
        onset_times=np.asarray(onset_times, dtype=float),
        onset_strengths=np.asarray(strengths, dtype=float),
    )


@dataclass
class QuantizedNote:
    beat: float          # snapped beat (quarter notes from grid start)
    time: float          # snapped time in seconds
    strength: float
    quant: int           # subdivision it snapped to (4,8,12,16,24)


def _snap_beat(beat: float, quant: int) -> float:
    """Snap a beat value to the given subdivision (notes per beat = quant/4)."""
    npb = quant / 4.0
    return round(beat * npb) / npb


def quantize(
    analysis: TimingAnalysis,
    allowed_quants: tuple[int, ...],
    tolerance_beats: float = 0.18,
) -> list[QuantizedNote]:
    """Snap onsets to the coarsest allowed subdivision within tolerance."""
    bpm = analysis.bpm
    offset = analysis.offset
    beat_period = 60.0 / bpm

    notes: list[QuantizedNote] = []
    seen_beats: set[float] = set()

    ordered = sorted(allowed_quants)
    for t, strength in zip(analysis.onset_times, analysis.onset_strengths):
        raw_beat = (t - offset) / beat_period
        if raw_beat < -0.5:
            continue
        chosen = None
        for q in ordered:  # coarsest first
            snapped = _snap_beat(raw_beat, q)
            if abs(snapped - raw_beat) <= tolerance_beats:
                chosen = (snapped, q)
                break
        if chosen is None:
            finest = ordered[-1]
            chosen = (_snap_beat(raw_beat, finest), finest)

        snapped_beat, q = chosen
        if snapped_beat < 0:
            snapped_beat = 0.0
        key = round(snapped_beat, 4)
        if key in seen_beats:
            continue
        seen_beats.add(key)
        notes.append(QuantizedNote(
            beat=snapped_beat,
            time=offset + snapped_beat * beat_period,
            strength=float(strength),
            quant=q,
        ))

    notes.sort(key=lambda n: n.beat)
    return notes


def thin_to_density(
    notes: list[QuantizedNote],
    target_nps: float,
    duration: float,
) -> list[QuantizedNote]:
    """Drop the weakest notes until density is near the target NPS.

    Keeps musically strong onsets; never drops below a minimal skeleton.
    """
    if not notes or duration <= 0:
        return notes
    target_count = int(target_nps * duration)
    if target_count >= len(notes):
        return notes

    # Always keep on-beat (quant==4) notes; rank the rest by strength.
    on_beat = [n for n in notes if abs(n.beat - round(n.beat)) < 1e-6]
    others = [n for n in notes if abs(n.beat - round(n.beat)) >= 1e-6]
    others.sort(key=lambda n: n.strength, reverse=True)

    keep = set(id(n) for n in on_beat)
    remaining = target_count - len(on_beat)
    for n in others:
        if remaining <= 0:
            break
        keep.add(id(n))
        remaining -= 1

    kept = [n for n in notes if id(n) in keep]
    kept.sort(key=lambda n: n.beat)
    return kept
