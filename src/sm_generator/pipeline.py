"""High-level pipeline: YouTube URL -> playable charts from every generator.

This is the orchestration layer used by both the CLI and the web server. It
downloads the audio, runs each available generator (our TempoSync + FootGraph,
and AutoStepper if installed), scores every chart on the same objective metrics,
and writes playable song folders plus a `comparison.json`.

DDC has no local/offline mode (only a web demo), so it is not run here; if a DDC
`.sm` is dropped into `output/<slug>/DDC/` it will be picked up and scored.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict

from . import footgraph, generate, metrics
from .simfile_io import Simfile, parse_sm_file, write_sm_file
from .timing import TimingAnalysis, analyze_audio


# --------------------------------------------------------------------------
# Paths / helpers
# --------------------------------------------------------------------------

def repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def slugify(name: str) -> str:
    """Make a filesystem- and StepMania-friendly name (Windows-safe)."""
    name = re.sub(r"[\\/:*?\"<>|]", "", name)   # illegal on Windows
    name = re.sub(r"[\x00-\x1f]", "", name)      # control chars / newlines
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(". ")                      # no trailing dot/space on Windows
    return name or "song"


def _find_exe(name: str) -> str | None:
    return shutil.which(name)


def _utf8_env() -> dict:
    """Environment that forces a child Python (yt-dlp) to emit UTF-8.

    When yt-dlp's stdout is captured through a pipe on Windows it otherwise
    falls back to the legacy code page and silently drops non-Latin characters
    (e.g. Japanese song titles), so we pin UTF-8 here.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def autostepper_jar() -> str | None:
    jar = os.path.join(repo_root(), "tools", "AutoStepper", "AutoStepper.jar")
    return jar if os.path.exists(jar) else None


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------

