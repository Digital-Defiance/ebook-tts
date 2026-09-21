"""Verbalise times and decimals before local synthesis.

The manuscript and the STT reference stay unchanged. Only the text handed to a
TTS engine is rewritten, because Whisper reads digits back as numerals and a
transcript gate would otherwise miss a garbled clock reading.
"""

from __future__ import annotations

import re


ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen"
).split()
TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty"}

TIME_RE = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\d:])")
EQUATION_RE = re.compile(r"\b([a-zA-Z])\s*=\s*(\d+)\b")
DECIMAL_RE = re.compile(r"(?<![\d.])(\d+)\.(\d+)(?![\d.])")


def say_two_digit(value: int) -> str:
  if value < 20:
    return ONES[value]
  tens, units = divmod(value, 10)
  word = TENS.get(tens, "")
  return f"{word}-{ONES[units]}" if units else word


def say_hour(hour: int, *, style: str = "plain") -> str:
  if style == "radio" and hour < 10:
    return f"oh {ONES[hour]}"
  return say_two_digit(hour)


def say_minute(minute: int) -> str:
  if minute == 0:
    return "hundred"
  if minute < 10:
    return f"oh {ONES[minute]}"
  return say_two_digit(minute)


def say_time(match: re.Match[str], *, style: str = "plain") -> str:
  hour = int(match.group(1))
  minute = int(match.group(2))
  seconds = match.group(3)
  if hour > 23 or minute > 59:
    return match.group(0)
  spoken = f"{say_hour(hour, style=style)} {say_minute(minute)}"
  if seconds is not None:
    value = int(seconds)
    if value > 59:
      return match.group(0)
    unit = "second" if value == 1 else "seconds"
    spoken += f" and {say_two_digit(value)} {unit}"
  return spoken


def say_decimal(match: re.Match[str]) -> str:
  whole, frac = match.group(1), match.group(2)
  digits = " ".join(ONES[int(digit)] for digit in frac)
  return f"{whole} point {digits}"


def verbalize_speech_text(text: str, *, style: str = "plain") -> str:
  """Rewrite forms that local TTS front-ends predictably mis-verbalise."""
  out = TIME_RE.sub(lambda match: say_time(match, style=style), text)
  out = EQUATION_RE.sub(
      lambda match: (
          f"{match.group(1)} equals {say_two_digit(int(match.group(2)))}"
          if int(match.group(2)) <= 69
          else f"{match.group(1)} equals {match.group(2)}"
      ),
      out,
  )
  return DECIMAL_RE.sub(say_decimal, out)
