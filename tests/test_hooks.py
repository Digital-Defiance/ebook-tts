from __future__ import annotations

import subprocess
import time
from dataclasses import replace
from pathlib import Path

from ebook_tts.config import default_config
from ebook_tts.editions.currency import editions_stale, newest_prose_mtime
from ebook_tts.errors import ConfigError
from ebook_tts.hooks.git import HOOK_MARK, git_dir, install_git_hooks
from ebook_tts.hooks.lifecycle import (
    check_saved_chapter,
    drain_pending,
    note_pending,
    pending_path,
    pre_commit_check,
    rebuild_if_stale,
    status_report,
)
from ebook_tts.manuscript.document import count_prose_words, render_chapter_markdown


def _git(cwd: Path, *args: str) -> None:
  subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _chapter_file(path: Path, *, words_offset: int = 0) -> Path:
  path.parent.mkdir(parents=True, exist_ok=True)
  body = "The apparatus receives tokens from the page and nothing else.\n"
  path.write_text(
      render_chapter_markdown(
          header={
              "chapter": 1,
              "title": "Start",
              "words": count_prose_words(body) + words_offset,
              "status": "draft",
          },
          prose=body,
      ),
      encoding="utf-8",
  )
  return path


def _manuscript_tree(root: Path, *, words_offset: int = 0) -> Path:
  chapters = root / "chapters"
  chapters.mkdir(parents=True)
  (root / "front-matter.md").write_text("# Title\n", encoding="utf-8")
  (root / "back-matter.md").write_text("", encoding="utf-8")
  return _chapter_file(chapters / "001-start.md", words_offset=words_offset)


def _config(root: Path):
  base = default_config()
  return replace(
      base,
      project=replace(base.project, source="manuscript"),
      manuscript=replace(base.manuscript, root=str(root), epub=str(root.parent / "book.epub")),
  )


def test_install_git_hooks_writes_marked_scripts(tmp_path: Path) -> None:
  _git(tmp_path, "init")
  written = install_git_hooks(tmp_path)
  assert {path.name for path in written} == {"pre-commit", "post-commit"}
  for path in written:
    text = path.read_text(encoding="utf-8")
    assert HOOK_MARK in text
    assert "ebook-tts hooks" in text
    assert path.stat().st_mode & 0o111


def test_install_refuses_to_clobber_a_foreign_hook(tmp_path: Path) -> None:
  _git(tmp_path, "init")
  hook = tmp_path / ".git" / "hooks" / "pre-commit"
  hook.parent.mkdir(parents=True, exist_ok=True)
  hook.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")
  try:
    install_git_hooks(tmp_path)
  except ConfigError as exc:
    assert "Refusing to replace" in str(exc)
  else:
    raise AssertionError("expected ConfigError")
  install_git_hooks(tmp_path, force=True)
  assert HOOK_MARK in hook.read_text(encoding="utf-8")


def test_git_dir_rejects_worktree_file(tmp_path: Path) -> None:
  _git(tmp_path, "init")
  git = tmp_path / ".git"
  # Simulate a linked worktree pointer file.
  import shutil

  shutil.rmtree(git)
  git.write_text("gitdir: /somewhere/else\n", encoding="utf-8")
  try:
    git_dir(tmp_path)
  except ConfigError as exc:
    assert "worktrees" in str(exc)
  else:
    raise AssertionError("expected ConfigError")


def test_pre_commit_rejects_staged_word_count_drift(tmp_path: Path) -> None:
  _git(tmp_path, "init")
  manuscript = tmp_path / "manuscript"
  _manuscript_tree(manuscript, words_offset=4)
  _git(tmp_path, "add", "manuscript/chapters/001-start.md")
  assert pre_commit_check(_config(manuscript), cwd=tmp_path) == 1


def test_pre_commit_accepts_clean_staged_chapter(tmp_path: Path) -> None:
  _git(tmp_path, "init")
  manuscript = tmp_path / "manuscript"
  _manuscript_tree(manuscript)
  _git(tmp_path, "add", "manuscript/chapters/001-start.md")
  assert pre_commit_check(_config(manuscript), cwd=tmp_path) == 0


def test_check_saved_chapter_notes_pending_defects(tmp_path: Path) -> None:
  manuscript = tmp_path / "manuscript"
  chapter = _manuscript_tree(manuscript, words_offset=3)
  config = _config(manuscript)
  findings = check_saved_chapter(chapter, config, cwd=tmp_path)
  assert findings
  pending = pending_path(tmp_path)
  assert pending.is_file()
  assert "CHAPTER_WORD_COUNT_MISMATCH" in pending.read_text(encoding="utf-8")
  drained = drain_pending(tmp_path)
  assert drained
  assert drain_pending(tmp_path) == []


def test_status_report_surfaces_pending_and_stale_edition(tmp_path: Path) -> None:
  manuscript = tmp_path / "manuscript"
  _manuscript_tree(manuscript)
  note_pending(tmp_path, "saved defect")
  blocks = status_report(_config(manuscript), cwd=tmp_path)
  joined = "\n".join(blocks)
  assert "saved defect" in joined
  assert "EPUB edition is older" in joined


def test_editions_stale_tracks_prose_mtime(tmp_path: Path) -> None:
  manuscript = tmp_path / "manuscript"
  _manuscript_tree(manuscript)
  epub = tmp_path / "book.epub"
  assert editions_stale(manuscript, epub, default_config().manuscript) is True
  epub.write_bytes(b"PK\x03\x04placeholder")
  # Ensure EPUB is newer than prose.
  future = time.time() + 5
  import os

  os.utime(epub, (future, future))
  assert editions_stale(manuscript, epub, default_config().manuscript) is False
  chapter = manuscript / "chapters" / "001-start.md"
  later = future + 10
  os.utime(chapter, (later, later))
  assert newest_prose_mtime(manuscript, default_config().manuscript) == later
  assert editions_stale(manuscript, epub, default_config().manuscript) is True


def test_rebuild_if_stale_reports_current_when_epub_is_fresh(tmp_path: Path) -> None:
  manuscript = tmp_path / "manuscript"
  _manuscript_tree(manuscript)
  epub = tmp_path / "book.epub"
  epub.write_bytes(b"PK\x03\x04placeholder")
  future = time.time() + 5
  import os

  os.utime(epub, (future, future))
  assert rebuild_if_stale(_config(manuscript), cwd=tmp_path) == "current"
