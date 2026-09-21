from ebook_tts.text.spoken import verbalize_speech_text


def test_clock_times_are_spoken_as_words() -> None:
  spoken = verbalize_speech_text("The request went into the queue at 19:52.")
  assert "nineteen fifty-two" in spoken
  assert "19:52" not in spoken


def test_decimal_and_equals_are_spoken() -> None:
  spoken = verbalize_speech_text("Measured 2.1 per second, n = 4.")
  assert "2 point one" in spoken
  assert "n equals four" in spoken


def test_radio_style_hours() -> None:
  spoken = verbalize_speech_text("Meet at 07:02.", style="radio")
  assert "oh seven" in spoken
  assert "oh two" in spoken
  assert "07:02" not in spoken


def test_invalid_clock_times_are_left_alone() -> None:
  spoken = verbalize_speech_text("Broken stamp 25:00 and 12:99.")
  assert "25:00" in spoken
  assert "12:99" in spoken


def test_plain_style_does_not_prefix_oh_for_single_digit_hour() -> None:
  spoken = verbalize_speech_text("Meet at 07:02.", style="plain")
  assert "seven oh two" in spoken
  assert "oh seven" not in spoken
