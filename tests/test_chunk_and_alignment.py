from __future__ import annotations

import pytest

from ebook_tts.errors import ConfigError
from ebook_tts.qa.alignment import align_text, missing_protected_terms
from ebook_tts.text.chunk import chunk_text


def test_chunking_preserves_tokens_and_limit() -> None:
  text = "\n\n".join(
      [
          "Dr. Rivera opened the door. This is a complete sentence with detail.",
          "Another paragraph contains enough words to require several bounded chunks. " * 8,
      ]
  )
  chunks = chunk_text(text, 120)
  assert len(chunks) > 2
  assert all(0 < len(chunk) <= 120 for chunk in chunks)
  assert text.split() == " ".join(chunks).split()


def test_chunking_splits_one_oversized_token_without_loss() -> None:
  text = "x" * 250
  chunks = chunk_text(text, 100)
  assert "".join(chunks) == text
  assert [len(chunk) for chunk in chunks] == [100, 100, 50]


def test_chunking_rejects_tiny_limit() -> None:
  with pytest.raises(ConfigError):
    chunk_text("hello world", 99)


def test_alignment_reports_exact_wer_cer_and_spans() -> None:
  metrics = align_text("Hello brave new world", "hello new world")
  assert metrics.word_edits == 1
  assert metrics.word_error_rate == pytest.approx(0.25)
  assert metrics.character_edits == 6
  assert metrics.spans[0]["operation"] == "delete"
  assert metrics.spans[0]["reference"] == "brave"


def test_protected_terms_are_unicode_and_case_normalized() -> None:
  missing = missing_protected_terms(
      "She raised a glass and said slàinte mhath.",
      ["Slàinte mhath", "Jasper Brooch"],
  )
  assert missing == ("Jasper Brooch",)
