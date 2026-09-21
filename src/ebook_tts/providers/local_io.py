"""Reference-audio loading for local Fish rendering."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

from ..errors import ProviderError


def load_reference(path: Path, target_rate: int):
  """Mono float32 reference at the engine sample rate, as an mlx array."""
  try:
    import mlx.core as mx
    import numpy as np
    import soundfile as sf
  except ImportError as exc:
    raise ProviderError(
        "Install the local extra on Apple Silicon: pip install 'ebook-tts[local]'"
    ) from exc
  audio, rate = sf.read(str(path), always_2d=False)
  audio = np.asarray(audio, dtype=np.float32)
  if audio.ndim > 1:
    audio = audio.mean(axis=1)
  if rate != target_rate:
    from scipy.signal import resample_poly

    ratio = Fraction(int(target_rate), int(rate)).limit_denominator(1000)
    audio = resample_poly(audio, ratio.numerator, ratio.denominator).astype(np.float32)
  peak = float(np.abs(audio).max()) if audio.size else 0.0
  if peak > 1.0:
    audio = audio / peak
  return mx.array(audio)
