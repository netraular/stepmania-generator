"""sm_generator — modern StepMania chart generator (dance-single).

A staged, validation-first generator:
  - Stage 1 (hybrid): real BPM/offset (librosa) + IA step timing (ddc_onset)
    + rule-based, pad-friendly step selection.
  - Stage 2 (optional ML): learned step selection trained on human charts.

The validation harness (`metrics`, `simfile_io`) lets us prove an output is
actually better before adopting any new approach.
"""

__version__ = "0.1.0"
