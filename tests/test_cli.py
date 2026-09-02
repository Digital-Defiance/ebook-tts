from __future__ import annotations

import json
from pathlib import Path

from ebook_tts.cli import main


def test_inspect_and_plan_make_no_provider_calls(
    epub_factory,
    tmp_path: Path,
    capsys,
) -> None:
  epub = epub_factory(include_cover=False)
  assert main(["inspect", str(epub), "--json"]) == 0
  inspected = json.loads(capsys.readouterr().out)
  assert inspected["book"]["title"] == "Synthetic Public Domain Book"
  workspace = tmp_path / "cli-workspace"
  assert main(["plan", str(epub), "--workspace", str(workspace), "--json"]) == 0
  planned = json.loads(capsys.readouterr().out)
  assert planned["tracks"] == 2
  assert planned["max_characters"] == 9_500
  assert (workspace / "workspace.json").is_file()


def test_generate_requires_sample_or_explicit_yes(
    epub_factory,
    tmp_path: Path,
    capsys,
) -> None:
  epub = epub_factory(include_cover=False)
  workspace = tmp_path / "guard-workspace"
  assert main(["plan", str(epub), "--workspace", str(workspace)]) == 0
  capsys.readouterr()
  result = main(["generate", str(workspace)])
  assert result == 1
  assert "approved matching voice sample" in capsys.readouterr().err
