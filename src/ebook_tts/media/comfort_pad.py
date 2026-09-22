"""Inter-chapter comfort pads for M4B packaging.

Concatenating chapter MP3s with nothing between them makes the last sentence of
one chapter run into the next chapter's announcement. A digital-silence pad is
also wrong for local Fish audio: pure zeros drop the room tone the listener has
been hearing (the "background opens up" defect). Pads therefore reuse the local
renderer's ``comfort_gap`` V6 tone shaped from the preceding chapter whenever
the local extra is available, and fall back to equal-length silence otherwise so
timing and chapter markers still stay exact.
"""

from __future__ import annotations

import math
import os
import shutil
from pathlib import Path

from ..errors import MediaError
from .tools import MediaTools, probe_audio, run_process


def write_inter_chapter_pad_mp3(
    *,
    donor_mp3: Path,
    output_mp3: Path,
    seconds: float,
    tools: MediaTools,
    seed: int,
    sample_rate: int = 44_100,
    bit_rate: int = 128_000,
) -> float:
  """Write a comfort-tone (or silent) pad MP3; return its probed duration."""
  if not math.isfinite(seconds) or seconds <= 0:
    raise MediaError(f"Inter-chapter pad length must be positive; got {seconds!r}.")
  if donor_mp3.is_symlink() or not donor_mp3.is_file():
    raise MediaError(f"Pad donor track is missing or unsafe: {donor_mp3}")
  if output_mp3.is_symlink() or output_mp3.exists():
    raise MediaError(f"Refusing to replace existing pad: {output_mp3}")

  output_mp3.parent.mkdir(parents=True, exist_ok=True)
  work = output_mp3.parent / f".{output_mp3.stem}.{os.getpid()}.pad-work"
  if work.exists():
    shutil.rmtree(work)
  work.mkdir(parents=True)
  try:
    if _try_write_comfort_pad(
        donor_mp3=donor_mp3,
        output_mp3=output_mp3,
        work=work,
        seconds=seconds,
        tools=tools,
        seed=seed,
        sample_rate=sample_rate,
        bit_rate=bit_rate,
    ):
      return float(probe_audio(output_mp3, tools.ffprobe).duration_seconds)
    _write_silence_pad_mp3(
        output_mp3=output_mp3,
        seconds=seconds,
        ffmpeg=tools.ffmpeg,
        sample_rate=sample_rate,
        bit_rate=bit_rate,
    )
    return float(probe_audio(output_mp3, tools.ffprobe).duration_seconds)
  finally:
    shutil.rmtree(work, ignore_errors=True)


def _try_write_comfort_pad(
    *,
    donor_mp3: Path,
    output_mp3: Path,
    work: Path,
    seconds: float,
    tools: MediaTools,
    seed: int,
    sample_rate: int,
    bit_rate: int,
) -> bool:
  """Return True when a comfort-tone pad was written."""
  try:
    import numpy as np
    import soundfile as sf

    from ..providers.local_assembly import comfort_gap
  except ImportError:
    return False

  donor_wav = work / "donor.wav"
  pad_wav = work / "pad.wav"
  decode = run_process(
      [
          tools.ffmpeg,
          "-hide_banner",
          "-loglevel",
          "error",
          "-y",
          "-i",
          str(donor_mp3),
          "-ac",
          "1",
          "-ar",
          str(sample_rate),
          str(donor_wav),
      ],
      timeout=1800,
  )
  if decode.returncode != 0 or not donor_wav.is_file():
    return False

  data, rate = sf.read(str(donor_wav), always_2d=False)
  donor = np.asarray(data, dtype=np.float32)
  if donor.ndim > 1:
    donor = donor.mean(axis=1)
  if rate != sample_rate:
    return False
  samples = max(1, round(seconds * sample_rate))
  pad = comfort_gap([donor], samples, sample_rate, seed=seed)
  if pad.size != samples:
    pad = np.zeros(samples, dtype=np.float32)
  sf.write(str(pad_wav), pad.astype(np.float32), sample_rate, subtype="PCM_16")
  encode = run_process(
      [
          tools.ffmpeg,
          "-hide_banner",
          "-loglevel",
          "error",
          "-y",
          "-i",
          str(pad_wav),
          "-ar",
          str(sample_rate),
          "-b:a",
          str(bit_rate),
          str(output_mp3),
      ],
      timeout=600,
  )
  return encode.returncode == 0 and output_mp3.is_file()


def _write_silence_pad_mp3(
    *,
    output_mp3: Path,
    seconds: float,
    ffmpeg: str,
    sample_rate: int,
    bit_rate: int,
) -> None:
  result = run_process(
      [
          ffmpeg,
          "-hide_banner",
          "-loglevel",
          "error",
          "-y",
          "-f",
          "lavfi",
          "-i",
          f"anullsrc=r={sample_rate}:cl=mono",
          "-t",
          f"{seconds:.3f}",
          "-ar",
          str(sample_rate),
          "-b:a",
          str(bit_rate),
          str(output_mp3),
      ],
      timeout=120,
  )
  if result.returncode != 0 or not output_mp3.is_file():
    raise MediaError(
        f"Could not encode inter-chapter silence pad: "
        f"{(result.stderr or result.stdout).strip()}"
    )
