"""Inter-chapter comfort pads for M4B assembly."""

from __future__ import annotations

from pathlib import Path

from pytest import approx

from ebook_tts.media.tools import assemble_m4b, probe_audio, probe_chapters


def test_assemble_m4b_inserts_chapter_gap(
    tmp_path: Path,
    tone_mp3: bytes,
    media_tools,
) -> None:
  track_a = tmp_path / "a.mp3"
  track_b = tmp_path / "b.mp3"
  track_a.write_bytes(tone_mp3)
  track_b.write_bytes(tone_mp3)
  dur_a = probe_audio(track_a, media_tools.ffprobe).duration_seconds
  dur_b = probe_audio(track_b, media_tools.ffprobe).duration_seconds

  with_gap = tmp_path / "with-gap.m4b"
  info = assemble_m4b(
      track_paths=[track_a, track_b],
      track_titles=["One", "Two"],
      track_durations=[dur_a, dur_b],
      output_path=with_gap,
      tools=media_tools,
      title="Gap Book",
      artist="Tester",
      album="Gap Book",
      chapter_gap_ms=2500.0,
  )
  chapters = probe_chapters(with_gap, media_tools.ffprobe)
  assert len(chapters) == 2
  first = float(chapters[0]["end_time"]) - float(chapters[0]["start_time"])
  second = float(chapters[1]["end_time"]) - float(chapters[1]["start_time"])
  assert first == approx(dur_a + 2.5, abs=0.35)
  assert second == approx(dur_b, abs=0.35)
  assert info.duration_seconds == approx(dur_a + dur_b + 2.5, abs=0.5)

  no_gap = tmp_path / "no-gap.m4b"
  assemble_m4b(
      track_paths=[track_a, track_b],
      track_titles=["One", "Two"],
      track_durations=[dur_a, dur_b],
      output_path=no_gap,
      tools=media_tools,
      title="No Gap Book",
      artist="Tester",
      album="No Gap Book",
      chapter_gap_ms=0.0,
  )
  plain = probe_chapters(no_gap, media_tools.ffprobe)
  plain_first = float(plain[0]["end_time"]) - float(plain[0]["start_time"])
  assert plain_first == approx(dur_a, abs=0.35)
