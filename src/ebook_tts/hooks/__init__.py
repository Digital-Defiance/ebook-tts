"""Portable lifecycle hooks for authored manuscripts."""

from .git import install_git_hooks
from .lifecycle import (
    check_saved_chapter,
    pre_commit_check,
    rebuild_if_stale,
    status_report,
)

__all__ = [
    "check_saved_chapter",
    "install_git_hooks",
    "pre_commit_check",
    "rebuild_if_stale",
    "status_report",
]
