"""Generate a chart with the TempoSync + FootGraph generator and validate it
against the AutoStepper and DDC baselines on the same objective metrics.

Usage:
    python scripts/generate_modern.py "songs/Burn the House Down - AJR.mp3"
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sm_generator import generate, metrics  # noqa: E402
from sm_generator.simfile_io import parse_sm_file, write_sm_file  # noqa: E402
from sm_generator.timing import analyze_audio  # noqa: E402


def _print_metrics(label, m):
    print(f"\n=== {label} :: {m.difficulty} (meter {m.meter}) ===")
    print(f"  notes={m.note_count} jumps={m.jumps} holds={m.holds} "
          f"dur={m.duration}s nps={m.nps} peak={m.peak_nps}")
    print(f"  lanes={m.lane_distribution} imbalance={m.lane_imbalance}")
    print(f"  crossover={m.crossover_rate} double_step={m.double_step_rate} "
          f"candle/min={m.candle_rate} travel={m.mean_foot_travel}")
    print(f"  entropy={m.step_entropy} quant_mix={m.quant_mix}")
    if m.onset_align_ms is not None:
        print(f"  onset_align={m.onset_align_ms}ms recall={m.onset_recall}")


def main():
    audio = sys.argv[1] if len(sys.argv) > 1 else "songs/Burn the House Down - AJR.mp3"
    title = os.path.splitext(os.path.basename(audio))[0]

    print(f"[1/4] Analyzing audio: {audio}")
    analysis = analyze_audio(audio)
    print(f"      BPM={analysis.bpm:.2f} offset={analysis.offset:.3f}s "
          f"duration={analysis.duration:.1f}s onsets={len(analysis.onset_times)}")

    print("[2/4] Generating charts (TempoSync + FootGraph)")
    sim = generate.generate_simfile(audio, title=title, artist="AJR")
    out_dir = os.path.join("output", "modern")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{title}.sm")
    write_sm_file(sim, out_path)
    print(f"      wrote {out_path} ({len(sim.charts)} charts)")

    onsets = analysis.onset_times

    print("[3/4] Scoring my charts")
    results = {"modern": []}
    for ch in sim.charts:
        m = metrics.compute_metrics(sim, ch, onset_times=onsets)
        _print_metrics("MODERN", m)
        results["modern"].append(m.as_dict())

    print("\n[4/4] Scoring baselines for comparison")
    baselines = {
        "autostepper": "output/Burn the House Down - AJR.mp3_dir/Burn the House Down - AJR.mp3.sm",
        "ddc": None,  # filled below if found
    }
    # locate DDC sm
    for root, _, files in os.walk("output/ddc"):
        for f in files:
            if f.endswith(".sm"):
                baselines["ddc"] = os.path.join(root, f)
    for name, path in baselines.items():
        if not path or not os.path.exists(path):
            print(f"  ({name} baseline not found, skipping)")
            continue
        bsim = parse_sm_file(path)
        results[name] = []
        for ch in bsim.charts:
            m = metrics.compute_metrics(bsim, ch, onset_times=onsets)
            _print_metrics(name.upper(), m)
            results[name].append(m.as_dict())

    with open(os.path.join(out_dir, "metrics.json"), "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    print(f"\nSaved metrics -> {os.path.join(out_dir, 'metrics.json')}")


if __name__ == "__main__":
    main()
