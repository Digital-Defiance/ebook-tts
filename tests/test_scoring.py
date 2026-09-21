from ebook_tts.qa.scoring import assess_transcript, is_benign_replace, normalized_tokens


def test_numeral_words_are_benign() -> None:
  result = assess_transcript("The count was 10.", "The count was ten.")
  assert result["passed"] is True
  assert result["suspicious_spans"] == []


def test_clock_times_are_benign() -> None:
  result = assess_transcript(
      "The clock said 19:52.",
      "The clock said nineteen fifty-two.",
  )
  assert result["passed"] is True
  assert result["suspicious_spans"] == []


def test_radio_times_and_equations_are_benign() -> None:
  result = assess_transcript(
      "It happened at 07:02 and n = 4.",
      "It happened at seven oh two and n equals four.",
  )
  assert result["passed"] is True
  assert result["suspicious_spans"] == []


def test_uk_us_spelling_is_benign() -> None:
  result = assess_transcript(
      "The neighbour organised travelling.",
      "The neighbor organized traveling.",
  )
  assert result["passed"] is True
  assert result["suspicious_spans"] == []


def test_weak_function_word_flip_is_benign() -> None:
  assert is_benign_replace(["the"], ["a"]) is True
  result = assess_transcript("She sat on the chair.", "She sat on a chair.")
  assert result["passed"] is True


def test_empty_expected_fails_when_audio_has_speech() -> None:
  result = assess_transcript("", "unexpected words")
  assert result["passed"] is False
  assert result["wer"] == 1.0


def test_empty_expected_passes_when_transcript_empty() -> None:
  result = assess_transcript("", "")
  assert result["passed"] is True


def test_real_omission_fails_the_gate() -> None:
  result = assess_transcript(
      "Ravi Anand said it again and this time he read the dates.",
      "Ravi said it again.",
  )
  assert result["passed"] is False
  assert result["suspicious_spans"] or result["wer"] > 0.12


def test_normalized_tokens_fold_apostrophes() -> None:
  assert normalized_tokens("don’t") == ("don't",)
