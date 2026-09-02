"""ffmpeg-based loudness, peak, and silence analysis."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..errors import MediaError
from ..media.tools import MediaTools, probe_audio, run_process


@dataclass(frozen=True)
class SilenceInterval:
  start: float
  end: float
  duration: float
  position: str


@dataclass(frozen=True)
class SignalMetrics:
  integrated_lufs: float | None
  loudness_range_lu: float | None
  true_peak_dbfs: float | None
  sample_peak_dbfs: float | None
  silence: tuple[SilenceInterval, ...]
  warnings: tuple[str, ...]


def _last_number(pattern: str, value: str) -> float | None:
  matches = re.findall(pattern, value, flags=re.MULTILINE)
  if not matches:
    return None
  raw = matches[-1].lower()
  if raw in {"-inf", "inf", "+inf"}:
    return -math.inf if raw == "-inf" else math.inf
  try:
    return float(raw)
  except ValueError:
    return None


def _analyze_ebur128(path: Path, tools: MediaTools) -> tuple[float | None, float | None, float | None]:
  result = run_process(
      [
          tools.ffmpeg,
          "-hide_banner",
          "-nostats",
          "-i",
          str(path),
          "-filter:a",
          "ebur128=peak=true:framelog=quiet",
          "-f",
          "null",
          "-",
      ],
      timeout=1800,
  )
  if result.returncode != 0:
    raise MediaError(f"Loudness analysis failed for {path}: {result.stderr.strip()}")
  output = result.stderr
  integrated = _last_number(r"^\s*I:\s*(-?(?:\d+(?:\.\d+)?|inf))\s+LUFS", output)
  loudness_range = _last_number(r"^\s*LRA:\s*(-?(?:\d+(?:\.\d+)?|inf))\s+LU", output)
  true_peak = _last_number(r"^\s*Peak:\s*(-?(?:\d+(?:\.\d+)?|inf))\s+dBFS", output)
  return integrated, loudness_range, true_peak


def _analyze_sample_peak(path: Path, tools: MediaTools) -> float | None:
  result = run_process(
      [
          tools.ffmpeg,
          "-hide_banner",
          "-nostats",
          "-i",
          str(path),
          "-filter:a",
          "astats=metadata=0:reset=0",
          "-f",
          "null",
          "-",
      ],
      timeout=1800,
  )
  if result.returncode != 0:
    raise MediaError(f"Peak analysis failed for {path}: {result.stderr.strip()}")
  return _last_number(r"Peak level dB:\s*(-?(?:\d+(?:\.\d+)?|inf))", result.stderr)


def _analyze_silence(
    path: Path,
    tools: MediaTools,
    duration: float,
    minimum_duration: float,
) -> tuple[SilenceInterval, ...]:
  result = run_process(
      [
          tools.ffmpeg,
          "-hide_banner",
          "-nostats",
          "-i",
          str(path),
          "-filter:a",
          f"silencedetect=noise=-45dB:d={minimum_duration:g}",
          "-f",
          "null",
          "-",
      ],
      timeout=1800,
  )
  if result.returncode != 0:
    raise MediaError(f"Silence analysis failed for {path}: {result.stderr.strip()}")
  starts = re.finditer(r"silence_start:\s*([0-9.]+)", result.stderr)
  ends = re.finditer(
      r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)",
      result.stderr,
  )
  events: list[tuple[int, str, float, float | None]] = []
  for match in starts:
    events.append((match.start(), "start", float(match.group(1)), None))
  for match in ends:
    events.append((match.start(), "end", float(match.group(1)), float(match.group(2))))
  active: float | None = None
  intervals: list[SilenceInterval] = []
  for _, kind, timestamp, reported_duration in sorted(events):
    if kind == "start":
      active = timestamp
    elif active is not None:
      interval_duration = reported_duration or max(0.0, timestamp - active)
      if active <= 0.1:
        position = "leading"
      elif timestamp >= duration - 0.1:
        position = "trailing"
      else:
        position = "internal"
      intervals.append(
          SilenceInterval(
              start=round(active, 6),
              end=round(timestamp, 6),
              duration=round(interval_duration, 6),
              position=position,
          )
      )
      active = None
  if active is not None:
    intervals.append(
        SilenceInterval(
            start=round(active, 6),
            end=round(duration, 6),
            duration=round(max(0.0, duration - active), 6),
            position="trailing" if active > 0.1 else "leading",
        )
    )
  return tuple(intervals)


def analyze_signal(
    path: Path,
    tools: MediaTools,
    *,
    max_internal_silence_seconds: float,
    clipping_peak_db: float,
) -> SignalMetrics:
  """Decode and record objective signal metrics with conservative warnings."""
  media = probe_audio(path, tools.ffprobe)
  integrated, loudness_range, true_peak = _analyze_ebur128(path, tools)
  sample_peak = _analyze_sample_peak(path, tools)
  silence = _analyze_silence(
      path,
      tools,
      media.duration_seconds,
      minimum_duration=min(1.0, max(0.1, max_internal_silence_seconds)),
  )
  warnings: list[str] = []
  measured_peak = true_peak if true_peak is not None else sample_peak
  if measured_peak is not None and measured_peak > clipping_peak_db:
    warnings.append(
        f"Peak {measured_peak:.2f} dBFS exceeds configured "
        f"{clipping_peak_db:.2f} dBFS review threshold."
    )
  for interval in silence:
    if (
        interval.position == "internal"
        and interval.duration > max_internal_silence_seconds
    ):
      warnings.append(
          f"Internal silence from {interval.start:.2f}s to {interval.end:.2f}s "
          f"lasts {interval.duration:.2f}s."
      )
  return SignalMetrics(
      integrated_lufs=integrated,
      loudness_range_lu=loudness_range,
      true_peak_dbfs=true_peak,
      sample_peak_dbfs=sample_peak,
      silence=silence,
      warnings=tuple(warnings),
  )


def signal_record(metrics: SignalMetrics) -> dict[str, Any]:
  return asdict(metrics)
