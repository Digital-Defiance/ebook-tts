"""ffmpeg/ffprobe discovery, validation, probing, decoding, and assembly."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from collections.abc import Sequence
from typing import Any

from ..errors import MediaError
from ..models import MediaInfo
from ..utils import atomic_write_text, fsync_directory, sha256_file


@dataclass(frozen=True)
class MediaTools:
  ffmpeg: str
  ffprobe: str


def run_process(
    command: Sequence[str],
    *,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
  try:
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
  except (OSError, subprocess.TimeoutExpired) as exc:
    raise MediaError(f"Could not run {command[0]}: {exc}") from exc


def resolve_executable(requested: str) -> str:
  if os.sep in requested or (os.altsep and os.altsep in requested):
    path = Path(requested).expanduser().resolve()
    if path.is_file() and os.access(path, os.X_OK):
      return str(path)
    raise MediaError(f"Executable is missing or not runnable: {path}")
  resolved = shutil.which(requested)
  if not resolved:
    raise MediaError(f"Required executable was not found on PATH: {requested}")
  return resolved


def _require_capability(
    executable: str,
    arguments: list[str],
    pattern: str,
    description: str,
) -> None:
  result = run_process([executable, "-hide_banner", *arguments], timeout=30)
  output = "\n".join(part for part in (result.stdout, result.stderr) if part)
  if result.returncode != 0 or not re.search(pattern, output, flags=re.MULTILINE):
    raise MediaError(f"{executable} lacks required {description} support.")


def preflight(ffmpeg_name: str, ffprobe_name: str) -> MediaTools:
  """Resolve tools and verify the exact capabilities used by assembly."""
  ffmpeg = resolve_executable(ffmpeg_name)
  ffprobe = resolve_executable(ffprobe_name)
  for executable in (ffmpeg, ffprobe):
    result = run_process([executable, "-version"], timeout=20)
    if result.returncode != 0:
      detail = (result.stderr or result.stdout).strip()
      raise MediaError(f"Media tool preflight failed for {executable}: {detail}")
  _require_capability(ffmpeg, ["-demuxers"], r"^\s*D\S*\s+concat\s", "concat demuxer")
  _require_capability(ffmpeg, ["-demuxers"], r"^\s*D\S*\s+mp3\s", "MP3 demuxer")
  _require_capability(ffmpeg, ["-muxers"], r"^\s*E\S*\s+mp3\s", "MP3 muxer")
  return MediaTools(ffmpeg=ffmpeg, ffprobe=ffprobe)


def expected_audio_format(output_format: str) -> tuple[str, int, int]:
  """Translate the v1 MP3 output profile to strict ffprobe expectations."""
  pieces = output_format.split("_")
  if len(pieces) != 3 or pieces[0] != "mp3":
    raise MediaError(
        f"Unsupported output format {output_format!r}; ebook-tts v1 requires "
        "mp3_<sample-rate>_<bitrate-kbps>."
    )
  try:
    sample_rate = int(pieces[1])
    bit_rate = int(pieces[2]) * 1000
  except ValueError as exc:
    raise MediaError(f"Invalid MP3 output format: {output_format}") from exc
  if sample_rate <= 0 or bit_rate <= 0:
    raise MediaError(f"Invalid MP3 output format: {output_format}")
  return "mp3", sample_rate, bit_rate


def probe_audio(
    path: Path,
    ffprobe: str,
    *,
    expected_format: str | None = None,
) -> MediaInfo:
  """Probe one audio stream and enforce configured provider output properties."""
  if not path.is_file() or path.stat().st_size == 0:
    raise MediaError(f"Audio file is missing or empty: {path}")
  result = run_process(
      [
          ffprobe,
          "-v",
          "error",
          "-select_streams",
          "a:0",
          "-show_entries",
          "stream=codec_name,sample_rate,bit_rate,channels,duration:format=format_name,duration,bit_rate",
          "-of",
          "json",
          str(path),
      ],
      timeout=120,
  )
  if result.returncode != 0:
    raise MediaError(
        f"ffprobe rejected {path}: {(result.stderr or result.stdout).strip()}"
    )
  try:
    payload = json.loads(result.stdout)
    stream = payload["streams"][0]
    container = payload["format"]
    codec = str(stream["codec_name"])
    format_names = {name.strip() for name in str(container["format_name"]).split(",")}
    sample_rate = int(stream["sample_rate"])
    channels = int(stream["channels"])
    duration = float(stream.get("duration") or container["duration"])
    raw_bit_rate = stream.get("bit_rate") or container.get("bit_rate")
    bit_rate = int(raw_bit_rate) if raw_bit_rate is not None else None
  except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
    raise MediaError(f"ffprobe returned incomplete data for {path}.") from exc
  if not math.isfinite(duration) or duration <= 0 or sample_rate <= 0 or channels <= 0:
    raise MediaError(f"Audio has invalid stream properties: {path}")
  if expected_format:
    expected_codec, expected_rate, expected_bitrate = expected_audio_format(expected_format)
    if codec != expected_codec:
      raise MediaError(f"Expected {expected_codec} in {path}; found {codec}.")
    if "mp3" not in format_names:
      raise MediaError(
          f"Expected an MP3 container in {path}; ffprobe found "
          f"{','.join(sorted(format_names)) or 'unknown'}."
      )
    if sample_rate != expected_rate:
      raise MediaError(
          f"Expected {expected_rate} Hz in {path}; found {sample_rate} Hz."
      )
    if bit_rate is None:
      raise MediaError(f"Expected MP3 bitrate metadata in {path}; ffprobe omitted it.")
    tolerance = max(2_000, int(expected_bitrate * 0.03))
    if abs(bit_rate - expected_bitrate) > tolerance:
      raise MediaError(
          f"Expected about {expected_bitrate // 1000} kbps in {path}; "
          f"found {bit_rate / 1000:g} kbps."
      )
  return MediaInfo(
      codec=codec,
      sample_rate=sample_rate,
      bit_rate=bit_rate,
      channels=channels,
      duration_seconds=round(duration, 6),
      bytes=path.stat().st_size,
      sha256=sha256_file(path),
  )


def decode_audio(path: Path, ffmpeg: str) -> None:
  """Decode the complete audio stream to catch truncation/corruption."""
  result = run_process(
      [
          ffmpeg,
          "-v",
          "error",
          "-xerror",
          "-f",
          "mp3",
          "-i",
          str(path),
          "-map",
          "0:a:0",
          "-f",
          "null",
          "-",
      ],
      timeout=900,
  )
  if result.returncode != 0:
    raise MediaError(
        f"Full audio decode failed for {path}: "
        f"{(result.stderr or result.stdout).strip()}"
    )


def media_record(info: MediaInfo) -> dict[str, Any]:
  return asdict(info)


def ffconcat_line(path: Path) -> str:
  value = str(path.resolve())
  if "\n" in value or "\r" in value:
    raise MediaError(f"Audio path contains a newline: {value!r}")
  escaped = value.replace("\\", "\\\\").replace("'", "'\\''")
  return f"file '{escaped}'"


def assemble_mp3_track(
    *,
    chunk_paths: Sequence[Path],
    chunk_durations: Sequence[float],
    output_path: Path,
    concat_path: Path,
    tools: MediaTools,
    output_format: str,
    title: str,
    album: str,
    artist: str,
    genre: str,
    track_number: int,
    track_count: int,
    cover_path: Path | None,
) -> MediaInfo:
  """Stream-copy ordered MP3 chunks, attach metadata/cover, and verify duration."""
  if not chunk_paths or len(chunk_paths) != len(chunk_durations):
    raise MediaError("Track assembly requires matching non-empty chunk inputs.")
  durations: list[float] = []
  for path, raw_duration in zip(chunk_paths, chunk_durations):
    if path.is_symlink() or not path.is_file():
      raise MediaError(f"Track assembly input is missing or unsafe: {path}")
    if isinstance(raw_duration, bool):
      raise MediaError(f"Track assembly duration is invalid for {path}.")
    try:
      duration = float(raw_duration)
    except (TypeError, ValueError) as exc:
      raise MediaError(f"Track assembly duration is invalid for {path}.") from exc
    if not math.isfinite(duration) or duration <= 0:
      raise MediaError(f"Track assembly duration is invalid for {path}.")
    durations.append(duration)
  expected_codec, _, _ = expected_audio_format(output_format)
  if expected_codec != "mp3":
    raise MediaError("The v1 track assembler supports MP3 provider output only.")
  if output_path.is_symlink() or output_path.exists():
    raise MediaError(f"Refusing to replace existing final audio: {output_path}")
  if concat_path.is_symlink():
    raise MediaError(f"Concat manifest must not be a symbolic link: {concat_path}")
  has_cover = cover_path is not None
  if has_cover and (cover_path.is_symlink() or not cover_path.is_file()):
    raise MediaError(f"Cover artwork is missing or unsafe: {cover_path}")
  output_path.parent.mkdir(parents=True, exist_ok=True)
  concat_path.parent.mkdir(parents=True, exist_ok=True)
  atomic_write_text(
      concat_path,
      "\n".join(ffconcat_line(path) for path in chunk_paths) + "\n",
  )
  temporary = output_path.with_name(
      f".{output_path.stem}.{os.getpid()}.part{output_path.suffix}"
  )
  temporary.unlink(missing_ok=True)
  command = [
      tools.ffmpeg,
      "-hide_banner",
      "-loglevel",
      "error",
      "-y",
      "-f",
      "concat",
      "-safe",
      "0",
      "-i",
      str(concat_path),
  ]
  if has_cover:
    assert cover_path is not None
    command.extend(["-i", str(cover_path), "-map", "0:a:0", "-map", "1:v:0"])
  else:
    command.extend(["-map", "0:a:0"])
  command.extend(["-c:a", "copy"])
  if has_cover:
    command.extend(
        [
            "-c:v",
            "copy",
            "-disposition:v:0",
            "attached_pic",
            "-metadata:s:v:0",
            "title=Cover",
            "-metadata:s:v:0",
            "comment=Cover (front)",
        ]
    )
  command.extend(
      [
          "-map_metadata",
          "-1",
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
          str(temporary),
      ]
  )
  result = run_process(command, timeout=1800)
  if result.returncode != 0:
    temporary.unlink(missing_ok=True)
    raise MediaError(
        f"ffmpeg could not assemble {title}: "
        f"{(result.stderr or result.stdout).strip()}"
    )
  info = probe_audio(temporary, tools.ffprobe, expected_format=output_format)
  expected_duration = sum(durations)
  delta = abs(info.duration_seconds - expected_duration)
  allowed = max(2.0, expected_duration * 0.02)
  if delta > allowed:
    temporary.unlink(missing_ok=True)
    raise MediaError(
        f"Assembled {title} duration differs from chunks by {delta:.2f}s "
        f"(allowed {allowed:.2f}s)."
    )
  decode_audio(temporary, tools.ffmpeg)
  os.replace(temporary, output_path)
  fsync_directory(output_path.parent)
  return probe_audio(output_path, tools.ffprobe, expected_format=output_format)
