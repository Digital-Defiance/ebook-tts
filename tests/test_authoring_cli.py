from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from ebook_tts.cli import main
from ebook_tts.manuscript.document import count_prose_words, render_chapter_markdown


def test_init_manuscript_and_check(tmp_path, capsys, monkeypatch) -> None:
  monkeypatch.chdir(tmp_path)
  manuscript = tmp_path / "manuscript"
  assert main(["init", "--manuscript", str(manuscript)]) == 0
  assert (tmp_path / "audiobook.toml").is_file()
  body = (
      "The apparatus receives tokens from the page and nothing else is required here.\n"
  )
  words = len(body.split())
  (manuscript / "chapters" / "001-start.md").write_text(
      f'---\nchapter: 1\ntitle: "Start"\nwords: {words}\nstatus: draft\n---\n{body}',
      encoding="utf-8",
  )
  assert main(["check", "--config", str(tmp_path / "audiobook.toml")]) == 0
  out = capsys.readouterr().out
  assert "Objective manuscript checks passed" in out


def test_check_fails_on_word_count_drift(tmp_path, capsys, monkeypatch) -> None:
  monkeypatch.chdir(tmp_path)
  manuscript = tmp_path / "manuscript"
  assert main(["init", "--manuscript", str(manuscript)]) == 0
  body = "One two three four five.\n"
  (manuscript / "chapters" / "001-start.md").write_text(
      render_chapter_markdown(
          header={
              "chapter": 1,
              "title": "Start",
              "words": count_prose_words(body) + 2,
              "status": "draft",
          },
          prose=body,
      ),
      encoding="utf-8",
  )
  assert main(["check", "--config", str(tmp_path / "audiobook.toml")]) == 1
  assert "CHAPTER_WORD_COUNT_MISMATCH" in capsys.readouterr().out


def test_compile_markdown_only(tmp_path, capsys, monkeypatch) -> None:
  monkeypatch.chdir(tmp_path)
  manuscript = tmp_path / "manuscript"
  assert main(["init", "--manuscript", str(manuscript)]) == 0
  body = "The apparatus receives tokens from the page and nothing else is required here.\n"
  (manuscript / "chapters" / "001-start.md").write_text(
      render_chapter_markdown(
          header={
              "chapter": 1,
              "title": "Start",
              "words": count_prose_words(body),
              "status": "draft",
          },
          prose=body,
      ),
      encoding="utf-8",
  )
  assert main(["compile", "--markdown-only", "--config", str(tmp_path / "audiobook.toml")]) == 0
  out = capsys.readouterr().out
  assert "compiled" in out
  compiled = tmp_path / ".build" / "compiled.md"
  assert compiled.is_file()
  text = compiled.read_text(encoding="utf-8")
  assert "# Chapter 1" in text
  assert "apparatus" in text


def test_extract_cli(epub_factory, tmp_path, capsys, monkeypatch) -> None:
  monkeypatch.chdir(tmp_path)
  epub = epub_factory(include_cover=False, text_repeat=1)
  destination = tmp_path / "from-epub"
  assert main(["extract", str(epub), "--manuscript", str(destination)]) == 0
  assert (destination / "chapters").is_dir()
  assert list((destination / "chapters").glob("*.md"))
  assert "Extracted" in capsys.readouterr().out


def test_status_reports_stale_edition(tmp_path, capsys, monkeypatch) -> None:
  monkeypatch.chdir(tmp_path)
  manuscript = tmp_path / "manuscript"
  assert main(["init", "--manuscript", str(manuscript)]) == 0
  body = "The apparatus receives tokens from the page and nothing else is required here.\n"
  (manuscript / "chapters" / "001-start.md").write_text(
      render_chapter_markdown(
          header={
              "chapter": 1,
              "title": "Start",
              "words": count_prose_words(body),
              "status": "draft",
          },
          prose=body,
      ),
      encoding="utf-8",
  )
  assert main(["status", "--config", str(tmp_path / "audiobook.toml")]) == 1
  assert "EPUB edition is older" in capsys.readouterr().out


def test_hooks_install_cli(tmp_path, capsys, monkeypatch) -> None:
  monkeypatch.chdir(tmp_path)
  import subprocess

  subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
  assert main(["hooks", "install"]) == 0
  out = capsys.readouterr().out
  assert "Installed" in out
  assert (tmp_path / ".git" / "hooks" / "pre-commit").is_file()


def _authored(manuscript: Path, chapter: int, title: str, body: str, **extra: object) -> None:
  keys = "".join(f"{key}: {value}\n" for key, value in extra.items())
  (manuscript / "chapters" / f"{chapter:03d}-ch.md").write_text(
      f'---\nchapter: {chapter}\ntitle: "{title}"\nwords: 0\nstatus: draft\n{keys}---\n{body}',
      encoding="utf-8",
  )


def test_check_write_counts_repairs_drift_then_passes(tmp_path, capsys, monkeypatch) -> None:
  monkeypatch.chdir(tmp_path)
  manuscript = tmp_path / "manuscript"
  assert main(["init", "--manuscript", str(manuscript)]) == 0
  config = str(tmp_path / "audiobook.toml")
  _authored(manuscript, 1, "Start", "One two three four five.\n")

  assert main(["check", "--config", config]) == 1
  capsys.readouterr()

  assert main(["check", "--config", config, "--write-counts"]) == 0
  out = capsys.readouterr().out
  assert "words: 0 -> 5" in out
  assert "Reconciled 1 declared word count(s)." in out

  assert main(["check", "--config", config]) == 0
  assert "Objective manuscript checks passed" in capsys.readouterr().out

  assert main(["check", "--config", config, "--write-counts"]) == 0
  assert "already matches" in capsys.readouterr().out


