from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ebook_tts.config import default_config
from ebook_tts.errors import ProviderError
from ebook_tts.models import DEFAULT_VOICE_ANCHOR, LocalTTSConfig
from ebook_tts.providers.base import SynthesisRequest, SynthesisResponse
from ebook_tts.providers.local import LocalTTSProvider
from ebook_tts.providers.local_turns import (
    generation_parts,
    narration_turns,
    plan_spoken_text,
    tagged_payload,
    tagged_payloads,
    turn_word_count,
)
from ebook_tts.workspace.generation import generate


def _local_config(**local_overrides):
  base = default_config()
  local = replace(base.tts.local, **local_overrides) if local_overrides else base.tts.local
  return replace(
      base,
      tts=replace(
          base.tts,
          provider="local",
          voice_id="narrator",
          model_id="mlx-community/fish-audio-s2-pro",
          max_characters=200_000,
          local=local,
      ),
  )


def test_turn_planning_tags_paragraphs_and_verbalizes() -> None:
  config = default_config().tts.local
  spoken = plan_spoken_text("It happened at 07:02.\n\nThen we left.", config)
  assert "seven oh two" in spoken
  turns = narration_turns(spoken, config)
  assert len(turns) == 2
  payload = tagged_payload(turns, config)
  assert payload.startswith("<|speaker:0|>")
  assert payload.count("<|speaker:0|>") >= 3


def test_verbalize_numerals_can_be_disabled() -> None:
  config = replace(default_config().tts.local, verbalize_numerals=False)
  assert plan_spoken_text("Meet at 19:52.", config) == "Meet at 19:52."


def test_radio_numeral_style_and_sentence_pause() -> None:
  config = replace(
      default_config().tts.local,
      numeral_style="radio",
      sentence_pause="long",
      sentence_turns=False,
  )
  spoken = plan_spoken_text("It happened at 07:02. Then we left.", config)
  assert "oh seven" in spoken or "oh seven" in spoken.replace("-", " ")
  turns = narration_turns("First sentence. Second sentence.", config)
  assert len(turns) == 1
  assert "[pause]" in turns[0]


def test_sentence_turns_split_paragraphs() -> None:
  config = replace(default_config().tts.local, sentence_turns=True, sentence_pause="none")
  turns = narration_turns("First sentence. Second sentence.", config)
  assert len(turns) >= 2


def test_tagged_payload_rejects_undersized_anchor() -> None:
  config = replace(default_config().tts.local, anchor=True, chunk_length=10_000)
  with pytest.raises(ValueError, match="Voice anchor"):
    tagged_payload(("Hello.",), config)
  assert len(DEFAULT_VOICE_ANCHOR.encode("utf-8")) > LocalTTSConfig().chunk_length


def test_generation_parts_pack_at_1100_words() -> None:
  # Build many short paragraphs that cross the long-context recovery threshold.
  paragraphs = tuple(f"Paragraph {index} has five spoken words here." for index in range(1, 301))
  assert sum(turn_word_count(part) for part in paragraphs) > 1100
  parts = generation_parts(paragraphs, 1100)
  assert len(parts) >= 2
  assert sum(len(part) for part in parts) == len(paragraphs)
  for part in parts[:-1]:
    assert sum(turn_word_count(unit) for unit in part) <= 1100
  # A single oversized paragraph is never split mid-turn.
  long = (" ".join(["word"] * 1200),)
  assert generation_parts(long, 1100) == (long,)
  assert generation_parts(paragraphs[:10], 0) == (paragraphs[:10],)


def test_tagged_payloads_reanchor_each_part() -> None:
  config = replace(
      default_config().tts.local,
      max_words_per_call=12,
      sentence_pause="none",
      sentence_turns=False,
  )
  turns = (
      "One two three four five six.",
      "Seven eight nine ten eleven twelve.",
      "Thirteen fourteen fifteen sixteen.",
  )
  payloads = tagged_payloads(turns, config)
  assert len(payloads) == 2
  for payload in payloads:
    assert payload.startswith("<|speaker:0|>Before the chapter begins")
    assert payload.count("Before the chapter begins") == 1


