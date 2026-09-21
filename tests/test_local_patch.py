from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from ebook_tts.models import LocalAudioPatch
from ebook_tts.providers.local_patch import (
    apply_audio_patch,
    apply_audio_patches,
    rewrite_mp3_with_patches,
)
from ebook_tts.errors import ProviderError


def _tone(seconds: float, rate: int, hz: float = 220.0, level: float = 0.2) -> np.ndarray:
  return (
      level * np.sin(2 * np.pi * hz * np.arange(int(rate * seconds)) / rate)
  ).astype(np.float32)


def _rms(audio: np.ndarray) -> float:
  return float(np.sqrt(np.mean(audio.astype(np.float64) ** 2) + 1e-20))


def test_silent_omission_splice_preserves_prefix_and_suffix(tmp_path: Path) -> None:
  rate = 24_000
  left = _tone(1.0, rate, 220.0)
  hole = np.zeros(int(rate * 0.5), dtype=np.float32)
  right = _tone(1.0, rate, 330.0)
  audio = np.concatenate([left, hole, right])
  phrase = _tone(0.3, rate, 440.0)
  phrase_path = tmp_path / "phrase.wav"
  sf.write(phrase_path, phrase, rate)
  start = left.size / rate
  end = (left.size + hole.size) / rate
  patch = LocalAudioPatch(
      phrase=str(phrase_path),
      start=start,
      end=end,
      replace_unintelligible=False,
  )
  patched, record = apply_audio_patch(
      audio, rate, patch, phrase_audio=phrase, phrase_rate=rate
  )
  assert patched.size == audio.size
  assert record["kind"] == "verified_silent_omission"
  assert np.allclose(patched[: left.size], left, atol=1e-5)
  assert np.allclose(patched[-right.size :], right, atol=1e-5)
  region = patched[left.size : left.size + hole.size]
  assert _rms(region) > _rms(hole) * 10
  # Seam windows: last 20 ms of left and first 20 ms of right stay intact.
  seam = int(rate * 0.020)
  assert np.allclose(patched[left.size - seam : left.size], left[-seam:], atol=1e-5)
  assert np.allclose(
      patched[left.size + hole.size : left.size + hole.size + seam],
      right[:seam],
      atol=1e-5,
  )


def test_duration_changing_splice_shifts_suffix(tmp_path: Path) -> None:
  rate = 24_000
  left = _tone(0.8, rate, 200.0)
  bad = _tone(0.1, rate, 100.0, level=0.05)
  right = _tone(0.8, rate, 300.0)
  audio = np.concatenate([left, bad, right])
  phrase = _tone(0.4, rate, 450.0)
  phrase_path = tmp_path / "long-phrase.wav"
  sf.write(phrase_path, phrase, rate)
  start = left.size / rate
  end = (left.size + bad.size) / rate
  patch = LocalAudioPatch(
      phrase=str(phrase_path),
      start=start,
      end=end,
      replace_unintelligible=True,
      allow_duration_change=True,
  )
  patched, record = apply_audio_patch(
      audio, rate, patch, phrase_audio=phrase, phrase_rate=rate
  )
  assert record["sample_delta"] == phrase.size - bad.size
  assert patched.size == audio.size + record["sample_delta"]
  assert np.allclose(patched[: left.size], left, atol=1e-5)
  assert np.allclose(patched[-right.size :], right, atol=1e-5)
  inserted = patched[left.size : left.size + phrase.size]
  assert _rms(inserted) > 0.01


def test_refuses_non_silent_region_without_flag(tmp_path: Path) -> None:
  rate = 16_000
  audio = _tone(1.0, rate, 220.0)
  phrase = _tone(0.2, rate, 440.0)
  phrase_path = tmp_path / "p.wav"
  sf.write(phrase_path, phrase, rate)
  patch = LocalAudioPatch(
      phrase=str(phrase_path),
      start=0.2,
      end=0.4,
      replace_unintelligible=False,
  )
  with pytest.raises(ProviderError, match="non-silent"):
    apply_audio_patch(audio, rate, patch, phrase_audio=phrase, phrase_rate=rate)


def test_multiple_patches_apply_later_first(tmp_path: Path) -> None:
  rate = 16_000
  audio = np.concatenate(
      [
          _tone(0.5, rate, 200.0),
          np.zeros(int(rate * 0.25), dtype=np.float32),
          _tone(0.5, rate, 250.0),
          np.zeros(int(rate * 0.25), dtype=np.float32),
          _tone(0.5, rate, 300.0),
      ]
  )
  phrase = _tone(0.15, rate, 500.0)
  path = tmp_path / "p.wav"
  sf.write(path, phrase, rate)
  patches = (
      LocalAudioPatch(phrase=str(path), start=0.5, end=0.75, replace_unintelligible=False),
      LocalAudioPatch(phrase=str(path), start=1.25, end=1.5, replace_unintelligible=False),
  )
  result = apply_audio_patches(audio, rate, patches)
  assert len(result.records) == 2
  assert result.sample_delta == 0
  assert _rms(result.audio[int(0.5 * rate) : int(0.75 * rate)]) > 0.01
  assert _rms(result.audio[int(1.25 * rate) : int(1.5 * rate)]) > 0.01


def test_rewrite_mp3_with_patches_round_trip(
    tmp_path: Path, media_tools
) -> None:
  rate = 44_100
  left = _tone(0.6, rate, 220.0)
  hole = np.zeros(int(rate * 0.4), dtype=np.float32)
  right = _tone(0.6, rate, 330.0)
  wav = tmp_path / "chapter.wav"
  sf.write(wav, np.concatenate([left, hole, right]), rate)
  mp3 = tmp_path / "chapter.mp3"
  import subprocess

  subprocess.run(
      [
          media_tools.ffmpeg,
          "-hide_banner",
          "-loglevel",
          "error",
          "-y",
          "-i",
          str(wav),
          "-b:a",
          "128k",
          str(mp3),
      ],
      check=True,
  )
  phrase = _tone(0.25, rate, 440.0)
  phrase_path = tmp_path / "phrase.wav"
  sf.write(phrase_path, phrase, rate)
  patch = LocalAudioPatch(
      phrase=str(phrase_path),
      start=left.size / rate,
      end=(left.size + hole.size) / rate,
      replace_unintelligible=False,
  )
  info, records = rewrite_mp3_with_patches(
      mp3_path=mp3,
      patches=(patch,),
      tools=media_tools,
      output_format="mp3_44100_128",
      title="Opening",
      album="Mini",
      artist="Author",
      genre="Audiobook",
      track_number=1,
      track_count=1,
  )
  assert mp3.is_file()
  assert info.duration_seconds > 1.0
  assert records[0]["kind"] == "verified_silent_omission"
