"""Versioned narration normalization."""

from __future__ import annotations

import re
import unicodedata

from ..errors import ConfigError
from ..models import NormalizationRule


_FLAG_MAP = {
    "i": re.IGNORECASE,
    "m": re.MULTILINE,
    "s": re.DOTALL,
}


def normalize_narration(text: str, rules: tuple[NormalizationRule, ...]) -> str:
  """Normalize Unicode and apply checked user-defined replacements in order."""
  result = unicodedata.normalize("NFC", text)
  for index, rule in enumerate(rules, start=1):
    if rule.regex:
      flags = 0
      unknown = sorted(set(rule.flags).difference(_FLAG_MAP))
      if unknown:
        raise ConfigError(
            f"Normalization rule {index} has unsupported flags: {''.join(unknown)}"
        )
      for flag in rule.flags:
        flags |= _FLAG_MAP[flag]
      try:
        result, count = re.subn(rule.pattern, rule.replacement, result, flags=flags)
      except re.error as exc:
        raise ConfigError(f"Invalid regex in normalization rule {index}: {exc}") from exc
    else:
      count = result.count(rule.pattern)
      result = result.replace(rule.pattern, rule.replacement)

    if rule.expected_count is not None and count != rule.expected_count:
      raise ConfigError(
          f"Normalization rule {index} matched {count} time(s); expected "
          f"{rule.expected_count}."
      )
  return result