def test_max_words_per_call_rejects_sentence_turns() -> None:
  config = replace(
      default_config().tts.local,
      max_words_per_call=1100,
      sentence_turns=True,
  )
  with pytest.raises(ValueError, match="paragraph turns"):
    tagged_payloads(("Hello there.",), config)


def test_pause_tags_do_not_count_toward_word_budget() -> None:
  assert turn_word_count("Hello [short pause] world") == 2


def test_local_provider_uses_injectable_renderer(tmp_path: Path, tone_mp3: bytes) -> None:
  seen: list[tuple[str, ...]] = []

  def renderer(request: SynthesisRequest, turns: tuple[str, ...]) -> bytes:
    seen.append(turns)
    assert "nineteen fifty-two" in " ".join(turns)
    assert request.voice_id == "narrator"
    return tone_mp3

  provider = LocalTTSProvider(_local_config(), renderer=renderer)
  response = provider.synthesize(
      SynthesisRequest(
          text="The request went into the queue at 19:52.",
          voice_id="narrator",
          model_id="mlx-community/fish-audio-s2-pro",
          output_format="mp3_44100_128",
      )
  )
  assert isinstance(response, SynthesisResponse)
  assert response.blocks == (tone_mp3,)
  assert response.billed_characters is None
  assert provider.billed is False
  assert seen and "nineteen fifty-two" in seen[0][0]


def test_local_provider_rejects_empty_narration() -> None:
  provider = LocalTTSProvider(_local_config(), renderer=lambda request, turns: b"")
  with pytest.raises(ProviderError, match="empty narration"):
    provider.synthesize(
        SynthesisRequest(
            text="   \n\n  ",
            voice_id="narrator",
            model_id="mlx-community/fish-audio-s2-pro",
            output_format="mp3_44100_128",
        )
    )


def test_local_provider_requires_voice_id() -> None:
  config = replace(
      default_config(),
      tts=replace(default_config().tts, provider="local", voice_id=""),
  )
  with pytest.raises(ProviderError, match="voice ID"):
    LocalTTSProvider(config, renderer=lambda request, turns: b"x")


def test_local_provider_requires_reference_files_without_renderer() -> None:
  with pytest.raises(ProviderError, match="reference_wav"):
    LocalTTSProvider(_local_config())


def test_local_failures_are_retryable(
    epub_factory,
    tmp_path,
    tone_mp3,
    media_tools,
) -> None:
  from ebook_tts.epub.package import load_publication
  from ebook_tts.workspace.manifests import create_plan

  class FailingLocal:
    name = "local"
    billed = False
    version = "test"

    def synthesize(self, request: SynthesisRequest) -> SynthesisResponse:
      raise RuntimeError("mlx blew up")

  config = _local_config()
  publication = load_publication(epub_factory(include_cover=False, text_repeat=1), config)
  plan = create_plan(publication, config, tmp_path / "workspace")
  try:
    generate(plan=plan, config=config, provider=FailingLocal(), tools=media_tools)
  except ProviderError as exc:
    assert "can be retried" in str(exc)
  else:
    raise AssertionError("expected ProviderError")
  markers = list((plan.workspace / "runs").rglob("*.attempt.json"))
  assert markers == []


def test_local_provider_generate_with_renderer(
    epub_factory,
    tmp_path,
    tone_mp3,
    media_tools,
) -> None:
  from ebook_tts.epub.package import load_publication
  from ebook_tts.workspace.manifests import create_plan

  calls = {"count": 0}

  def renderer(request: SynthesisRequest, turns: tuple[str, ...]) -> bytes:
    calls["count"] += 1
    assert turns
    return tone_mp3

  config = _local_config()
  publication = load_publication(epub_factory(include_cover=False, text_repeat=1), config)
  plan = create_plan(publication, config, tmp_path / "workspace")
  provider = LocalTTSProvider(config, renderer=renderer)
  run = generate(plan=plan, config=config, provider=provider, tools=media_tools)
  assert run.manifest["status"] == "complete"
  assert calls["count"] >= 1
  assert list((plan.workspace / "runs").rglob("*.attempt.json")) == []
