from __future__ import annotations

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
