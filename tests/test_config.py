from __future__ import annotations

from pathlib import Path

import pytest

from ebook_tts.config import default_config, load_config
from ebook_tts.errors import ConfigError
from ebook_tts.text.normalize import normalize_narration


def test_multilingual_v2_default_is_9500(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.delenv("ELEVENLABS_MODEL_ID", raising=False)
  assert default_config().tts.max_characters == 9_500


def test_config_rejects_model_limit_violation(tmp_path: Path) -> None:
  config = tmp_path / "bad.toml"
  config.write_text(
      '[tts]\nmodel_id="eleven_multilingual_v2"\nmax_characters=10001\n',
      encoding="utf-8",
  )
  with pytest.raises(ConfigError, match="10,000-character"):
    load_config(config)


def test_checked_normalization_count(tmp_path: Path) -> None:
  config = tmp_path / "book.toml"
  config.write_text(
      '''[[normalization]]
pattern = "teh"
replacement = "the"
expected_count = 2
''',
      encoding="utf-8",
  )
  rules = load_config(config).normalization
  assert normalize_narration("teh and teh", rules) == "the and the"
  with pytest.raises(ConfigError, match="matched 1"):
    normalize_narration("only teh", rules)
