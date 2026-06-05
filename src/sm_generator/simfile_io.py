"""Reading and writing StepMania `.sm` simfiles.

Only the subset needed by this project is supported: a single music track,
constant or piecewise BPMs, an offset, and one or more `dance-single` note
charts. Note values follow the StepMania convention:

    0 = empty, 1 = tap, 2 = hold head, 3 = hold/roll tail, 4 = roll head,
    M = mine.

A chart is stored as a list of "measures"; each measure is a list of rows;
each row is a 4-character string (one char per panel: Left, Down, Up, Right).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


PANELS = ("L", "D", "U", "R")
EMPTY_ROW = "0000"


@dataclass
class Chart:
    """A single difficulty chart in `dance-single` mode."""

    steps_type: str = "dance-single"
    description: str = ""
    difficulty: str = "Medium"
    meter: int = 1
    # measures[i] is a list of rows; each row is a 4-char string.
    measures: list[list[str]] = field(default_factory=list)

    def iter_rows(self):
        """Yield (measure_index, row_index, rows_in_measure, row_string)."""
        for mi, measure in enumerate(self.measures):
            n = len(measure)
            for ri, row in enumerate(measure):
                yield mi, ri, n, row

    def note_rows(self):
        """Yield (beat, row_string) for every non-empty row.

        `beat` is in quarter-note beats from the start of the chart.
        """
        for mi, measure in enumerate(self.measures):
            n = len(measure) or 1
            for ri, row in enumerate(measure):
                if row != EMPTY_ROW:
                    beat = mi * 4.0 + (ri / n) * 4.0
                    yield beat, row


@dataclass
class Simfile:
    title: str = ""
    artist: str = ""
    music: str = ""
    banner: str = ""
    background: str = ""
    offset: float = 0.0
    # list of (beat, bpm) pairs, sorted by beat.
    bpms: list[tuple[float, float]] = field(default_factory=lambda: [(0.0, 120.0)])
    sample_start: float = 0.0
    sample_length: float = 30.0
    charts: list[Chart] = field(default_factory=list)

    # -- convenience ------------------------------------------------------
    @property
    def primary_bpm(self) -> float:
        return self.bpms[0][1] if self.bpms else 120.0


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"#([A-Z0-9]+):(.*?);", re.DOTALL)


def _strip_comments(text: str) -> str:
    return re.sub(r"//[^\n]*", "", text)


def _parse_bpms(raw: str) -> list[tuple[float, float]]:
    bpms: list[tuple[float, float]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        beat_s, bpm_s = chunk.split("=", 1)
        try:
            bpms.append((float(beat_s), float(bpm_s)))
        except ValueError:
            continue
    bpms.sort(key=lambda x: x[0])
    return bpms or [(0.0, 120.0)]


def _parse_notes_value(value: str) -> Chart | None:
    """Parse the body of a #NOTES: tag into a Chart."""
    # The notes value has 6 colon-separated header fields, then the note data.
    parts = value.split(":")
    if len(parts) < 6:
        return None
    steps_type = parts[0].strip()
    description = parts[1].strip()
    difficulty = parts[2].strip()
    try:
        meter = int(parts[3].strip())
    except ValueError:
        meter = 1
    note_data = ":".join(parts[5:])

    measures: list[list[str]] = []
    for measure_block in note_data.split(","):
        rows = [r.strip() for r in measure_block.splitlines()]
        rows = [r for r in rows if r and re.fullmatch(r"[0-9MmLlFf]{4}", r)]
        if rows:
            measures.append([r.upper() for r in rows])
    return Chart(
        steps_type=steps_type,
        description=description,
        difficulty=difficulty,
        meter=meter,
        measures=measures,
    )


def parse_sm(text: str) -> Simfile:
    """Parse `.sm` text into a Simfile object."""
    text = _strip_comments(text)
    sim = Simfile()
    for m in _TAG_RE.finditer(text):
        tag = m.group(1).upper()
        value = m.group(2).strip()
        if tag == "TITLE":
            sim.title = value
        elif tag == "ARTIST":
            sim.artist = value
        elif tag == "MUSIC":
            sim.music = value
        elif tag == "BANNER":
            sim.banner = value
        elif tag == "BACKGROUND":
            sim.background = value
        elif tag == "OFFSET":
            try:
                sim.offset = float(value)
            except ValueError:
                pass
        elif tag == "BPMS":
            sim.bpms = _parse_bpms(value)
        elif tag == "SAMPLESTART":
            try:
                sim.sample_start = float(value)
            except ValueError:
                pass
        elif tag == "SAMPLELENGTH":
            try:
                sim.sample_length = float(value)
            except ValueError:
                pass
        elif tag == "NOTES":
            chart = _parse_notes_value(value)
            if chart is not None:
                sim.charts.append(chart)
    return sim


def parse_sm_file(path: str) -> Simfile:
    with open(path, encoding="utf-8", errors="ignore") as f:
        return parse_sm(f.read())


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def _format_bpms(bpms: list[tuple[float, float]]) -> str:
    return ",".join(f"{beat:.3f}={bpm:.3f}" for beat, bpm in bpms)


def _format_chart(chart: Chart) -> str:
    lines = ["#NOTES:"]
    lines.append(f"     {chart.steps_type}:")
    lines.append(f"     {chart.description}:")
    lines.append(f"     {chart.difficulty}:")
    lines.append(f"     {chart.meter}:")
    lines.append("     0.000,0.000,0.000,0.000,0.000:")
    measure_strs = []
    for measure in chart.measures:
        measure_strs.append("\n".join(measure) if measure else EMPTY_ROW)
    lines.append("\n,\n".join(measure_strs))
    return "\n".join(lines) + "\n;"


def write_sm(sim: Simfile) -> str:
    """Serialize a Simfile to `.sm` text."""
    out = [
        f"#TITLE:{sim.title};",
        f"#ARTIST:{sim.artist};",
        f"#MUSIC:{sim.music};",
    ]
    # Optional artwork (e.g. a YouTube thumbnail used as banner + background).
    if sim.banner:
        out.append(f"#BANNER:{sim.banner};")
    if sim.background:
        out.append(f"#BACKGROUND:{sim.background};")
    out += [
        f"#OFFSET:{sim.offset:.6f};",
        f"#BPMS:{_format_bpms(sim.bpms)};",
        f"#SAMPLESTART:{sim.sample_start:.3f};",
        f"#SAMPLELENGTH:{sim.sample_length:.3f};",
        "#SELECTABLE:YES;",
        "#STOPS:;",
    ]
    for chart in sim.charts:
        out.append(_format_chart(chart))
    return "\n".join(out) + "\n"


def write_sm_file(sim: Simfile, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(write_sm(sim))
