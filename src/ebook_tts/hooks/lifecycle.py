"""Lifecycle commands that replace IDE-specific save/stop/prompt hooks.

Kiro bound three jobs to editor events:

- PostFileSave: chapter-scope word-count/header check
- Stop: rebuild EPUB when prose is newer
- UserPromptSubmit: surface pending defects

Those triggers are not portable. This module exposes the same jobs as ordinary
commands so git hooks, editor tasks, or an agent session start can call them.
"""

from __future__ import annotations

from pathlib import Path

from ..editions.currency import editions_stale
from ..editions.epub import build_epub
from ..errors import ManuscriptError
from ..models import AppConfig
from ..manuscript.check import check_chapter
from ..manuscript.compile import manuscript_root
from ..manuscript.discover import load_chapter
from ..manuscript.document import ChapterDiagnostic


PENDING_NAME = Path(".build") / "pending-defects.txt"


def pending_path(cwd: Path) -> Path:
  return cwd / PENDING_NAME


def note_pending(cwd: Path, message: str) -> None:
  path = pending_path(cwd)
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open("a", encoding="utf-8") as handle:
    handle.write(message.rstrip() + "\n")


def drain_pending(cwd: Path) -> list[str]:
  path = pending_path(cwd)
  if not path.is_file():
    return []
  text = path.read_text(encoding="utf-8").strip()
  path.write_text("", encoding="utf-8")
  return text.splitlines() if text else []


def staged_chapter_paths(cwd: Path, config: AppConfig) -> tuple[Path, ...]:
  """Chapter files staged in git, used by the pre-commit hook."""
  import subprocess

  result = subprocess.run(
      ["git", "diff", "--cached", "--name-only", "-z"],
      cwd=cwd,
      capture_output=True,
      check=False,
  )
  if result.returncode != 0:
    return ()
  root = manuscript_root(config, cwd=cwd)
  chapters = (root / config.manuscript.chapters).resolve()
  found: list[Path] = []
  for raw in result.stdout.split(b"\0"):
    if not raw:
      continue
    path = (cwd / raw.decode("utf-8", errors="replace")).resolve()
    try:
      path.relative_to(chapters)
    except ValueError:
      continue
    if path.suffix == ".md" and path.is_file():
      found.append(path)
  return tuple(found)


def pre_commit_check(config: AppConfig, *, cwd: Path) -> int:
  """Exit 1 when staged chapters fail objective checks."""
  failures: list[str] = []
  for path in staged_chapter_paths(cwd, config):
    findings = check_saved_chapter(path, config, cwd=cwd)
    failures.extend(item.format_text() for item in findings)
  if failures:
    print("ebook-tts: staged chapter checks failed:")
    for line in failures:
      print(f"  {line}")
    return 1
  return 0


def check_saved_chapter(path: Path, config: AppConfig, *, cwd: Path) -> tuple[ChapterDiagnostic, ...]:
  """Objective check for one saved chapter. Never raises on a bad file."""
  try:
    document = load_chapter(path, config.manuscript)
  except OSError:
    return ()
  findings = tuple(
      item for item in check_chapter(document) if item.severity == "error"
  )
  if findings:
    note_pending(
        cwd,
        f"{path.name}\n" + "\n".join(f"  {item.format_text()}" for item in findings),
    )
  return findings


def rebuild_if_stale(config: AppConfig, *, cwd: Path) -> str:
  """Rebuild the EPUB only when manuscript prose is newer. Always safe to call."""
  root = manuscript_root(config, cwd=cwd)
  epub = Path(config.manuscript.epub)
  if not epub.is_absolute():
    epub = cwd / epub
  if not editions_stale(root, epub, config.manuscript):
    return "current"
  try:
    build_epub(config, cwd=cwd, if_stale=False)
  except ManuscriptError as exc:
    note_pending(cwd, f"Edition rebuild failed: {exc}")
    return "failed"
  return "rebuilt"


def status_report(config: AppConfig, *, cwd: Path) -> list[str]:
  """Human-readable defects and stale-edition warnings. Does not block."""
  from ..manuscript.check import check_manuscript

  blocks: list[str] = []
  saved = drain_pending(cwd)
  if saved:
    blocks.append("Chapter checks failed since the last status:\n" + "\n".join(saved))
  try:
    report = check_manuscript(manuscript_root(config, cwd=cwd), config)
  except ManuscriptError as exc:
    blocks.append(str(exc))
  else:
    errors = [item.format_text() for item in report.diagnostics if item.severity == "error"]
    if errors:
      blocks.append(
          "Objective manuscript defects outstanding:\n"
          + "\n".join(f"  {item}" for item in errors)
      )
  root = manuscript_root(config, cwd=cwd)
  epub = Path(config.manuscript.epub)
  if not epub.is_absolute():
    epub = cwd / epub
  if editions_stale(root, epub, config.manuscript):
    blocks.append(
        "The EPUB edition is older than the prose. "
        "`ebook-tts compile` rebuilds it."
    )
  return blocks
