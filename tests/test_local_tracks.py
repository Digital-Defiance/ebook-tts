from __future__ import annotations

from pathlib import Path

import pytest

from ebook_tts.config import load_config
from ebook_tts.errors import ConfigError
from ebook_tts.models import LocalTrackOverride, SpokenReplaceRule
from ebook_tts.providers.local_tracks import (
    apply_spoken_replacements,
    effective_local_config,
    match_track_override,
)
from ebook_tts.providers.local_turns import plan_spoken_text, tagged_payloads
from ebook_tts.config import default_config
from dataclasses import replace


def test_load_track_overrides(tmp_path: Path) -> None:
  phrase = tmp_path / "patches" / "fix.wav"
  phrase.parent.mkdir()
  phrase.write_bytes(b"RIFF....")
  config = tmp_path / "audiobook.toml"
  config.write_text(
      f'''
[tts]
provider = "local"
voice_id = "narrator"
model_id = "mlx-community/fish-audio-s2-pro"
max_characters = 200000

[tts.local]
reference_wav = "voices/a.wav"
reference_text = "voices/a.txt"
max_words_per_call = 0

[[tts.local.tracks]]
chapter = 13
max_words_per_call = 1100

[[tts.local.tracks]]
chapter = 15
spoken_replace = [
  {{ from = "*Open Channel*", to = "Open-Channel" }},
]
patches = [
  {{ phrase = "patches/fix.wav", start = 1.0, end = 2.5, replace_unintelligible = true }},
]
''',
      encoding="utf-8",
  )
  loaded = load_config(config)
  assert loaded.tts.local.max_words_per_call == 0
  assert len(loaded.tts.local.tracks) == 2
  first = loaded.tts.local.tracks[0]
  assert first.chapter == 13
  assert first.max_words_per_call == 1100
  second = loaded.tts.local.tracks[1]
  assert second.spoken_replace[0].old == "*Open Channel*"
  assert second.patches[0].phrase.endswith("patches/fix.wav")
  assert second.patches[0].start == 1.0


def test_track_override_requires_match_key(tmp_path: Path) -> None:
  config = tmp_path / "bad.toml"
  config.write_text(
      '''
[tts.local]
[[tts.local.tracks]]
max_words_per_call = 1100
''',
      encoding="utf-8",
  )
  with pytest.raises(ConfigError, match="chapter and/or track"):
    load_config(config)


def test_match_and_effective_local_config() -> None:
  tracks = (
      LocalTrackOverride(chapter=13, max_words_per_call=1100),
      LocalTrackOverride(
          chapter=15,
          spoken_replace=(SpokenReplaceRule(old="*A*", new="A"),),
      ),
  )
  hit = match_track_override(tracks, chapter_number=13, track_number=5)
  assert hit is not None and hit.max_words_per_call == 1100
  base = default_config().tts.local
  assert effective_local_config(base, hit).max_words_per_call == 1100
  assert match_track_override(tracks, chapter_number=99, track_number=1) is None


def test_spoken_replace_is_generation_only() -> None:
  rules = (SpokenReplaceRule(old="*Open Channel*", new="Open-Channel"),)
  text = "In a footnote, *Open Channel* was named."
  assert apply_spoken_replacements(text, rules) == (
      "In a footnote, Open-Channel was named."
  )
  with pytest.raises(ValueError, match="exactly once"):
    apply_spoken_replacements("none", rules)

  local = replace(default_config().tts.local, verbalize_numerals=False)
  spoken = plan_spoken_text(text, local, spoken_replace=rules)
  assert "Open-Channel" in spoken
  assert "*Open Channel*" not in spoken


def test_per_track_max_words_packs_payloads() -> None:
  local = replace(
      default_config().tts.local,
      max_words_per_call=0,
      sentence_pause="none",
      sentence_turns=False,
      anchor=False,
  )
  turns = (
      "One two three four five six.",
      "Seven eight nine ten eleven twelve.",
      "Thirteen fourteen fifteen.",
  )
  assert len(tagged_payloads(turns, local)) == 1
  limited = replace(local, max_words_per_call=12)
  assert len(tagged_payloads(turns, limited)) == 2
