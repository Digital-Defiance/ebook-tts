"""Cross-platform advisory workspace locking."""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType
from typing import IO

from ..errors import WorkspaceError


class WorkspaceLock:
  """Hold one non-blocking exclusive lock for a mutating command."""

  def __init__(self, workspace: Path) -> None:
    self.path = workspace / ".lock"
    self._stream: IO[bytes] | None = None

  def __enter__(self) -> WorkspaceLock:
    self.path.parent.mkdir(parents=True, exist_ok=True)
    stream = self.path.open("a+b")
    try:
      if os.name == "nt":
        import msvcrt

        stream.seek(0)
        if stream.read(1) == b"":
          stream.seek(0)
          stream.write(b"\0")
          stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
      else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
      stream.close()
      raise WorkspaceError(
          f"Another ebook-tts process is using workspace {self.path.parent}."
      ) from exc
    stream.seek(0)
    stream.truncate()
    stream.write(f"pid={os.getpid()}\n".encode())
    stream.flush()
    os.fsync(stream.fileno())
    self._stream = stream
    return self

  def __exit__(
      self,
      exc_type: type[BaseException] | None,
      exc: BaseException | None,
      traceback: TracebackType | None,
  ) -> None:
    if self._stream is None:
      return
    try:
      if os.name == "nt":
        import msvcrt

        self._stream.seek(0)
        msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
      else:
        import fcntl

        fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
    finally:
      self._stream.close()
      self._stream = None
