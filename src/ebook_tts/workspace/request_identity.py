from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..models import MODEL_PROFILES
from ..utils import canonical_json, sha256_text


def _required_string(generation: Mapping[str, Any], name: str) -> str:
  value = generation.get(name)
  if not isinstance(value, str) or not value:
    raise ValueError(f"generation.{name} must be a non-empty string")
  return value


def effective_context_characters(
    model_id: str,
    configured: int,
    *,
    legacy_v1: bool,
) -> int:
  """Return the text-context size used by the original paid request."""
  if isinstance(configured, bool) or not isinstance(configured, int) or configured < 0:
    raise ValueError("generation.context_characters must be a non-negative integer")
  if legacy_v1:
    # This is the exact compatibility rule used by the legacy-v1 generator.
    return 0 if model_id == "eleven_v3" else configured
  profile = MODEL_PROFILES.get(model_id)
  return configured if profile is None or profile.supports_text_context else 0


def adjacent_text_context(
    texts: Sequence[str],
    position: int,
    maximum: int,
) -> tuple[str, str]:
  """Derive previous/next request context without crossing a track boundary."""
  if not 0 <= position < len(texts):
    raise ValueError("chunk position is outside the track")
  previous = texts[position - 1][-maximum:] if maximum and position > 0 else ""
  following = texts[position + 1][:maximum] if maximum and position + 1 < len(texts) else ""
  return previous, following


def expanded_request_record(
    *,
    text: str,
    voice_id: str,
    model_id: str,
    output_format: str,
    previous_text: str,
    next_text: str,
    previous_request_ids: Sequence[str] = (),
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
  """Build the canonical request record used by native generation."""
  return {
      "text": text,
      "voice_id": voice_id,
      "model_id": model_id,
      "output_format": output_format,
      "previous_text": previous_text,
      "next_text": next_text,
      "previous_request_ids": list(previous_request_ids),
      "settings": dict(settings or {}),
  }


def sparse_legacy_request_record(
    *,
    text: str,
    voice_id: str,
    model_id: str,
    output_format: str,
    previous_text: str,
    next_text: str,
) -> dict[str, Any]:
  """Build the historical legacy-v1 request record with empty fields omitted."""
  request = {
      "voice_id": voice_id,
      "model_id": model_id,
      "output_format": output_format,
      "text": text,
  }
  if previous_text:
    request["previous_text"] = previous_text
  if next_text:
    request["next_text"] = next_text
  return request


def request_fingerprint(record: Mapping[str, Any]) -> str:
  return sha256_text(canonical_json(dict(record)))


def request_fingerprint_candidates(
    *,
    texts: Sequence[str],
    position: int,
    generation: Mapping[str, Any],
    legacy_v1: bool,
) -> frozenset[str]:
  """Recompute the only request fingerprints valid for one persisted chunk."""
  voice_id = _required_string(generation, "voice_id")
  model_id = _required_string(generation, "model_id")
  output_format = _required_string(generation, "output_format")
  context = effective_context_characters(
      model_id,
      generation.get("context_characters"),
      legacy_v1=legacy_v1,
  )
  settings = generation.get("voice_settings", {})
  if not isinstance(settings, dict):
    raise ValueError("generation.voice_settings must be a JSON object")
  if legacy_v1 and settings:
    raise ValueError("legacy-v1 generation.voice_settings must be empty")
  previous_text, next_text = adjacent_text_context(texts, position, context)
  expanded = expanded_request_record(
      text=texts[position],
      voice_id=voice_id,
      model_id=model_id,
      output_format=output_format,
      previous_text=previous_text,
      next_text=next_text,
      settings=settings,
  )
  candidates = {request_fingerprint(expanded)}
  if legacy_v1:
    sparse = sparse_legacy_request_record(
        text=texts[position],
        voice_id=voice_id,
        model_id=model_id,
        output_format=output_format,
        previous_text=previous_text,
        next_text=next_text,
    )
    candidates.add(request_fingerprint(sparse))
  return frozenset(candidates)
