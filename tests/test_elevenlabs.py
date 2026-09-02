from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

import pytest

from ebook_tts.providers.base import SynthesisRequest, TranscriptWord
from ebook_tts.providers.elevenlabs import (
    ElevenLabsSTTProvider,
    ElevenLabsTTSProvider,
)


class _FakeResponseContext:
  def __init__(self, data: Iterable[bytes], request_id: str) -> None:
    self.response = SimpleNamespace(
        data=data,
        _response=SimpleNamespace(
            headers={"request-id": request_id, "character-cost": "17"}
        ),
    )
    self.enter_count = 0
    self.exited_with: tuple[Any, Any, Any] | None = None

  def __enter__(self) -> Any:
    self.enter_count += 1
    return self.response

  def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
    self.exited_with = (exc_type, exc, traceback)


class _FakeTTSClient:
  def __init__(self, *, fail_during_stream: bool = False) -> None:
    self.fail_during_stream = fail_during_stream
    self.calls: list[dict[str, Any]] = []
    self.contexts: list[_FakeResponseContext] = []
    self.text_to_speech = SimpleNamespace(
        with_raw_response=SimpleNamespace(convert=self._convert)
    )

  def _convert(self, **kwargs: Any) -> _FakeResponseContext:
    self.calls.append(kwargs)
    if self.fail_during_stream:
      data = self._failing_blocks()
    else:
      data = iter((b"audio-", b"bytes"))
    context = _FakeResponseContext(data, f"request-{len(self.calls)}")
    self.contexts.append(context)
    return context

  @staticmethod
  def _failing_blocks() -> Iterable[bytes]:
    yield b"partial"
    raise RuntimeError("stream failed")


class _FakeSTTResult:
  text = "Hello world"
  language_code = "en"
  words = (
      SimpleNamespace(
          type="word",
          text="Hello",
          start=0.0,
          end=0.4,
          confidence=0.98,
      ),
      {"type": "spacing", "text": " "},
      {"text": "world", "start": 0.5, "end": 0.9},
      {"type": "word", "text": "   ", "start": 1.0, "end": 1.1},
  )

  def model_dump(self, *, mode: str) -> dict[str, Any]:
    assert mode == "json"
    return {"text": self.text, "language_code": self.language_code, "fake": True}


class _FakeSTTClient:
  def __init__(self) -> None:
    self.calls: list[dict[str, Any]] = []
    self.uploads: list[bytes] = []
    self.stream_was_open: list[bool] = []
    self.speech_to_text = SimpleNamespace(convert=self._convert)

  def _convert(self, **kwargs: Any) -> _FakeSTTResult:
    self.calls.append(kwargs)
    stream = kwargs["file"]
    self.stream_was_open.append(not stream.closed)
    self.uploads.append(stream.read())
    return _FakeSTTResult()


def test_tts_disables_retries_with_fresh_options_and_cleans_up_context() -> None:
  client = _FakeTTSClient()
  provider = ElevenLabsTTSProvider.__new__(ElevenLabsTTSProvider)
  provider._client = client
  request = SynthesisRequest(
      text="Read this.",
      voice_id="voice-1",
      model_id="model-1",
      output_format="mp3_44100_128",
      previous_text="Before.",
      next_text="After.",
      previous_request_ids=("prior-1", "prior-2"),
      settings={"stability": 0.4},
  )

  responses = [provider.synthesize(request), provider.synthesize(request)]

  expected_call = {
      "voice_id": "voice-1",
      "text": "Read this.",
      "model_id": "model-1",
      "output_format": "mp3_44100_128",
      "request_options": {"max_retries": 0},
      "previous_text": "Before.",
      "next_text": "After.",
      "previous_request_ids": ["prior-1", "prior-2"],
      "voice_settings": {"stability": 0.4},
  }
  assert client.calls == [expected_call, expected_call]
  assert client.calls[0]["request_options"] is not client.calls[1]["request_options"]
  assert [response.request_id for response in responses] == ["request-1", "request-2"]
  assert [response.billed_characters for response in responses] == [17, 17]
  assert [context.enter_count for context in client.contexts] == [1, 1]
  assert [context.exited_with for context in client.contexts] == [None, None]

  assert [list(response.blocks) for response in responses] == [
      [b"audio-", b"bytes"],
      [b"audio-", b"bytes"],
  ]
  assert [context.exited_with for context in client.contexts] == [
      (None, None, None),
      (None, None, None),
  ]


def test_tts_cleans_up_context_when_streaming_fails() -> None:
  client = _FakeTTSClient(fail_during_stream=True)
  provider = ElevenLabsTTSProvider.__new__(ElevenLabsTTSProvider)
  provider._client = client

  response = provider.synthesize(
      SynthesisRequest("Text", "voice", "model", "mp3_44100_128")
  )

  with pytest.raises(RuntimeError, match="stream failed"):
    list(response.blocks)
  exc_type, exc, traceback = client.contexts[0].exited_with or (None, None, None)
  assert exc_type is RuntimeError
  assert str(exc) == "stream failed"
  assert traceback is not None
  assert client.calls[0]["request_options"] == {"max_retries": 0}


def test_stt_disables_retries_with_fresh_options_and_parses_response(
    tmp_path: Path,
) -> None:
  audio_path = tmp_path / "sample.mp3"
  audio_path.write_bytes(b"fake audio")
  client = _FakeSTTClient()
  provider = ElevenLabsSTTProvider.__new__(ElevenLabsSTTProvider)
  provider.model_id = "scribe-test"
  provider._client = client

  transcripts = [
      provider.transcribe(audio_path, language="en"),
      provider.transcribe(audio_path, language=None),
  ]

  first_call, second_call = client.calls
  assert first_call == {
      "model_id": "scribe-test",
      "file": first_call["file"],
      "timestamps_granularity": "word",
      "diarize": False,
      "request_options": {"max_retries": 0},
      "language_code": "en",
  }
  assert second_call == {
      "model_id": "scribe-test",
      "file": second_call["file"],
      "timestamps_granularity": "word",
      "diarize": False,
      "request_options": {"max_retries": 0},
  }
  assert first_call["request_options"] is not second_call["request_options"]
  assert client.stream_was_open == [True, True]
  assert client.uploads == [b"fake audio", b"fake audio"]
  assert first_call["file"].closed
  assert second_call["file"].closed

  for transcript in transcripts:
    assert transcript.text == "Hello world"
    assert transcript.language == "en"
    assert transcript.words == (
        TranscriptWord("Hello", 0.0, 0.4, 0.98),
        TranscriptWord("world", 0.5, 0.9, None),
    )
    assert transcript.raw == {
        "text": "Hello world",
        "language_code": "en",
        "fake": True,
    }
