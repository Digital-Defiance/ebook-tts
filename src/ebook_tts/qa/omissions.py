"""Actionable omission reports from spoken-gate failures.

`assess_transcript` detects omissions (`max_expected_gap`); this module names
the missing words, locates them in the prose, and suggests an audio patch
region from energy pauses. It informs a human repair; it does not splice.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Sequence

from .scoring import is_benign_replace, normalized_tokens

# Gate fails at 8; report anything approaching that threshold.
DEFAULT_MIN_GAP = 5
DEFAULT_DURATION_TOLERANCE = 0.5
MappingLike = dict[str, Any]


@dataclass(frozen=True)
class OmissionGap:
  expected_index: int
  expected: tuple[str, ...]
  heard: tuple[str, ...]
  gap: int
  tag: str
  anchor: str | None
  prose_context: str
  suggest_start: float | None = None
  suggest_end: float | None = None
  segment_start: float | None = None
  segment_end: float | None = None

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)


def find_gaps(
    expected: Sequence[str],
    heard: Sequence[str],
    *,
    min_gap: int = DEFAULT_MIN_GAP,
) -> list[dict[str, Any]]:
  """Non-benign delete/replace runs of expected words, in manuscript order."""
  expected_t = tuple(expected)
  heard_t = tuple(heard)
  matcher = SequenceMatcher(a=expected_t, b=heard_t, autojunk=False)
  opcodes = matcher.get_opcodes()
  gaps: list[dict[str, Any]] = []
  for idx, (tag, i1, i2, j1, j2) in enumerate(opcodes):
    if tag not in {"delete", "replace"}:
      continue
    gap = i2 - i1
    if gap < min_gap:
      continue
    exp_part = list(expected_t[i1:i2])
    heard_part = list(heard_t[j1:j2])
    if tag == "replace" and is_benign_replace(exp_part, heard_part):
      continue
    if heard_part:
      anchor: str | None = heard_part[-1]
    elif idx > 0 and opcodes[idx - 1][0] == "equal":
      prev = opcodes[idx - 1]
      anchor = heard_t[prev[4] - 1] if prev[4] > prev[3] else None
    else:
      anchor = None
    gaps.append(
        {
            "expected_index": i1,
            "expected": exp_part,
            "heard": heard_part,
            "gap": gap,
            "tag": tag,
            "anchor": anchor,
        }
    )
  return gaps


def prose_context(prose: str, missing: Sequence[str], *, width: int = 90) -> str:
  """Locate the missing run in raw prose and show surrounding text."""
  if not missing:
    return ""
  needle = " ".join(missing)
  hay = prose.lower()
  for length in range(len(missing), 0, -1):
    probe = " ".join(missing[:length]).lower()
    idx = hay.find(probe)
    if idx >= 0:
      start = max(0, idx - width)
      end = min(len(prose), idx + len(needle) + width)
      prefix = "…" if start else ""
      suffix = "…" if end < len(prose) else ""
      return prefix + prose[start:end].strip() + suffix
  return "(run not located verbatim in prose; check spelling/hyphenation)"


def energy_pauses(
    audio: Any,
    rate: int,
    lo_sec: float,
    hi_sec: float,
    *,
    floor_db: float = -42.0,
    min_ms: float = 60.0,
) -> list[tuple[float, float]]:
  """Quiet spans in a window, by short-frame RMS."""
  import numpy as np

  lo = max(0, int(lo_sec * rate))
  hi = min(int(audio.size), int(hi_sec * rate))
  segment = audio[lo:hi]
  frame = max(1, int(rate * 0.010))
  n = int(segment.size // frame)
  if n == 0:
    return []
  levels = np.sqrt(
      (segment[: n * frame].reshape(-1, frame).astype(np.float64) ** 2).mean(axis=1)
      + 1e-12
  )
  db = 20 * np.log10(levels + 1e-9)
  pauses: list[tuple[float, float]] = []
  start: float | None = None
  for i in range(n):
    t = lo_sec + i * 0.010
    quiet = bool(db[i] < floor_db)
    if quiet and start is None:
      start = t
    elif not quiet and start is not None:
      if (t - start) * 1000 >= min_ms:
        pauses.append((round(start, 3), round(t, 3)))
      start = None
  if start is not None and (hi_sec - start) * 1000 >= min_ms:
    pauses.append((round(start, 3), round(hi_sec, 3)))
  return pauses


def containing_speech_segment(
    assembly_map: Sequence[MappingLike],
    rate: int,
    start_sec: float,
    end_sec: float,
) -> dict[str, Any] | None:
  """Return the speech_segment wholly containing ``[start_sec, end_sec]``."""
  for entry in assembly_map:
    if not isinstance(entry, dict) or entry.get("kind") != "speech_segment":
      continue
    try:
      s = float(entry["start_sample"]) / rate
      e = float(entry["end_sample"]) / rate
    except (KeyError, TypeError, ValueError):
      continue
    if s <= start_sec + 0.05 and e >= end_sec - 0.05:
      return entry
  return None


def locate_seam_segment(
    *,
    asr_segments: Sequence[MappingLike],
    assembly_map: Sequence[MappingLike],
    rate: int,
    anchor_word: str | None,
) -> dict[str, Any] | None:
  """Best-guess speech_segment holding a drop, via the ASR segment with anchor."""
  target_time: float | None = None
  if anchor_word:
    needle = anchor_word.lower()
    for segment in asr_segments:
      if not isinstance(segment, dict):
        continue
      if needle in str(segment.get("text", "")).lower():
        try:
          target_time = float(segment.get("end", 0.0))
        except (TypeError, ValueError):
          continue
  if target_time is None:
    return None
  best: dict[str, Any] | None = None
  for entry in assembly_map:
    if not isinstance(entry, dict) or entry.get("kind") != "speech_segment":
      continue
    try:
      s = float(entry["start_sample"]) / rate
      e = float(entry["end_sample"]) / rate
    except (KeyError, TypeError, ValueError):
      continue
    if s <= target_time <= e + 1.0:
      best = entry
  return best


def diagnose_omissions(
    expected_text: str,
    heard_text: str,
    *,
    min_gap: int = DEFAULT_MIN_GAP,
    audio: Any | None = None,
    sample_rate: int = 44100,
    assembly_map: Sequence[MappingLike] | None = None,
    asr_segments: Sequence[MappingLike] | None = None,
) -> list[OmissionGap]:
  """Diff expected vs heard and optionally suggest patch regions from audio."""
  expected = normalized_tokens(expected_text)
  heard = normalized_tokens(heard_text)
  raw_gaps = find_gaps(expected, heard, min_gap=min_gap)
  results: list[OmissionGap] = []
  duration = (
      float(audio.size) / float(sample_rate)
      if audio is not None and sample_rate > 0
      else 0.0
  )
  for gap in raw_gaps:
    suggest_start: float | None = None
    suggest_end: float | None = None
    segment_start: float | None = None
    segment_end: float | None = None
    if audio is not None and sample_rate > 0:
      seg = None
      if assembly_map and asr_segments is not None:
        seg = locate_seam_segment(
            asr_segments=asr_segments,
            assembly_map=assembly_map,
            rate=sample_rate,
            anchor_word=gap.get("anchor"),
        )
      if seg is not None:
        segment_start = float(seg["start_sample"]) / sample_rate
        segment_end = float(seg["end_sample"]) / sample_rate
        pauses = energy_pauses(
            audio, sample_rate, max(0.0, segment_end - 3.0), segment_end
        )
        suggest_start = pauses[-1][0] if pauses else round(segment_end - 0.5, 3)
        suggest_end = round(segment_end, 3)
      elif duration > 0:
        # Proportional estimate when no assembly map is available (ebook-tts MP3).
        frac = gap["expected_index"] / max(1, len(expected))
        approx = duration * frac
        window_lo = max(0.0, approx - 2.5)
        window_hi = min(duration, approx + 2.5)
        pauses = energy_pauses(audio, sample_rate, window_lo, window_hi)
        if pauses:
          # Prefer the pause nearest the proportional estimate.
          target = approx
          nearest = min(
              pauses,
              key=lambda pause, t=target: abs(((pause[0] + pause[1]) / 2) - t),
          )
          suggest_start = nearest[0]
          suggest_end = round(min(duration, nearest[1] + 0.35), 3)
        else:
          suggest_start = round(max(0.0, approx - 0.5), 3)
          suggest_end = round(min(duration, approx + 0.5), 3)
    results.append(
        OmissionGap(
            expected_index=int(gap["expected_index"]),
            expected=tuple(gap["expected"]),
            heard=tuple(gap["heard"]),
            gap=int(gap["gap"]),
            tag=str(gap["tag"]),
            anchor=gap.get("anchor"),
            prose_context=prose_context(expected_text, gap["expected"]),
            suggest_start=suggest_start,
            suggest_end=suggest_end,
            segment_start=segment_start,
            segment_end=segment_end,
        )
    )
  return results


def format_find_report(
    gaps: Sequence[OmissionGap],
    *,
    label: str,
    gate: dict[str, Any] | None = None,
    min_gap: int = DEFAULT_MIN_GAP,
) -> str:
  """Human-readable omission report (exit status decided by caller)."""
  lines = [f"source: {label}"]
  if gate:
    lines.append(
        "  gate: "
        f"passed={gate.get('passed')} wer={gate.get('wer')} "
        f"coverage={gate.get('coverage')} "
        f"max_expected_gap={gate.get('max_expected_gap')}"
    )
  if not gaps:
    lines.append(
        f"  no non-benign expected-gaps >= {min_gap} tokens; "
        "any failure is benign ASR drift, not audible omission"
    )
    return "\n".join(lines)
  lines.append(f"  {len(gaps)} real omission(s):\n")
  for n, gap in enumerate(gaps, 1):
    lines.append(f"  [{n}] {gap.tag} of {gap.gap} expected token(s):")
    lines.append(f"      missing : {' '.join(gap.expected)}")
    if gap.heard:
      lines.append(f"      heard   : {' '.join(gap.heard)}")
    lines.append(f"      prose   : {gap.prose_context}")
    if gap.segment_start is not None and gap.segment_end is not None:
      lines.append(
          f"      segment : speech_segment {gap.segment_start:.3f}"
          f"-{gap.segment_end:.3f}s"
      )
    if gap.suggest_start is not None and gap.suggest_end is not None:
      lines.append(
          f"      suggest : --start {gap.suggest_start:.3f} "
          f"--end {gap.suggest_end:.3f} "
          "--replace-unintelligible --allow-duration-change"
      )
      lines.append(
          "      phrase  : re-render the missing run (prefix the last retained "
          "word so the join is not fused), verify in isolation"
      )
    lines.append("")
  return "\n".join(lines).rstrip() + "\n"


def load_audio_mono(path: Path, *, ffmpeg: str | None = None) -> tuple[Any, int]:
  """Load WAV/FLAC via soundfile, or decode MP3/M4A through ffmpeg to a temp WAV."""
  try:
    import numpy as np
    import soundfile as sf
  except ImportError as exc:
    raise RuntimeError(
        "numpy and soundfile are required for audio localization "
        "(pip install 'ebook-tts[local]')."
    ) from exc

  suffix = path.suffix.lower()
  if suffix in {".wav", ".flac", ".ogg"}:
    data, rate = sf.read(str(path), always_2d=False)
    audio = np.asarray(data, dtype=np.float32)
    if audio.ndim > 1:
      audio = audio.mean(axis=1)
    return audio, int(rate)

  if not ffmpeg:
    raise RuntimeError(
        f"Cannot load {suffix} without ffmpeg; pass a decoded WAV or install ffmpeg."
    )
  from ..media.tools import run_process

  with tempfile.TemporaryDirectory(prefix="ebook-tts-omit-") as tmp:
    wav = Path(tmp) / "slice.wav"
    result = run_process(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(path),
            "-ac",
            "1",
            str(wav),
        ],
        timeout=1800,
    )
    if result.returncode != 0:
      raise RuntimeError(
          f"ffmpeg could not decode {path}: "
          f"{(result.stderr or result.stdout).strip()}"
      )
    data, rate = sf.read(str(wav), always_2d=False)
    audio = np.asarray(data, dtype=np.float32)
    if audio.ndim > 1:
      audio = audio.mean(axis=1)
    return audio, int(rate)


def robust_transcribe_window(
    audio: Any,
    rate: int,
    start: float,
    end: float,
    *,
    model: str = "mlx-community/whisper-large-v3-turbo",
) -> str:
  """Windowed ASR via temp file with condition_on_previous_text=False."""
  try:
    import mlx_whisper
    import soundfile as sf
  except ImportError as exc:
    raise RuntimeError(
        "Install the local extra on Apple Silicon: pip install 'ebook-tts[local]'"
    ) from exc

  import numpy as np

  seg = np.asarray(audio[int(start * rate) : int(end * rate)], dtype=np.float32)
  with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
    temp = Path(handle.name)
  try:
    sf.write(str(temp), seg, rate)
    result = mlx_whisper.transcribe(
        str(temp),
        path_or_hf_repo=model,
        language="en",
        condition_on_previous_text=False,
        verbose=False,
    )
  finally:
    temp.unlink(missing_ok=True)
  return str(result.get("text") or "").strip()


@dataclass(frozen=True)
class FormatCheck:
  label: str
  duration: float
  diff: float | None
  ok: bool
  detail: str

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)


def check_format_durations(
    *,
    reference_path: Path,
    reference_duration: float,
    mp3_path: Path | None = None,
    m4b_path: Path | None = None,
    chapter_title: str | None = None,
    ffprobe: str = "ffprobe",
    tolerance: float = DEFAULT_DURATION_TOLERANCE,
) -> list[FormatCheck]:
  """Confirm delivered audio and packaged forms agree on duration."""
  from ..media.tools import probe_audio, probe_chapters

  checks = [
      FormatCheck(
          label="reference",
          duration=reference_duration,
          diff=None,
          ok=True,
          detail=f"{reference_path.name}  {reference_duration:.2f}s",
      )
  ]
  if mp3_path is not None:
    info = probe_audio(mp3_path, ffprobe)
    diff = abs(info.duration_seconds - reference_duration)
    ok = diff < tolerance
    checks.append(
        FormatCheck(
            label="mp3",
            duration=info.duration_seconds,
            diff=diff,
            ok=ok,
            detail=(
                f"{mp3_path.name}  {info.duration_seconds:.2f}s  "
                f"diff {diff:.2f}s  {'OK' if ok else 'MISMATCH'}"
            ),
        )
    )
  if m4b_path is not None:
    if not chapter_title:
      checks.append(
          FormatCheck(
              label="m4b",
              duration=0.0,
              diff=None,
              ok=False,
              detail="--chapter-title is required when checking an M4B",
          )
      )
    else:
      chapters = probe_chapters(m4b_path, ffprobe)
      match = next(
          (
              c
              for c in chapters
              if str(c.get("tags", {}).get("title", "")).startswith(chapter_title)
          ),
          None,
      )
      if match is None:
        checks.append(
            FormatCheck(
                label="m4b",
                duration=0.0,
                diff=None,
                ok=False,
                detail=f"no chapter titled {chapter_title!r}  MISMATCH",
            )
        )
      else:
        cdur = float(match["end_time"]) - float(match["start_time"])
        diff = abs(cdur - reference_duration)
        ok = diff < tolerance
        title = match.get("tags", {}).get("title", chapter_title)
        checks.append(
            FormatCheck(
                label="m4b",
                duration=cdur,
                diff=diff,
                ok=ok,
                detail=(
                    f"{title!r}  {cdur:.2f}s  diff {diff:.2f}s  "
                    f"{'OK' if ok else 'MISMATCH'}"
                ),
            )
        )
  return checks


def load_transcript_cache(path: Path) -> str:
  """Read heard text from a cached QA transcript JSON."""
  record = json.loads(path.read_text(encoding="utf-8"))
  text = record.get("text")
  if isinstance(text, str) and text.strip():
    return text
  raise ValueError(f"Transcript cache has no text: {path}")
