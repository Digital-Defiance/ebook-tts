"""Post-process PCM segments from a local session engine.

Slope flattening and spectral comfort gaps are the production assembly from
the Fish pipeline. They live here so the ElevenLabs request path stays free of
numpy, and so a short sample can fall back to silence if there is not enough
natural pause audio to harvest.
"""

from __future__ import annotations

from typing import Any


GAP_ALGORITHM = "spectral-v6"
GAP_FFT_SIZE = 2048
GAP_CUTOFF_HZ = 3500.0
QUIET_DB = -26.0
HF_SUSPECT = 0.12
HF_SPLIT_HZ = 4000.0
FRAME_MS = 20.0


def _numpy() -> Any:
  try:
    import numpy as np
  except ImportError as exc:
    raise RuntimeError(
        "Local assembly requires numpy. Install ebook-tts[local]."
    ) from exc
  return np


def active_rms(audio: Any, rate: int) -> float:
  np = _numpy()
  frame = max(1, int(rate * 0.020))
  usable = (audio.size // frame) * frame
  if usable == 0:
    return 0.0
  frames = audio[:usable].reshape(-1, frame).astype(np.float64)
  rms = np.sqrt((frames**2).mean(axis=1) + 1e-12)
  peak = float(rms.max())
  if peak <= 0:
    return 0.0
  voiced = rms[rms >= peak * 10 ** (-36 / 20)]
  return float(np.sqrt(np.mean(voiced**2))) if voiced.size else 0.0


def flatten_slope(audio: Any, rate: int, max_db: float = 6.0) -> tuple[Any, float]:
  np = _numpy()
  window = max(1, int(rate * 0.200))
  count = audio.size // window
  if count < 4:
    return audio, 0.0
  frames = audio[: count * window].reshape(count, window).astype(np.float64)
  rms = np.sqrt((frames**2).mean(axis=1) + 1e-12)
  peak = float(rms.max())
  if peak <= 0:
    return audio, 0.0
  voiced = rms >= peak * 10 ** (-36 / 20)
  if int(voiced.sum()) < 4:
    return audio, 0.0
  centres = (np.arange(count) + 0.5) * window / rate
  slope = float(np.polyfit(centres[voiced], 20 * np.log10(rms[voiced]), 1)[0])
  span = float(centres[voiced][-1] - centres[voiced][0])
  if span <= 0 or abs(slope * span) < 0.5:
    return audio, 0.0
  total = float(np.clip(slope * span, -max_db, max_db))
  midpoint = float(np.mean(centres[voiced]))
  times = np.arange(audio.size, dtype=np.float64) / rate
  gain_db = np.clip(-(total / span) * (times - midpoint), -max_db, max_db)
  out = (audio.astype(np.float64) * 10 ** (gain_db / 20)).astype(np.float32)
  loudest = float(np.abs(out).max())
  if loudest > 0.99:
    out *= 0.99 / loudest
  return out, round(-total, 3)


def match_levels(segments: list[Any], rate: int, limit_db: float = 2.0):
  np = _numpy()
  levels = [active_rms(segment, rate) for segment in segments]
  usable = [level for level in levels if level > 0]
  if not usable:
    return segments, [0.0] * len(segments), 0.0
  target = float(np.median(usable))
  out, gains = [], []
  for segment, level in zip(segments, levels):
    if level <= 0:
      out.append(segment)
      gains.append(0.0)
      continue
    gain_db = float(np.clip(20 * np.log10(target / level), -limit_db, limit_db))
    adjusted = segment.astype(np.float32) * (10 ** (gain_db / 20))
    peak = float(np.abs(adjusted).max())
    if peak > 0.99:
      adjusted *= 0.99 / peak
    out.append(adjusted)
    gains.append(round(gain_db, 3))
  return out, gains, target


def _runs(mask: Any) -> list[tuple[int, int]]:
  np = _numpy()
  runs: list[tuple[int, int]] = []
  start: int | None = None
  for index, flag in enumerate(mask):
    if flag and start is None:
      start = index
    elif not flag and start is not None:
      runs.append((start, index))
      start = None
  if start is not None:
    runs.append((start, int(len(mask))))
  return runs


def frame_stats(audio: Any, rate: int):
  np = _numpy()
  size = int(rate * FRAME_MS / 1000)
  count = audio.size // size
  if count == 0:
    return np.zeros(0), np.zeros(0), size
  block = audio[: count * size].reshape(count, size).astype(np.float64)
  level = np.sqrt((block**2).mean(axis=1) + 1e-12)
  window = np.hanning(size)
  freqs = np.fft.rfftfreq(size, 1 / rate)
  high = freqs > HF_SPLIT_HZ
  power = np.abs(np.fft.rfft(block * window[None, :], axis=1)) ** 2
  total = power.sum(axis=1) + 1e-20
  return level, (power[:, high].sum(axis=1) / total), size


def harvest_floor(audio: Any, level: Any, hf: Any, size: int, speech: float):
  np = _numpy()
  quiet = level < speech * 10 ** (QUIET_DB / 20)
  dark = hf < HF_SUSPECT * 0.5
  runs = [(start, end) for start, end in _runs(quiet & dark) if (end - start) * size >= size * 4]
  runs.sort(key=lambda item: item[1] - item[0], reverse=True)
  if not runs:
    return np.zeros(0, dtype=np.float32)
  donor = [audio[start * size : end * size] for start, end in runs[:12]]
  return np.concatenate(donor).astype(np.float32)


def spectral_fill(length: int, donor: Any, rate: int, target_level: float, rng: Any) -> Any:
  np = _numpy()
  nfft = GAP_FFT_SIZE
  if donor.size < nfft or length <= 0:
    return np.zeros(length, dtype=np.float32)
  hop = nfft // 2
  window = np.hanning(nfft)
  spectra = []
  for start in range(0, donor.size - nfft + 1, hop):
    frame = donor[start : start + nfft].astype(np.float64)
    level = float(np.sqrt(np.mean(frame**2) + 1e-20))
    if level < 1e-6:
      continue
    spectra.append(np.abs(np.fft.rfft(frame * window)) ** 2)
  if not spectra:
    return np.zeros(length, dtype=np.float32)
  psd = np.median(np.stack(spectra), axis=0)
  freqs = np.fft.rfftfreq(nfft, 1 / rate)
  lo = max(200.0, GAP_CUTOFF_HZ - 500.0)
  hi = GAP_CUTOFF_HZ + 500.0
  mask = np.ones_like(freqs)
  transition = (freqs > lo) & (freqs < hi)
  mask[freqs >= hi] = 0.0
  mask[transition] = 0.5 * (1.0 + np.cos(np.pi * (freqs[transition] - lo) / (hi - lo)))
  psd *= mask**2
  blocks = int(np.ceil((length + nfft) / hop))
  out = np.zeros(blocks * hop + nfft, dtype=np.float64)
  weight = np.zeros_like(out)
  scale = np.sqrt(np.maximum(psd, 0.0))
  for index in range(blocks):
    coeff = rng.standard_normal(scale.size) + 1j * rng.standard_normal(scale.size)
    coeff *= scale / np.sqrt(2.0)
    coeff[0] = rng.standard_normal() * scale[0]
    coeff[-1] = rng.standard_normal() * scale[-1]
    frame = np.fft.irfft(coeff, n=nfft) * window
    start = index * hop
    out[start : start + nfft] += frame
    weight[start : start + nfft] += window**2
  valid = weight > 1e-8
  out[valid] /= np.sqrt(weight[valid])
  out = out[nfft : nfft + length]
  current = float(np.sqrt(np.mean(out**2) + 1e-20))
  if current > 0 and target_level > 0:
    out *= target_level / current
  return out.astype(np.float32)


def comfort_gap(segments: list[Any], samples: int, rate: int, seed: int) -> Any:
  np = _numpy()
  if samples <= 0:
    return np.zeros(0, dtype=np.float32)
  if not segments:
    return np.zeros(samples, dtype=np.float32)
  source = np.concatenate(segments)
  level, hf, size = frame_stats(source, rate)
  speech = float(np.percentile(level, 90)) if level.size else 0.0
  donor = harvest_floor(source, level, hf, size, speech)
  if donor.size < GAP_FFT_SIZE:
    return np.zeros(samples, dtype=np.float32)
  quiet = level < speech * 10 ** (QUIET_DB / 20)
  natural_runs = [
      float(np.median(level[start:end]))
      for start, end in _runs(quiet & (hf < HF_SUSPECT * 0.5))
      if end - start >= 4
  ]
  if not natural_runs:
    return np.zeros(samples, dtype=np.float32)
  gap = spectral_fill(
      samples,
      donor,
      rate,
      float(np.median(natural_runs)),
      np.random.default_rng(seed),
  )
  fade = min(int(rate * 0.040), samples // 4)
  if fade > 0:
    gap[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
    gap[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
  return gap.astype(np.float32)


def assemble_session(segments: list[Any], rate: int, gap_ms: float) -> Any:
  np = _numpy()
  if not segments:
    raise RuntimeError("Local engine produced no audio segments.")
  corrected = []
  for segment in segments:
    flattened, _ = flatten_slope(segment, rate)
    corrected.append(flattened)
  levelled, _, _ = match_levels(corrected, rate)
  gap_samples = int(rate * gap_ms / 1000)
  pieces = []
  for index, segment in enumerate(levelled):
    pieces.append(segment)
    if index < len(levelled) - 1 and gap_samples > 0:
      pieces.append(comfort_gap(levelled, gap_samples, rate, seed=1000 + index))
  joined = np.concatenate(pieces)
  peak = float(np.abs(joined).max())
  if peak > 0.99:
    joined *= 0.99 / peak
  return joined.astype(np.float32)
