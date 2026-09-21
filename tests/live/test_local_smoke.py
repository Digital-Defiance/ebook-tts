"""Opt-in live Fish + Whisper smoke.

This module is skipped unless all of the following hold:

1. ``EBOOK_TTS_LIVE_LOCAL=1``
2. Apple Silicon with ``ebook-tts[local]`` installed (mlx, mlx-audio, mlx-whisper)
3. ``EBOOK_TTS_LIVE_REFERENCE_WAV`` and ``EBOOK_TTS_LIVE_REFERENCE_TEXT`` point at
   a voice clone pair you have the right to use

Default ``pytest`` stays offline. Run explicitly:

```bash
uv sync --extra local --extra dev
export EBOOK_TTS_LIVE_LOCAL=1
export EBOOK_TTS_LIVE_REFERENCE_WAV=/path/to/narrator.wav
export EBOOK_TTS_LIVE_REFERENCE_TEXT=/path/to/narrator.txt
uv run pytest -m live -q
```

The smoke renders one short chapter with voice anchoring, discards the anchor
segment, transcribes with Whisper, and fails if the anchor leaks into the
delivered audio or the spoken gate rejects the chapter.
"""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from ebook_tts.config import default_config
from ebook_tts.models import DEFAULT_VOICE_ANCHOR
from ebook_tts.providers.base import SynthesisRequest
from ebook_tts.providers.local import LocalSTTProvider, LocalTTSProvider
from ebook_tts.qa.scoring import assess_transcript
from ebook_tts.text.spoken import verbalize_speech_text
from ebook_tts.utils import sha256_file


LIVE_ENV = "EBOOK_TTS_LIVE_LOCAL"
REF_WAV_ENV = "EBOOK_TTS_LIVE_REFERENCE_WAV"
REF_TXT_ENV = "EBOOK_TTS_LIVE_REFERENCE_TEXT"
REF_WAV_SHA_ENV = "EBOOK_TTS_LIVE_REFERENCE_WAV_SHA256"
REF_TXT_SHA_ENV = "EBOOK_TTS_LIVE_REFERENCE_TEXT_SHA256"

# Phrases that must appear in the discarded anchor and must not survive into
# the delivered chapter audio after Whisper.
ANCHOR_MARKERS = (
    "before the chapter begins",
    "settle into the same chair",
    "notebook squarely on the desk",
)

CHAPTER_PROSE = (
    "Ravi Anand opened the log at 07:02 and read the first line aloud. "
    "The apparatus receives tokens from the page and nothing else."
)


def _require_live_environment() -> tuple[Path, Path]:
  if os.environ.get(LIVE_ENV) != "1":
    pytest.skip(f"Set {LIVE_ENV}=1 to run the real Fish/Whisper smoke")
  if platform.machine() != "arm64" or platform.system() != "Darwin":
    pytest.skip("Live local smoke requires Apple Silicon macOS")
  if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
    pytest.skip("ffmpeg and ffprobe are required for live local smoke")
  try:
    import mlx  # noqa: F401
    import mlx_audio  # noqa: F401
    import mlx_whisper  # noqa: F401
    import numpy  # noqa: F401
    import soundfile  # noqa: F401
  except ImportError as exc:
    pytest.skip(f"Install ebook-tts[local] for live smoke: {exc}")

  wav = Path(os.environ.get(REF_WAV_ENV, "")).expanduser()
  txt = Path(os.environ.get(REF_TXT_ENV, "")).expanduser()
  if not wav.is_file() or not txt.is_file():
    pytest.skip(
        f"Set {REF_WAV_ENV} and {REF_TXT_ENV} to a voice reference pair "
        "you have the right to clone"
    )
  expected_wav = os.environ.get(REF_WAV_SHA_ENV, "").strip().lower()
  expected_txt = os.environ.get(REF_TXT_SHA_ENV, "").strip().lower()
  if expected_wav and sha256_file(wav) != expected_wav:
    pytest.fail(f"Reference wav sha256 mismatch for {wav}")
  if expected_txt and sha256_file(txt) != expected_txt:
    pytest.fail(f"Reference text sha256 mismatch for {txt}")
  return wav, txt


def _live_config(wav: Path, txt: Path):
  base = default_config()
  return replace(
      base,
      tts=replace(
          base.tts,
          provider="local",
          voice_id="live-narrator",
          model_id="mlx-community/fish-audio-s2-pro",
          output_format="mp3_44100_128",
          max_characters=200_000,
          local=replace(
              base.tts.local,
              reference_wav=str(wav),
              reference_text=str(txt),
              anchor=True,
              verbalize_numerals=True,
              numeral_style="plain",
              seed=70,
              sentence_pause="short",
              sentence_turns=False,
          ),
      ),
      qa=replace(
          base.qa,
          stt_provider="local",
          stt_model="mlx-community/whisper-large-v3-turbo",
          language="en",
          spoken_gate=True,
          protected_terms=("Ravi Anand",),
          clipping_peak_db=1.0,
      ),
  )


