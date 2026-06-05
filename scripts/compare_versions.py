"""Compare every TempoSync density version on the same song.

Runs each named generator variant (see ``generate.VARIANTS``) on one audio file,
writes a playable folder per version, scores all charts on the shared objective
metrics, and saves a ``versions_comparison.json`` plus a compact table so the
versions can be compared a posteriori.

Usage:
    python scripts/compare_versions.py "songs/Song.mp3"
    python scripts/compare_versions.py "songs/Song.mp3" \
        --difficulties Easy Medium Hard Challenge --artist "DECO*27"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sm_generator import generate, metrics  # noqa: E402
from sm_generator.simfile_io import write_sm_file  # noqa: E402
from sm_generator.timing import analyze_audio  # noqa: E402


def _slug(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]", "", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    return name or "song"


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare TempoSync density versions")
    parser.add_argument("audio", help="path to the input audio file")
    parser.add_argument("--artist", default="Unknown")
    parser.add_argument("--title", default=None)
    parser.add_argument(
        "--difficulties", nargs="+",
        default=["Easy", "Medium", "Hard", "Challenge"],
        help="difficulties to generate for every version",
    )
    parser.add_argument(
        "--out", default=None,
        help="output root (default: output/versions/<title>)",
    )
    args = parser.parse_args()

    audio = args.audio
    if not os.path.exists(audio):
        raise SystemExit(f"audio not found: {audio}")
    title = args.title or os.path.splitext(os.path.basename(audio))[0]

    print(f"[1/3] Analyzing once: {audio}")
    analysis = analyze_audio(audio)
    onset_nps = len(analysis.onset_times) / max(analysis.duration, 1e-3)
    print(
        f"      BPM={analysis.bpm:.2f} duration={analysis.duration:.1f}s "
        f"onsets={len(analysis.onset_times)} (~{onset_nps:.2f} onsets/s)"
    )

    out_root = args.out or os.path.join("output", "versions", _slug(title))
    os.makedirs(out_root, exist_ok=True)
    music_name = os.path.basename(audio)

    report: dict = {
        "title": title,
        "artist": args.artist,
        "audio": audio,
        "bpm": round(analysis.bpm, 2),
        "duration": round(analysis.duration, 1),
        "onset_nps": round(onset_nps, 3),
        "difficulties": args.difficulties,
        "versions": {},
    }

    print("[2/3] Generating + scoring each version")
    for vid, (display, strategy) in generate.VARIANTS.items():
        sim = generate.generate_simfile(
            audio, title=title, artist=args.artist,
            difficulties=args.difficulties, strategy=strategy, analysis=analysis,
        )
        folder = os.path.join(out_root, vid)
        os.makedirs(folder, exist_ok=True)
        sim.music = music_name
        shutil.copy2(audio, os.path.join(folder, music_name))
        write_sm_file(sim, os.path.join(folder, f"{_slug(title)}.sm"))

        charts = []
        for ch in sim.charts:
            m = metrics.compute_metrics(sim, ch, onset_times=analysis.onset_times)
            charts.append(m.as_dict())
        report["versions"][vid] = {
            "display": display, "strategy": strategy, "charts": charts,
        }

    with open(os.path.join(out_root, "versions_comparison.json"), "w",
              encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    print("[3/3] Density (NPS) per difficulty per version")
    print(f"      (target should rise Easy -> Challenge; flat = bad grading)\n")
    header = f"{'difficulty':<12}" + "".join(f"{v:<22}" for v in generate.VARIANTS)
    print(header)
    print("-" * len(header))
    for i, diff in enumerate(args.difficulties):
        row = f"{diff:<12}"
        for vid in generate.VARIANTS:
            ch = report["versions"][vid]["charts"]
            m = ch[i] if i < len(ch) else None
            if m:
                cell = f"nps={m['nps']:<5} sync={m['onset_align_ms']}ms"
            else:
                cell = "-"
            row += f"{cell:<22}"
        print(row)

    print(f"\nSaved versions to {out_root}")
    print(f"Report: {os.path.join(out_root, 'versions_comparison.json')}")


if __name__ == "__main__":
    main()
