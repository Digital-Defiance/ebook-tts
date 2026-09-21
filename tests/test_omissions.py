"""Unit tests for omission diagnosis helpers."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ebook_tts.qa.omissions import (
    diagnose_omissions,
    energy_pauses,
    find_gaps,
    format_find_report,
    prose_context,
)
from ebook_tts.qa.scoring import normalized_tokens


def test_find_gaps_reports_real_deletion() -> None:
  expected = normalized_tokens(
      "Ravi Anand said it again and this time he read the dates carefully aloud."
  )
  heard = normalized_tokens("Ravi said it again carefully aloud.")
  gaps = find_gaps(expected, heard, min_gap=5)
  assert gaps
  assert gaps[0]["tag"] in {"delete", "replace"}
  assert gaps[0]["gap"] >= 5
  joined = " ".join(gaps[0]["expected"])
  assert "anand" in joined or "dates" in joined or "time" in joined


def test_find_gaps_skips_benign_numeral_replace() -> None:
  expected = normalized_tokens("The year was 1952 in London.")
  heard = normalized_tokens("The year was nineteen fifty two in London.")
  gaps = find_gaps(expected, heard, min_gap=1)
  assert gaps == []


def test_prose_context_anchors_missing_run() -> None:
  prose = "She opened the door and walked into the quiet library without a word."
  missing = ["walked", "into", "the", "quiet", "library"]
  context = prose_context(prose, missing, width=20)
  assert "quiet library" in context
  assert "…" in context or "opened" in context


def test_energy_pauses_detect_quiet_span() -> None:
  rate = 16_000
  speech = np.sin(2 * np.pi * 220 * np.arange(rate) / rate).astype(np.float32) * 0.2
  silence = np.zeros(int(rate * 0.2), dtype=np.float32)
  audio = np.concatenate([speech, silence, speech])
  pauses = energy_pauses(audio, rate, 0.0, len(audio) / rate, floor_db=-40.0, min_ms=50.0)
  assert pauses
  mid = pauses[0]
  assert 0.8 < mid[0] < 1.2
  assert mid[1] > mid[0]


def test_diagnose_omissions_suggests_region_from_audio() -> None:
  expected = (
      "One two three four five six seven eight nine ten "
      "eleven twelve thirteen fourteen fifteen sixteen."
  )
  heard = "One two three four five six seven eight nine ten sixteen."
  rate = 8_000
  # Roughly place a quiet seam after ~10/16 of duration.
  duration = 3.2
  n = int(rate * duration)
  audio = (np.sin(2 * np.pi * 180 * np.arange(n) / rate) * 0.15).astype(np.float32)
  seam = int(rate * 2.0)
  audio[seam : seam + int(rate * 0.15)] = 0.0
  gaps = diagnose_omissions(
      expected,
      heard,
      min_gap=5,
      audio=audio,
      sample_rate=rate,
  )
  assert len(gaps) == 1
  assert gaps[0].suggest_start is not None
  assert gaps[0].suggest_end is not None
  assert gaps[0].suggest_end > gaps[0].suggest_start
  report = format_find_report(gaps, label="unit", min_gap=5)
  assert "missing" in report
  assert "suggest" in report


def test_diagnose_find_cli_with_text_files(tmp_path: Path, capsys) -> None:
  from ebook_tts.cli import main

  expected = tmp_path / "expected.txt"
  heard = tmp_path / "heard.txt"
  expected.write_text(
      "Alpha beta gamma delta epsilon zeta eta theta iota kappa lambda.",
      encoding="utf-8",
  )
  heard.write_text("Alpha beta gamma kappa lambda.", encoding="utf-8")
  code = main(
      [
          "diagnose",
          "find",
          "--expected",
          str(expected),
          "--heard",
          str(heard),
          "--min-gap",
          "5",
      ]
  )
  assert code == 1
  out = capsys.readouterr().out
  assert "real omission" in out
  assert "delta" in out or "epsilon" in out


def test_diagnose_find_cli_json_clean(tmp_path: Path, capsys) -> None:
  from ebook_tts.cli import main

  expected = tmp_path / "expected.txt"
  heard = tmp_path / "heard.txt"
  text = "The neighbour organised travelling in 1952."
  expected.write_text(text, encoding="utf-8")
  heard.write_text(
      "The neighbor organized traveling in nineteen fifty two.",
      encoding="utf-8",
  )
  code = main(
      [
          "diagnose",
          "find",
          "--expected",
          str(expected),
          "--heard",
          str(heard),
          "--json",
      ]
  )
  assert code == 0
  payload = __import__("json").loads(capsys.readouterr().out)
  assert payload["omissions"] == []