@pytest.mark.live
def test_live_fish_whisper_voice_anchor_and_spoken_gate(tmp_path: Path) -> None:
  """Real on-device render: Fish speaks, Whisper hears, anchor must not leak."""
  wav, txt = _require_live_environment()
  config = _live_config(wav, txt)

  discarded: list = []
  provider = LocalTTSProvider(
      config,
      anchor_sink=lambda audio: discarded.append(audio),
  )
  stt = LocalSTTProvider(model_id=config.qa.stt_model)

  request = SynthesisRequest(
      text=CHAPTER_PROSE,
      voice_id=config.tts.voice_id,
      model_id=config.tts.model_id,
      output_format=config.tts.output_format,
  )
  response = provider.synthesize(request)
  audio_blocks = list(response.blocks)
  assert audio_blocks and audio_blocks[0]
  mp3_path = tmp_path / "live-chapter.mp3"
  mp3_path.write_bytes(audio_blocks[0])
  assert mp3_path.stat().st_size > 8_000

  assert discarded, "Voice anchor was enabled but no segment was discarded"
  import numpy as np

  anchor = np.asarray(discarded[0], dtype=np.float32)
  assert anchor.size > 8_000, "Discarded anchor audio looks too short to be speech"
  assert float(np.abs(anchor).max()) > 0.01, "Discarded anchor audio is near silence"

  transcript = stt.transcribe(mp3_path, language=config.qa.language)
  heard = transcript.text.strip()
  assert heard, "Whisper returned an empty transcript for the live render"

  heard_fold = heard.casefold()
  for marker in ANCHOR_MARKERS:
    assert marker not in heard_fold, (
        f"Voice anchor leaked into delivered audio; Whisper heard {marker!r} "
        f"in: {heard!r}"
    )

  expected = verbalize_speech_text(CHAPTER_PROSE, style=config.tts.local.numeral_style)
  spoken = assess_transcript(expected, heard)
  assert spoken["passed"] is True, (
      f"Spoken gate failed on live audio: wer={spoken['wer']} "
      f"coverage={spoken['coverage']} spans={spoken['suspicious_spans']} "
      f"heard={heard!r}"
  )
  assert "ravi" in heard_fold and "anand" in heard_fold
  # Distinctive chapter content that is not in the anchor.
  assert "apparatus" in heard_fold or "tokens" in heard_fold

  # Sanity: the anchor text itself would fail the chapter spoken gate.
  leaked = assess_transcript(expected, DEFAULT_VOICE_ANCHOR)
  assert leaked["passed"] is False


MULTI_CALL_PROSE = (
    "Ravi Anand opened the log at 07:02 and read the first line aloud.\n\n"
    "The apparatus receives tokens from the page and nothing else is required."
)


@pytest.mark.live
def test_live_fish_max_words_per_call_reanchors_each_part(tmp_path: Path) -> None:
  """Long-context recovery: two generate() calls, each discards its own anchor."""
  wav, txt = _require_live_environment()
  config = _live_config(wav, txt)
  config = replace(
      config,
      tts=replace(
          config.tts,
          local=replace(
              config.tts.local,
              max_words_per_call=14,
              sentence_pause="none",
          ),
      ),
  )

  discarded: list = []
  provider = LocalTTSProvider(
      config,
      anchor_sink=lambda audio: discarded.append(audio),
  )
  stt = LocalSTTProvider(model_id=config.qa.stt_model)
  response = provider.synthesize(
      SynthesisRequest(
          text=MULTI_CALL_PROSE,
          voice_id=config.tts.voice_id,
          model_id=config.tts.model_id,
          output_format=config.tts.output_format,
      )
  )
  mp3_path = tmp_path / "live-multipart.mp3"
  mp3_path.write_bytes(list(response.blocks)[0])

  assert len(discarded) == 2, (
      f"Expected one discarded anchor per generate() call; got {len(discarded)}"
  )
  import numpy as np

  lengths = [int(np.asarray(item).size) for item in discarded]
  # Same seed + same anchor text should produce near-identical lead-ins.
  assert abs(lengths[0] - lengths[1]) / max(lengths) < 0.15

  heard = stt.transcribe(mp3_path, language=config.qa.language).text.strip().casefold()
  for marker in ANCHOR_MARKERS:
    assert marker not in heard
  assert "ravi" in heard and "apparatus" in heard
  expected = verbalize_speech_text(
      MULTI_CALL_PROSE.replace("\n\n", " "),
      style=config.tts.local.numeral_style,
  )
  spoken = assess_transcript(expected, heard)
  assert spoken["passed"] is True, (
      f"Multi-call spoken gate failed: wer={spoken['wer']} heard={heard!r}"
  )
