# stepmania-generator

Tools and notes for **automatically generating StepMania / Project OutFox
stepcharts** (in `dance-single` mode, for a 4‑panel dance pad) from YouTube
songs.

This repo is a companion to
[playlist-to-stepmania](https://github.com/netraular/playlist-to-stepmania):
that project finds existing charts for a playlist, and this one generates charts
for the songs that don't have any.

## Why

Many songs have no community-made stepchart. Auto-generators produce a playable
*draft* from the raw audio. The result is rarely as good as a hand-made chart
(beat detection can miss the downbeat, and patterns can feel awkward on a pad),
but it's a solid starting point you can refine in an editor like
[ArrowVortex](https://arrowvortex.ghli.org/) or
[GrooveAuthor](https://github.com/PerryAsleep/GrooveAuthor).

## Generators evaluated

| Generator | Language | Mode | Pad-friendly | Tested here |
| --- | --- | --- | --- | --- |
| **TempoSync + FootGraph** (this repo) | Python | Fully automatic | Yes (foot-aware) | ✅ Built-in, validated |
| [AutoStepper](https://github.com/phr00t/AutoStepper) (phr00t) | Java | Fully automatic | Yes (built for pads) | ✅ Works end-to-end |
| [StepGenerator](https://github.com/Johell1NS/StepGenerator) (Johell1NS) | Python | Semi-automatic (needs ArrowVortex + manual BPM) | Yes (dance-single) | ⚠️ Requires manual GUI step |
| [DDC](https://ddc.chrisdonahue.com/) (Dance Dance Convolution) | Python/TF | Fully automatic (web demo) | Weak (no foot model, fixed 125 BPM) | ✅ Baseline only |

This repo ships its **own** fully-automatic generator,
**TempoSync + FootGraph** (see [docs/METHOD.md](docs/METHOD.md)). It syncs to the
song's *real* BPM and chooses arrows with a foot-aware lowest-cost search, so the
result is more in-time and more comfortable on a pad than the alternatives. On
the test song it aligns ~2× better than AutoStepper, keeps a near-perfect 25%
per-panel balance, and produces essentially zero crossovers/candles.

**AutoStepper** remains a good Java alternative: it runs fully unattended,
generates all difficulty levels with holds/jumps, and is explicitly optimized
for dance pads.

**StepGenerator** is more modern and can give nicer results, but it is
*semi-automatic*: you must detect the BPM/downbeat yourself in ArrowVortex
before it generates the steps. Use it when AutoStepper's timing is off.

## Quick start (web app — recommended)

The easiest way to use everything is the built-in web app. It takes a YouTube
link, downloads the audio, runs **every available generator** (our TempoSync +
FootGraph and AutoStepper), scores them side by side, and gives you the playable
song folders to download.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

.venv\Scripts\python.exe server.py
# then open http://localhost:8000
```

Paste a YouTube URL, optionally set the title/artist and which difficulties to
generate, and press **Generate & compare**. Each generator's charts are scored
on the same objective metrics (timing sync, lane balance, crossovers, candles)
so you can see which version plays better, then download the folder as a `.zip`.

Outputs are written to `output/web/<title>/` with one playable subfolder per
generator plus a `comparison.json`.

## Quick start (built-in generator, CLI)

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# drop an MP3 into songs/, then:
.venv\Scripts\python.exe scripts\generate_modern.py "songs\Your Song - Artist.mp3"
```

Charts are written to `output/modern/<title>.sm` (all 5 difficulties) along with
a `metrics.json` comparing them to any baselines in `output/`. See
[docs/METHOD.md](docs/METHOD.md) for the method and validation results.


## Requirements

- [Java 17+](https://learn.microsoft.com/java/openjdk/) (for AutoStepper)
- [Python 3](https://www.python.org/) with [yt-dlp](https://github.com/yt-dlp/yt-dlp): `pip install yt-dlp`
- [ffmpeg](https://ffmpeg.org/) on your PATH (for audio extraction)

## Quick start (AutoStepper)

```powershell
# 1. Download AutoStepper (one time)
powershell -ExecutionPolicy Bypass -File scripts/setup-autostepper.ps1

# 2. Generate a chart from a YouTube URL
powershell -ExecutionPolicy Bypass -File scripts/generate.ps1 `
    -Url "https://www.youtube.com/watch?v=UnyLfqpyi94" `
    -Title "Burn the House Down" -Artist "AJR"
```

The generated song folder appears under `output/`. Copy it into your StepMania
/ OutFox `Songs` directory to play.

`generate.ps1` parameters:

- `-Url` (required) — YouTube URL.
- `-Title`, `-Artist` (required) — used for the file name / chart title.
- `-Duration` — seconds of audio to chart (default `130`).
- `-Hard` — add extra steps (default `$true`).
- `-Python` — path to your `python.exe` if `python` isn't on PATH.

## StepGenerator (semi-automatic, manual)

1. Install [ArrowVortex](https://arrowvortex.ghli.org/) and make sure ffmpeg is
   on PATH.
2. Clone and set it up:
   ```powershell
   git clone https://github.com/Johell1NS/StepGenerator.git tools/StepGenerator
   cd tools/StepGenerator
   .\setup_venv.bat
   # edit path.txt -> full path to ArrowVortex.exe
   ```
3. Run `menu.bat`, paste the YouTube URL (or drop an MP3 named
   `Title - Artist.mp3` into its `songs/` folder), detect the BPM/downbeat in
   ArrowVortex, save, then press ENTER to generate.

## Folder layout

```
server.py                built-in web app (committed)
web/                     web UI assets (committed)
src/sm_generator/        built-in TempoSync + FootGraph generator (committed)
scripts/                 helper scripts (committed)
docs/                    method & validation notes (committed)
tools/                   downloaded generators        (git-ignored)
songs/                   input audio                  (git-ignored)
output/                  generated charts             (git-ignored)
```

## Notes & tips

- Auto-generated timing can be slightly off. If the arrows drift, open the `.sm`
  in ArrowVortex, fix the BPM/offset, and re-sync.
- For dance pads, review the hardest difficulty for awkward crossovers/double
  steps and simplify by hand if needed.
- Charts are drafts — always playtest before sharing.

## Credits

- [AutoStepper](https://github.com/phr00t/AutoStepper) by Phr00t's Software
  (modified MIT, attribution required, non-commercial).
- [StepGenerator](https://github.com/Johell1NS/StepGenerator) by Johell1NS
  (GPL-3.0).