@dataclass
class GeneratorResult:
    name: str
    available: bool = True
    sm_path: str | None = None
    folder: str | None = None
    charts: list[dict] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PipelineResult:
    title: str
    artist: str
    slug: str
    audio_path: str
    bpm: float
    offset: float
    duration: float
    output_dir: str
    generators: list[GeneratorResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _noop(_msg: str, _pct: float = 0.0) -> None:
    pass


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

def fetch_title(url: str) -> str:
    """Ask yt-dlp for the video title without downloading."""
    yt = _find_exe("yt-dlp")
    if not yt:
        return "song"
    try:
        out = subprocess.run(
            [yt, "--no-playlist", "--skip-download", "--print", "%(title)s", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=True, env=_utf8_env(),
        )
        # Collapse any stray newlines/whitespace into a single clean line.
        title = " ".join((out.stdout or "").split())
        return title or "song"
    except Exception:
        return "song"


def _clean_line(value: str) -> str:
    return " ".join((value or "").split())


def fetch_metadata(url: str) -> dict:
    """Fetch title + artist from a YouTube URL without downloading.

    Uses yt-dlp's music tags (`track`/`artist`) when available and falls back
    to parsing a "Artist - Title" style video title. Returns a dict with
    ``title`` and ``artist`` keys.
    """
    yt = _find_exe("yt-dlp")
    if not yt:
        return {"title": "", "artist": ""}
    try:
        out = subprocess.run(
            [yt, "--no-playlist", "--skip-download",
             "--print", "%(track)s\n%(artist)s\n%(title)s\n%(uploader)s", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=True, env=_utf8_env(),
        )
        lines = (out.stdout or "").splitlines()
        # yt-dlp prints the literal "NA" for missing fields.
        def field(i):
            v = _clean_line(lines[i]) if i < len(lines) else ""
            return "" if v.upper() == "NA" else v

        track, artist, title, uploader = field(0), field(1), field(2), field(3)

        song_title = track or title or "song"
        song_artist = artist

        # Fall back to splitting "Artist - Title" out of the video title.
        if not song_artist and " - " in title:
            left, right = title.split(" - ", 1)
            song_artist = _clean_line(left)
            if not track:
                song_title = _clean_line(right)
        if not song_artist:
            song_artist = uploader

        # Strip common YouTube noise from the title.
        for junk in ("(Official Music Video)", "(Official Video)",
                     "(Official Audio)", "(Lyric Video)", "(Audio)",
                     "[Official Music Video]", "[Official Video]"):
            song_title = song_title.replace(junk, "").replace(junk.lower(), "")
        song_title = _clean_line(song_title)

        return {"title": song_title or "song", "artist": song_artist or ""}
    except Exception:
        return {"title": "", "artist": ""}


def download_audio(url: str, slug: str, songs_dir: str, progress=_noop) -> str:
    """Download a single track as mp3; return its path."""
    yt = _find_exe("yt-dlp")
    if not yt:
        raise RuntimeError("yt-dlp not found on PATH")
    os.makedirs(songs_dir, exist_ok=True)
    out_tmpl = os.path.join(songs_dir, f"{slug}.%(ext)s")
    audio_path = os.path.join(songs_dir, f"{slug}.mp3")
    if os.path.exists(audio_path):
        progress("Audio already downloaded (cached)", 30)
        return audio_path
    progress("Downloading audio from YouTube", 10)
    subprocess.run(
        [yt, "--no-playlist", "-x", "--audio-format", "mp3",
         "--audio-quality", "0", "-o", out_tmpl, url],
        check=True, capture_output=True, text=True,
    )
    if not os.path.exists(audio_path):
        raise RuntimeError("yt-dlp finished but no mp3 was produced")
    return audio_path


def run_ours(
    audio_path: str,
    title: str,
    artist: str,
    difficulties: list[str] | None,
    analysis: TimingAnalysis,
    out_root: str,
    progress=_noop,
) -> GeneratorResult:
    """Generate our charts into a playable folder and score them."""
    res = GeneratorResult(name="TempoSync + FootGraph")
    try:
        progress("Generating our charts", 55)
        sim = generate.generate_simfile(
            audio_path, title=title, artist=artist, difficulties=difficulties
        )
        folder = os.path.join(out_root, "Ours")
        os.makedirs(folder, exist_ok=True)
        music_name = os.path.basename(audio_path)
        sim.music = music_name
        shutil.copy2(audio_path, os.path.join(folder, music_name))
        sm_path = os.path.join(folder, f"{slugify(title)}.sm")
        write_sm_file(sim, sm_path)

        for ch in sim.charts:
            m = metrics.compute_metrics(sim, ch, onset_times=analysis.onset_times)
            res.charts.append(m.as_dict())
        res.sm_path = sm_path
        res.folder = folder
    except Exception as exc:  # noqa: BLE001
        res.error = str(exc)
    return res


def run_autostepper(
    audio_path: str,
    out_root: str,
    analysis: TimingAnalysis,
    progress=_noop,
) -> GeneratorResult:
    """Run AutoStepper (if installed) into a playable folder and score it."""
    res = GeneratorResult(name="AutoStepper")
    jar = autostepper_jar()
    java = _find_exe("java")
    if not jar or not java:
        res.available = False
        res.error = "AutoStepper.jar or Java not found"
        return res
    try:
        progress("Running AutoStepper", 75)
        as_out = os.path.join(out_root, "AutoStepper")
        os.makedirs(as_out, exist_ok=True)
        # AutoStepper is a Java app that mangles non-ASCII paths on Windows
        # (it reports success but silently writes nowhere). So we run it inside
        # ASCII-only temp dirs and move the result into place with Python.
        with tempfile.TemporaryDirectory() as in_dir, \
                tempfile.TemporaryDirectory() as tmp_out:
            ascii_name = "track.mp3"
            shutil.copy2(audio_path, os.path.join(in_dir, ascii_name))
            duration = int(analysis.duration) + 2
            subprocess.run(
                [java, "-jar", os.path.basename(jar),
                 f"input={in_dir}", f"output={tmp_out}",
                 f"duration={duration}", "hard=true"],
                cwd=os.path.dirname(jar), check=True,
                capture_output=True, text=True, timeout=600,
            )
            # Find the .sm AutoStepper produced in the temp output.
            tmp_sm = None
            for root, _dirs, files in os.walk(tmp_out):
                for f in files:
                    if f.endswith(".sm"):
                        tmp_sm = os.path.join(root, f)
                        break
            if not tmp_sm:
                res.error = "produced no .sm output"
                return res
            # Copy the .sm next to the audio in the final (Unicode-safe) folder.
            music_name = os.path.basename(audio_path)
            shutil.copy2(audio_path, os.path.join(as_out, music_name))
            sm_path = os.path.join(as_out, music_name + ".sm")
            # Rewrite the #MUSIC tag so the chart points at the copied audio.
            bsim = parse_sm_file(tmp_sm)
            bsim.music = music_name
            write_sm_file(bsim, sm_path)

        for ch in bsim.charts:
            m = metrics.compute_metrics(bsim, ch, onset_times=analysis.onset_times)
            res.charts.append(m.as_dict())
        res.sm_path = sm_path
        res.folder = as_out
    except subprocess.TimeoutExpired:
        res.error = "AutoStepper timed out"
    except Exception as exc:  # noqa: BLE001
        res.error = str(exc)
    return res


def score_existing(name: str, folder: str, analysis: TimingAnalysis) -> GeneratorResult:
    """Score any pre-existing .sm placed in `folder` (e.g. a DDC export)."""
    res = GeneratorResult(name=name)
    sm_path = None
    for root, _dirs, files in os.walk(folder):
        for f in files:
            if f.endswith(".sm"):
                sm_path = os.path.join(root, f)
                break
    if not sm_path:
        res.available = False
        return res
    try:
        bsim = parse_sm_file(sm_path)
        for ch in bsim.charts:
            m = metrics.compute_metrics(bsim, ch, onset_times=analysis.onset_times)
            res.charts.append(m.as_dict())
        res.sm_path = sm_path
        res.folder = os.path.dirname(sm_path)
    except Exception as exc:  # noqa: BLE001
        res.error = str(exc)
    return res


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def run_pipeline(
    url: str,
    title: str | None = None,
    artist: str = "Unknown",
    difficulties: list[str] | None = None,
    include_autostepper: bool = True,
    progress=_noop,
) -> PipelineResult:
    """Full pipeline for one YouTube URL."""
    root = repo_root()
    songs_dir = os.path.join(root, "songs")
    output_base = os.path.join(root, "output", "web")

    if difficulties is None:
        difficulties = ["Easy", "Medium", "Hard"]

    progress("Fetching track info", 5)
    if not title:
        title = fetch_title(url)
    slug = slugify(title)

    audio_path = download_audio(url, slug, songs_dir, progress=progress)

    progress("Analyzing audio (BPM, onsets)", 35)
    analysis = analyze_audio(audio_path)

    out_root = os.path.join(output_base, slug)
    os.makedirs(out_root, exist_ok=True)

    result = PipelineResult(
        title=title, artist=artist, slug=slug, audio_path=audio_path,
        bpm=round(analysis.bpm, 2), offset=round(analysis.offset, 3),
        duration=round(analysis.duration, 1), output_dir=out_root,
    )

    result.generators.append(
        run_ours(audio_path, title, artist, difficulties, analysis, out_root, progress)
    )
    if include_autostepper:
        result.generators.append(
            run_autostepper(audio_path, out_root, analysis, progress)
        )
    # Pick up a manually-placed DDC export, if any.
    ddc = score_existing("DDC", os.path.join(out_root, "DDC"), analysis)
    if ddc.available and ddc.charts:
        result.generators.append(ddc)

    progress("Writing comparison", 95)
    with open(os.path.join(out_root, "comparison.json"), "w", encoding="utf-8") as fh:
        json.dump(result.to_dict(), fh, indent=2)

    progress("Done", 100)
    return result
