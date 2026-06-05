"""High-level pipeline: YouTube URL -> playable charts from every generator.

This is the orchestration layer used by both the CLI and the web server. It
downloads the audio, runs each available generator (our TempoSync + FootGraph,
and AutoStepper if installed), scores every chart on the same objective metrics,
and writes playable song folders plus a `comparison.json`.

DDC has no local/offline mode (only a web demo), so it is not run here; if a DDC
`.sm` is dropped into `output/<slug>/DDC/` it will be picked up and scored.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
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
    """ASCII-safe filesystem / StepMania folder name (Windows-safe).

    StepMania reads the *display* name from the chart's #TITLE / #ARTIST tags,
    not from the folder or file name, so we can keep folder and file names plain
    ASCII without losing the original (e.g. Japanese) title in-game. Accented
    Latin is transliterated (é -> e) and other non-ASCII (e.g. CJK) is dropped;
    if a title is entirely non-Latin we fall back to a short, stable hash so each
    song still gets a unique folder.
    """
    original = name or ""
    ascii_name = (
        unicodedata.normalize("NFKD", original)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    ascii_name = re.sub(r"[\\/:*?\"<>|]", "", ascii_name)   # illegal on Windows
    ascii_name = re.sub(r"[\x00-\x1f]", "", ascii_name)      # control chars
    ascii_name = re.sub(r"\s+", " ", ascii_name).strip()
    ascii_name = ascii_name.rstrip(". ")                     # no trailing dot/space
    if ascii_name:
        return ascii_name
    # Nothing ASCII survived (e.g. a fully Japanese title): use a stable hash.
    digest = hashlib.md5(original.strip().encode("utf-8")).hexdigest()[:8]
    return f"song-{digest}"


def _find_exe(name: str) -> str | None:
    return shutil.which(name)


def _log(msg: str) -> None:
    """Print a diagnostic line to stderr so the server console shows it."""
    print(f"[pipeline] {msg}", file=sys.stderr, flush=True)


def _ytdlp_cmd() -> list[str] | None:
    """Return the command prefix to invoke yt-dlp, or None if unavailable.

    Prefers a standalone ``yt-dlp`` executable on PATH, but falls back to
    ``python -m yt_dlp`` using the *current* interpreter. This is what makes it
    work on machines where yt-dlp was ``pip install``ed into the environment but
    its Scripts directory is not on PATH (the usual cause of silent
    "couldn't detect" failures on a fresh PC).
    """
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    try:
        import importlib.util
        if importlib.util.find_spec("yt_dlp") is not None:
            return [sys.executable, "-m", "yt_dlp"]
    except Exception:  # noqa: BLE001
        pass
    return None


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
    cmd = _ytdlp_cmd()
    if not cmd:
        _log("yt-dlp not found (not on PATH and not importable as a module); "
             "install it with `pip install yt-dlp`")
        return "song"
    try:
        out = subprocess.run(
            cmd + ["--no-playlist", "--skip-download", "--print", "%(title)s", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=True, env=_utf8_env(),
        )
        # Collapse any stray newlines/whitespace into a single clean line.
        title = " ".join((out.stdout or "").split())
        return title or "song"
    except subprocess.CalledProcessError as exc:
        _log(f"fetch_title failed for {url!r}: yt-dlp exited {exc.returncode}: "
             f"{(exc.stderr or '').strip()[:500]}")
        return "song"
    except Exception as exc:  # noqa: BLE001
        _log(f"fetch_title failed for {url!r}: {exc!r}")
        return "song"


def _clean_line(value: str) -> str:
    return " ".join((value or "").split())


def fetch_metadata(url: str) -> dict:
    """Fetch title + artist from a YouTube URL without downloading.

    Uses yt-dlp's music tags (`track`/`artist`) when available and falls back
    to parsing a "Artist - Title" style video title. Returns a dict with
    ``title`` and ``artist`` keys.
    """
    cmd = _ytdlp_cmd()
    if not cmd:
        _log("yt-dlp not found (not on PATH and not importable as a module); "
             "install it with `pip install yt-dlp`")
        return {"title": "", "artist": "", "error": "yt-dlp not installed"}
    try:
        out = subprocess.run(
            cmd + ["--no-playlist", "--skip-download",
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
    except subprocess.CalledProcessError as exc:
        reason = (exc.stderr or "").strip()
        _log(f"fetch_metadata failed for {url!r}: yt-dlp exited "
             f"{exc.returncode}: {reason[:500]}")
        return {"title": "", "artist": "",
                "error": reason.splitlines()[-1] if reason else "yt-dlp error"}
    except subprocess.TimeoutExpired:
        _log(f"fetch_metadata timed out for {url!r}")
        return {"title": "", "artist": "", "error": "yt-dlp timed out"}
    except Exception as exc:  # noqa: BLE001
        _log(f"fetch_metadata failed for {url!r}: {exc!r}")
        return {"title": "", "artist": "", "error": str(exc)}


def download_audio(url: str, slug: str, songs_dir: str, progress=_noop) -> str:
    """Download a single track as mp3; return its path."""
    cmd = _ytdlp_cmd()
    if not cmd:
        raise RuntimeError(
            "yt-dlp not found. Install it with `pip install yt-dlp` (or put "
            "yt-dlp on PATH)."
        )
    os.makedirs(songs_dir, exist_ok=True)
    out_tmpl = os.path.join(songs_dir, f"{slug}.%(ext)s")
    audio_path = os.path.join(songs_dir, f"{slug}.mp3")
    if os.path.exists(audio_path):
        progress("Audio already downloaded (cached)", 30)
        return audio_path
    progress("Downloading audio from YouTube", 10)
    subprocess.run(
        cmd + ["--no-playlist", "-x", "--audio-format", "mp3",
         "--audio-quality", "0", "-o", out_tmpl, url],
        check=True, capture_output=True, text=True,
    )
    if not os.path.exists(audio_path):
        raise RuntimeError("yt-dlp finished but no mp3 was produced")
    return audio_path


_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def download_thumbnail(url: str, slug: str, songs_dir: str,
                       progress=_noop) -> str | None:
    """Download the video thumbnail as an image; return its path or None.

    Used as the song's banner + background artwork. Prefers a jpg (converted
    via ffmpeg when available, which StepMania always supports) but falls back
    to whatever image yt-dlp wrote. Cached next to the audio as ``{slug}.*``.
    """
    cmd = _ytdlp_cmd()
    if not cmd:
        return None
    # Return a cached image if we already have one for this slug.
    for ext in _IMG_EXTS:
        cached = os.path.join(songs_dir, f"{slug}{ext}")
        if os.path.exists(cached):
            return cached
    os.makedirs(songs_dir, exist_ok=True)
    out_tmpl = os.path.join(songs_dir, f"{slug}.%(ext)s")
    try:
        progress("Downloading thumbnail", 32)
        subprocess.run(
            cmd + ["--no-playlist", "--skip-download", "--write-thumbnail",
                   "--convert-thumbnails", "jpg", "-o", out_tmpl, url],
            check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60, env=_utf8_env(),
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"download_thumbnail failed for {url!r}: {exc!r}")
    # Pick the resulting image (prefer jpg/png over webp).
    for ext in _IMG_EXTS:
        path = os.path.join(songs_dir, f"{slug}{ext}")
        if os.path.exists(path):
            return path
    return None


def is_playlist_url(url: str) -> bool:
    """True if the URL points at a YouTube playlist (has a ``list=`` param)."""
    return "list=" in (url or "")


def fetch_playlist_title(url: str) -> str:
    """Return the playlist's title (for the pack/output folder name)."""
    cmd = _ytdlp_cmd()
    if not cmd:
        return "Playlist"
    try:
        out = subprocess.run(
            cmd + ["--flat-playlist", "--playlist-items", "1",
                   "--print", "%(playlist_title)s", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=True, env=_utf8_env(),
        )
        title = " ".join((out.stdout or "").split())
        return title if title and title.upper() != "NA" else "Playlist"
    except Exception as exc:  # noqa: BLE001
        _log(f"fetch_playlist_title failed for {url!r}: {exc!r}")
        return "Playlist"


def fetch_playlist_entries(url: str) -> list[dict]:
    """List a playlist's videos as ``[{"url", "title"}, ...]`` (no download)."""
    cmd = _ytdlp_cmd()
    if not cmd:
        _log("yt-dlp not found; cannot enumerate playlist")
        return []
    try:
        out = subprocess.run(
            cmd + ["--flat-playlist", "--print", "%(id)s\t%(title)s", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, check=True, env=_utf8_env(),
        )
    except subprocess.CalledProcessError as exc:
        _log(f"fetch_playlist_entries failed for {url!r}: yt-dlp exited "
             f"{exc.returncode}: {(exc.stderr or '').strip()[:500]}")
        return []
    except Exception as exc:  # noqa: BLE001
        _log(f"fetch_playlist_entries failed for {url!r}: {exc!r}")
        return []

    entries: list[dict] = []
    for line in (out.stdout or "").splitlines():
        vid, _, vtitle = line.partition("\t")
        vid = vid.strip()
        if not vid or vid.upper() == "NA":
            continue
        entries.append({
            "url": f"https://www.youtube.com/watch?v={vid}",
            "title": _clean_line(vtitle),
        })
    return entries


def _place_artwork(image_path: str | None, folder: str, song_slug: str) -> str:
    """Copy the song image into ``folder`` with an ASCII name; return its name.

    Returns "" if there is no usable image. StepMania accepts the same file as
    both banner and background and scales it, so one thumbnail covers both.
    """
    if not image_path or not os.path.exists(image_path):
        return ""
    ext = os.path.splitext(image_path)[1].lower() or ".jpg"
    img_name = f"{song_slug}{ext}"
    try:
        shutil.copy2(image_path, os.path.join(folder, img_name))
        return img_name
    except Exception as exc:  # noqa: BLE001
        _log(f"could not place artwork in {folder!r}: {exc!r}")
        return ""


def run_ours(
    audio_path: str,
    title: str,
    artist: str,
    difficulties: list[str] | None,
    analysis: TimingAnalysis,
    out_root: str,
    progress=_noop,
    image_path: str | None = None,
) -> GeneratorResult:
    """Generate our charts into a playable folder and score them."""
    res = GeneratorResult(name="TempoSync + FootGraph")
    try:
        progress("Generating our charts", 55)
        sim = generate.generate_simfile(
            audio_path, title=title, artist=artist, difficulties=difficulties,
            analysis=analysis,
        )
        # StepMania requires Songs/<Group>/<Song>/file.sm (two levels of
        # nesting). We make the generator folder a "pack" (the group) and give
        # each song its own subfolder, so dropping the pack into Songs/ is
        # detected and a playlist becomes one pack with many song folders.
        pack = os.path.join(out_root, "Ours")
        song_slug = slugify(title)
        folder = os.path.join(pack, song_slug)
        os.makedirs(folder, exist_ok=True)
        # Use an ASCII filename for the audio too (the source mp3 may be named
        # with non-Latin characters); the in-game title still comes from #TITLE.
        ext = os.path.splitext(audio_path)[1] or ".mp3"
        music_name = f"{song_slug}{ext}"
        sim.music = music_name
        shutil.copy2(audio_path, os.path.join(folder, music_name))
        # Use the video thumbnail as banner + background artwork.
        img_name = _place_artwork(image_path, folder, song_slug)
        if img_name:
            sim.banner = img_name
            sim.background = img_name
        sm_path = os.path.join(folder, f"{song_slug}.sm")
        write_sm_file(sim, sm_path)

        for ch in sim.charts:
            m = metrics.compute_metrics(sim, ch, onset_times=analysis.onset_times)
            res.charts.append(m.as_dict())
        res.sm_path = sm_path
        # Expose the *pack* so downloads bundle a StepMania-ready group folder.
        res.folder = pack
    except Exception as exc:  # noqa: BLE001
        res.error = str(exc)
    return res


def run_autostepper(
    audio_path: str,
    title: str,
    out_root: str,
    analysis: TimingAnalysis,
    progress=_noop,
    image_path: str | None = None,
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
        # Same StepMania pack layout as run_ours: <pack>/<song>/file.sm.
        pack = os.path.join(out_root, "AutoStepper")
        song_slug = slugify(title)
        as_out = os.path.join(pack, song_slug)
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
            # Copy the .sm next to the audio in the final (ASCII-named) folder.
            ext = os.path.splitext(audio_path)[1] or ".mp3"
            music_name = f"{song_slug}{ext}"
            shutil.copy2(audio_path, os.path.join(as_out, music_name))
            sm_path = os.path.join(as_out, f"{song_slug}.sm")
            # Rewrite the #MUSIC tag so the chart points at the copied audio.
            bsim = parse_sm_file(tmp_sm)
            bsim.music = music_name
            # Use the video thumbnail as banner + background artwork.
            img_name = _place_artwork(image_path, as_out, song_slug)
            if img_name:
                bsim.banner = img_name
                bsim.background = img_name
            write_sm_file(bsim, sm_path)

        for ch in bsim.charts:
            m = metrics.compute_metrics(bsim, ch, onset_times=analysis.onset_times)
            res.charts.append(m.as_dict())
        res.sm_path = sm_path
        res.folder = pack
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

def _process_song(
    url: str,
    title: str | None,
    artist: str,
    difficulties: list[str],
    include_autostepper: bool,
    songs_dir: str,
    out_root: str,
    progress=_noop,
) -> PipelineResult:
    """Download + analyze one track and run every generator into ``out_root``.

    ``out_root`` is the shared job folder; each generator writes a
    StepMania pack (``<out_root>/Ours`` etc.) with a per-song subfolder.
    """
    if not title:
        title = fetch_title(url)
    slug = slugify(title)

    audio_path = download_audio(url, slug, songs_dir, progress=progress)
    # Grab the video thumbnail for banner/background artwork (best-effort).
    image_path = download_thumbnail(url, slug, songs_dir, progress=progress)

    progress("Analyzing audio (BPM, onsets)", 35)
    analysis = analyze_audio(audio_path)

    os.makedirs(out_root, exist_ok=True)
    result = PipelineResult(
        title=title, artist=artist, slug=slug, audio_path=audio_path,
        bpm=round(analysis.bpm, 2), offset=round(analysis.offset, 3),
        duration=round(analysis.duration, 1), output_dir=out_root,
    )

    result.generators.append(
        run_ours(audio_path, title, artist, difficulties, analysis, out_root,
                 progress, image_path=image_path)
    )
    if include_autostepper:
        result.generators.append(
            run_autostepper(audio_path, title, out_root, analysis, progress,
                            image_path=image_path)
        )
    # Pick up a manually-placed DDC export, if any.
    ddc = score_existing("DDC", os.path.join(out_root, "DDC", slug), analysis)
    if ddc.available and ddc.charts:
        result.generators.append(ddc)

    return result


def run_pipeline(
    url: str,
    title: str | None = None,
    artist: str = "Unknown",
    difficulties: list[str] | None = None,
    include_autostepper: bool = True,
    progress=_noop,
) -> PipelineResult:
    """Full pipeline for one YouTube URL (single song)."""
    root = repo_root()
    songs_dir = os.path.join(root, "songs")
    output_base = os.path.join(root, "output", "web")

    if difficulties is None:
        difficulties = ["Easy", "Medium", "Hard"]

    progress("Fetching track info", 5)
    if not title:
        title = fetch_title(url)
    slug = slugify(title)
    out_root = os.path.join(output_base, slug)

    result = _process_song(
        url, title, artist, difficulties, include_autostepper,
        songs_dir, out_root, progress,
    )

    progress("Writing comparison", 95)
    with open(os.path.join(out_root, "comparison.json"), "w", encoding="utf-8") as fh:
        json.dump(result.to_dict(), fh, indent=2)

    progress("Done", 100)
    return result


def run_job(
    url: str,
    title: str | None = None,
    artist: str = "Unknown",
    difficulties: list[str] | None = None,
    include_autostepper: bool = True,
    playlist: bool = False,
    progress=_noop,
) -> dict:
    """Dispatch to single-song or whole-playlist processing.

    Always returns ``{is_playlist, title, output_dir, songs: [...]}`` so the
    web UI can render one or many songs uniformly.
    """
    root = repo_root()
    songs_dir = os.path.join(root, "songs")
    output_base = os.path.join(root, "output", "web")

    if difficulties is None:
        difficulties = ["Easy", "Medium", "Hard"]

    # Single song -------------------------------------------------------------
    if not (playlist and is_playlist_url(url)):
        result = run_pipeline(
            url, title, artist, difficulties, include_autostepper, progress,
        )
        return {
            "is_playlist": False,
            "title": result.title,
            "output_dir": result.output_dir,
            "songs": [result.to_dict()],
        }

    # Whole playlist ----------------------------------------------------------
    progress("Reading playlist", 3)
    pl_title = fetch_playlist_title(url)
    entries = fetch_playlist_entries(url)
    if not entries:
        raise RuntimeError(
            "Couldn't read the playlist (yt-dlp returned no entries). "
            "Check the URL and that it is a public playlist."
        )

    pl_slug = slugify(pl_title)
    out_root = os.path.join(output_base, pl_slug)
    os.makedirs(out_root, exist_ok=True)

    songs: list[dict] = []
    n = len(entries)
    for i, entry in enumerate(entries):
        base = 5 + int(90 * i / n)
        span = max(1, int(90 / n))

        def _p(msg: str, pct: float = 0.0, _b=base, _s=span, _i=i, _n=n) -> None:
            scaled = _b + (_s * (pct / 100.0))
            progress(f"[{_i + 1}/{_n}] {msg}", min(95.0, scaled))

        try:
            result = _process_song(
                entry["url"], entry.get("title") or None, artist, difficulties,
                include_autostepper, songs_dir, out_root, _p,
            )
            songs.append(result.to_dict())
        except Exception as exc:  # noqa: BLE001
            _log(f"playlist entry {i + 1}/{n} failed "
                 f"({entry.get('url')!r}): {exc!r}")
            songs.append({
                "title": entry.get("title") or "Unknown",
                "artist": artist, "slug": "", "audio_path": "",
                "bpm": 0, "offset": 0, "duration": 0,
                "output_dir": out_root, "generators": [],
                "error": str(exc),
            })

    job = {
        "is_playlist": True,
        "title": pl_title,
        "output_dir": out_root,
        "songs": songs,
    }
    progress("Writing comparison", 95)
    with open(os.path.join(out_root, "comparison.json"), "w", encoding="utf-8") as fh:
        json.dump(job, fh, indent=2)

    progress("Done", 100)
    return job

