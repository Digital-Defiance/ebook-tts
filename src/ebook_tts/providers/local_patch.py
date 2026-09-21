"""Verified phrase splice for local Fish omission / unintelligible regions.

Port of frontier-book's patch_verified_omission safety rules. Operates on PCM so
callers can decode MP3 tracks, patch, and re-encode. Manuscript ASR expected text
is unchanged; only the delivered waveform is repaired.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ProviderError
from ..models import LocalAudioPatch
from ..utils import sha256_file
from .local_assembly import active_rms, comfort_gap


@dataclass(frozen=True)
class PatchResult:
  audio: Any
  rate: int
  records: tuple[dict[str, Any], ...]
  sample_delta: int


def _numpy() -> Any:
  try:
    import numpy as np
  except ImportError as exc:
    raise ProviderError(
        "Local audio patches require numpy. Install ebook-tts[local]."
    ) from exc
  return np


def load_mono(path: Path) -> tuple[Any, int]:
  try:
    import soundfile as sf
  except ImportError as exc:
    raise ProviderError("soundfile is required for local audio patches.") from exc
  np = _numpy()
  audio, rate = sf.read(str(path), always_2d=False)
  audio = np.asarray(audio, dtype=np.float32)
  if audio.ndim > 1:
    audio = audio.mean(axis=1)
  return audio, int(rate)


def trim_phrase(audio: Any, rate: int, floor_db: float = -45.0) -> Any:
  np = _numpy()
  frame = max(1, int(rate * 0.010))
  usable = audio[: audio.size - audio.size % frame]
  if usable.size == 0:
    raise ProviderError("patch phrase contains no speech")
  levels = np.sqrt(
      (usable.reshape(-1, frame).astype(np.float64) ** 2).mean(axis=1) + 1e-20
  )
  peak = float(levels.max()) if levels.size else 0.0
  live = (
      np.flatnonzero(levels >= peak * 10 ** (floor_db / 20))
      if peak > 0
      else np.zeros(0, int)
  )
  if live.size == 0:
    raise ProviderError("patch phrase contains no speech")
  return audio[live[0] * frame : min(audio.size, (live[-1] + 1) * frame)]


def apply_audio_patch(
    audio: Any,
    rate: int,
    patch: LocalAudioPatch,
    *,
    phrase_audio: Any | None = None,
    phrase_rate: int | None = None,
) -> tuple[Any, dict[str, Any]]:
  """Splice one verified phrase into PCM. Returns (patched, record)."""
  np = _numpy()
  if phrase_audio is None:
    phrase_audio, phrase_rate = load_mono(Path(patch.phrase))
  if phrase_rate != rate:
    raise ProviderError(
        f"patch phrase rate {phrase_rate} does not match chapter rate {rate}"
    )
  phrase = trim_phrase(np.asarray(phrase_audio, dtype=np.float32), rate)

  start = int(round(patch.start * rate))
  end = int(round(patch.end * rate))
  if not 0 <= start < end <= audio.size:
    raise ProviderError("patch region is outside the chapter audio")
  region = audio[start:end]
  chapter_level = active_rms(audio, rate)
  region_rms = float(np.sqrt(np.mean(region.astype(np.float64) ** 2) + 1e-20))
  relative_db = 20 * np.log10((region_rms + 1e-20) / (chapter_level + 1e-20))
  if relative_db > -20.0 and not patch.replace_unintelligible:
    raise ProviderError(
        f"refusing to overwrite non-silent material: region is {relative_db:.1f} dB "
        "relative to chapter speech; set replace_unintelligible after ASR proves "
        "the expected words are absent"
    )
  if phrase.size > region.size and not patch.allow_duration_change:
    raise ProviderError(
        f"phrase is {phrase.size / rate:.3f}s but region is {region.size / rate:.3f}s; "
        "set allow_duration_change rather than time-compressing speech"
    )

  phrase_level = active_rms(phrase, rate)
  if phrase_level > 0 and chapter_level > 0:
    gain_db = float(np.clip(20 * np.log10(chapter_level / phrase_level), -3.0, 3.0))
    phrase = phrase * 10 ** (gain_db / 20)
  else:
    gain_db = 0.0
  peak = float(np.abs(phrase).max())
  if peak > 0.99:
    phrase *= 0.99 / peak

  fade = min(int(rate * 0.030), phrase.size // 4, region.size // 4)
  clip = phrase.copy()
  if fade:
    clip[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
    clip[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)

  if phrase.size > region.size:
    patched = np.concatenate([audio[:start], clip, audio[end:]]).astype(np.float32)
    position = start
  else:
    patched = audio.copy()
    if patch.replace_unintelligible:
      donors = [audio[:start], audio[end:]]
      replacement = comfort_gap(donors, region.size, rate, seed=15015)
      if fade:
        alpha = np.linspace(0, 1, fade, dtype=np.float32)
        replacement[:fade] = region[:fade] * (1 - alpha) + replacement[:fade] * alpha
        replacement[-fade:] = (
            replacement[-fade:] * (1 - alpha) + region[-fade:] * alpha
        )
      patched[start:end] = replacement
    position = start + (region.size - phrase.size) // 2
    patched[position : position + clip.size] += clip

  sample_delta = int(patched.size - audio.size)
  record = {
      "kind": (
          "verified_unintelligible_replacement"
          if patch.replace_unintelligible
          else "verified_silent_omission"
      ),
      "phrase_path": patch.phrase,
      "phrase_sha256": sha256_file(Path(patch.phrase))
      if Path(patch.phrase).is_file()
      else "",
      "region_start_sample": start,
      "region_end_sample": end,
      "phrase_start_sample": position,
      "phrase_end_sample": position + phrase.size,
      "sample_delta": sample_delta,
      "region_relative_db": round(float(relative_db), 2),
      "gain_db": round(gain_db, 3),
      "announcement_text": patch.announcement_text or "",
  }
  return patched, record


def apply_audio_patches(
    audio: Any,
    rate: int,
    patches: tuple[LocalAudioPatch, ...],
) -> PatchResult:
  """Apply later-in-file patches first so earlier region times stay valid."""
  if not patches:
    return PatchResult(audio=audio, rate=rate, records=(), sample_delta=0)
  working = audio
  records: list[dict[str, Any]] = []
  original_size = int(audio.size)
  for patch in sorted(patches, key=lambda item: -item.start):
    working, record = apply_audio_patch(working, rate, patch)
    records.append(record)
  records.reverse()
  return PatchResult(
      audio=working,
      rate=rate,
      records=tuple(records),
      sample_delta=int(working.size - original_size),
  )


def rewrite_mp3_with_patches(
    *,
    mp3_path: Path,
    patches: tuple[LocalAudioPatch, ...],
    tools: Any,
    output_format: str,
    title: str,
    album: str,
    artist: str,
    genre: str,
    track_number: int,
    track_count: int,
    cover_path: Path | None = None,
) -> tuple[Any, tuple[dict[str, Any], ...]]:
  """Decode a finished track, apply verified phrase splices, re-encode in place."""
  if not patches:
    raise ProviderError("rewrite_mp3_with_patches requires at least one patch")
  try:
    import soundfile as sf
  except ImportError as exc:
    raise ProviderError("soundfile is required for local audio patches.") from exc
  from ..media.tools import (
      decode_audio,
      expected_audio_format,
      fsync_directory,
      probe_audio,
      run_process,
  )

  np = _numpy()
  work = mp3_path.parent / f".{mp3_path.stem}.patch-{os.getpid()}"
  work.mkdir(parents=True, exist_ok=True)
  try:
    wav_in = work / "in.wav"
    wav_out = work / "out.wav"
    mp3_tmp = work / "out.mp3"
    decode = run_process(
        [
            tools.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(mp3_path),
            "-ac",
            "1",
            str(wav_in),
        ],
        timeout=1800,
    )
    if decode.returncode != 0:
      raise ProviderError(
          f"could not decode track for patching: "
          f"{(decode.stderr or decode.stdout).strip()}"
      )
    audio, rate = sf.read(str(wav_in), always_2d=False)
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim > 1:
      audio = audio.mean(axis=1)
    result = apply_audio_patches(audio, rate, patches)
    sf.write(str(wav_out), result.audio, rate, subtype="PCM_16")
    codec, sample_rate, bit_rate = expected_audio_format(output_format)
    if codec != "mp3":
      raise ProviderError("Local patch rewrite currently supports MP3 tracks only.")
    command = [
        tools.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(wav_out),
    ]
    if cover_path is not None:
      command.extend(["-i", str(cover_path), "-map", "0:a:0", "-map", "1:v:0"])
    else:
      command.extend(["-map", "0:a:0"])
    command.extend(
        [
            "-ar",
            str(sample_rate),
            "-b:a",
            str(bit_rate),
            "-id3v2_version",
            "3",
            "-metadata",
            f"title={title}",
            "-metadata",
            f"album={album}",
            "-metadata",
            f"artist={artist}",
            "-metadata",
            f"album_artist={artist}",
            "-metadata",
            f"genre={genre}",
            "-metadata",
            f"track={track_number}/{track_count}",
        ]
    )
    if cover_path is not None:
      command.extend(
          [
              "-c:v",
              "copy",
              "-disposition:v:0",
              "attached_pic",
              "-metadata:s:v:0",
              "title=Cover",
          ]
      )
    command.append(str(mp3_tmp))
    encode = run_process(command, timeout=1800)
    if encode.returncode != 0:
      raise ProviderError(
          f"could not re-encode patched track: "
          f"{(encode.stderr or encode.stdout).strip()}"
      )
    decode_audio(mp3_tmp, tools.ffmpeg)
    os.replace(mp3_tmp, mp3_path)
    fsync_directory(mp3_path.parent)
    return (
        probe_audio(mp3_path, tools.ffprobe, expected_format=output_format),
        result.records,
    )
  finally:
    shutil.rmtree(work, ignore_errors=True)