def test_check_write_counts_rejects_single_chapter_scope(tmp_path, monkeypatch) -> None:
  monkeypatch.chdir(tmp_path)
  manuscript = tmp_path / "manuscript"
  assert main(["init", "--manuscript", str(manuscript)]) == 0
  _authored(manuscript, 1, "Start", "One two three.\n")
  assert main([
      "check",
      "--config",
      str(tmp_path / "audiobook.toml"),
      "--write-counts",
      "--chapter",
      str(manuscript / "chapters" / "001-ch.md"),
  ]) == 1


def test_chapters_groups_and_filters_by_any_header_key(tmp_path, capsys, monkeypatch) -> None:
  monkeypatch.chdir(tmp_path)
  manuscript = tmp_path / "manuscript"
  assert main(["init", "--manuscript", str(manuscript)]) == 0
  config = str(tmp_path / "audiobook.toml")
  _authored(manuscript, 1, "First", "One two three.\n", pov_id="POV-NIA")
  _authored(manuscript, 2, "Second", "One two three four.\n", pov_id="POV-MARA")
  _authored(manuscript, 3, "Third", "One two.\n", pov_id="POV-NIA")
  assert main(["check", "--config", config, "--write-counts"]) == 0
  capsys.readouterr()

  assert main(["chapters", "--config", config, "--group-by", "pov_id"]) == 0
  out = capsys.readouterr().out
  assert "pov_id=POV-NIA  2 chapter(s), 5 words" in out
  assert "pov_id=POV-MARA  1 chapter(s), 4 words" in out
  assert "3 chapter(s), 9 words" in out

  assert main(["chapters", "--config", config, "--where", "pov_id=POV-NIA"]) == 0
  out = capsys.readouterr().out
  assert "First" in out and "Third" in out and "Second" not in out

  assert main(["chapters", "--config", config, "--where", "pov_id=POV-NOBODY"]) == 0
  assert "No chapters matched." in capsys.readouterr().out


def test_chapters_rejects_a_malformed_where_clause(tmp_path, monkeypatch) -> None:
  monkeypatch.chdir(tmp_path)
  manuscript = tmp_path / "manuscript"
  assert main(["init", "--manuscript", str(manuscript)]) == 0
  _authored(manuscript, 1, "First", "One two three.\n")
  assert main([
      "chapters", "--config", str(tmp_path / "audiobook.toml"), "--where", "pov_id",
  ]) == 1


def _png(path: Path) -> None:
  """Smallest valid PNG, so cover wiring can be tested without Pillow."""
  path.write_bytes(
      bytes.fromhex(
          "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
          "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
      )
  )


def test_book_cover_is_configuration_not_a_flag(tmp_path, monkeypatch) -> None:
  monkeypatch.chdir(tmp_path)
  manuscript = tmp_path / "manuscript"
  assert main(["init", "--manuscript", str(manuscript)]) == 0
  config_path = tmp_path / "audiobook.toml"
  _authored(manuscript, 1, "Start", "One two three.\n")
  _png(tmp_path / "cover.png")
  config_path.write_text(
      config_path.read_text(encoding="utf-8").replace(
          '# cover = "cover.png"', 'cover = "cover.png"'
      ),
      encoding="utf-8",
  )
  from ebook_tts.config import load_config

  config = load_config(config_path)
  # Resolved relative to the config file, not the process working directory.
  assert Path(config.book.cover) == tmp_path / "cover.png"

  assert main(["check", "--config", str(config_path), "--write-counts"]) == 0
  # plan picks the cover up with no flag at all
  assert main([
      "plan", "--config", str(config_path), "--workspace", str(tmp_path / "ws"),
  ]) == 0
  plan = json.loads(
      next((tmp_path / "ws" / "plans").iterdir()).joinpath("plan.json").read_text()
  )
  assert plan["cover"] is not None


def test_missing_configured_cover_fails_loudly(tmp_path, monkeypatch) -> None:
  monkeypatch.chdir(tmp_path)
  manuscript = tmp_path / "manuscript"
  assert main(["init", "--manuscript", str(manuscript)]) == 0
  config_path = tmp_path / "audiobook.toml"
  _authored(manuscript, 1, "Start", "One two three.\n")
  config_path.write_text(
      config_path.read_text(encoding="utf-8").replace(
          '# cover = "cover.png"', 'cover = "absent.png"'
      ),
      encoding="utf-8",
  )
  assert main(["check", "--config", str(config_path), "--write-counts"]) == 0
  # A coverless build is invisible in the output, so this must not pass quietly.
  assert main(["compile", "--config", str(config_path), "--markdown-only"]) == 0
  assert main([
      "plan", "--config", str(config_path), "--workspace", str(tmp_path / "ws"),
  ]) == 1


def test_doctor_warns_when_a_cover_has_no_alt_text(tmp_path, capsys, monkeypatch) -> None:
  monkeypatch.chdir(tmp_path)
  manuscript = tmp_path / "manuscript"
  assert main(["init", "--manuscript", str(manuscript)]) == 0
  config_path = tmp_path / "audiobook.toml"
  _png(tmp_path / "cover.png")
  config_path.write_text(
      config_path.read_text(encoding="utf-8").replace(
          '# cover = "cover.png"', 'cover = "cover.png"'
      ),
      encoding="utf-8",
  )
  main(["doctor", "--config", str(config_path)])
  out = capsys.readouterr().out
  assert "cover" in out
  assert "accessibility.cover_alt" in out
