"""Install portable git hooks that call ebook-tts lifecycle commands.

Git hooks are the durable replacement for Kiro's PostFileSave / Stop /
UserPromptSubmit bindings. They live in `.git/hooks/` and invoke this package
so the behavior stays versioned with ebook-tts rather than copied into a
prompt-only IDE.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import ConfigError

HOOK_MARK = "# ebook-tts-lifecycle"

PRE_COMMIT = f"""#!/bin/sh
{HOOK_MARK}
# Fail the commit when a staged chapter header no longer matches its prose.
# This is the git translation of the old on-save word-count check.
if command -v ebook-tts >/dev/null 2>&1; then
  exec ebook-tts hooks pre-commit
elif python3 -c "import ebook_tts" >/dev/null 2>&1; then
  exec python3 -m ebook_tts hooks pre-commit
fi
exit 0
"""

POST_COMMIT = f"""#!/bin/sh
{HOOK_MARK}
# Rebuild the EPUB when committed prose is newer. Never fails the commit;
# build errors are recorded for `ebook-tts status`.
if command -v ebook-tts >/dev/null 2>&1; then
  ebook-tts hooks post-commit || true
elif python3 -c "import ebook_tts" >/dev/null 2>&1; then
  python3 -m ebook_tts hooks post-commit || true
fi
exit 0
"""


def git_dir(cwd: Path) -> Path:
  current = cwd.resolve()
  for candidate in (current, *current.parents):
    git = candidate / ".git"
    if git.is_dir():
      return git
    if git.is_file():
      raise ConfigError("Git worktrees with a .git file are not yet supported.")
  raise ConfigError(f"No git repository above {cwd}")


def install_git_hooks(cwd: Path, *, force: bool = False) -> list[Path]:
  hooks = git_dir(cwd) / "hooks"
  hooks.mkdir(parents=True, exist_ok=True)
  written: list[Path] = []
  for name, body in (("pre-commit", PRE_COMMIT), ("post-commit", POST_COMMIT)):
    path = hooks / name
    if path.exists() and HOOK_MARK not in path.read_text(encoding="utf-8", errors="replace"):
      if not force:
        raise ConfigError(
            f"Refusing to replace existing git hook {path}. "
            "Re-run with --force after inspecting it."
        )
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    written.append(path)
  return written
