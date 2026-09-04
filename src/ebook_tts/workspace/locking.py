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
    stream = self.path.open("a+b", buffering=0)
    try:
      if os.name == "nt":
        import msvcrt

        descriptor = stream.fileno()
        if os.fstat(descriptor).st_size == 0:
          stream.write(b"\0")
          stream.flush()
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
      else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
      stream.close()
      raise WorkspaceError(
          f"Another ebook-tts process is using workspace {self.path.parent}."
      ) from exc

    try:
      stream.seek(0)
      stream.truncate()
      stream.write(f"pid={os.getpid()}\n".encode())
      stream.flush()
      os.fsync(stream.fileno())
    except OSError as exc:
      stream.close()
      raise WorkspaceError(
          f"Could not initialize workspace lock {self.path}: {exc}"
      ) from exc
    except BaseException:
      stream.close()
      raise
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
    stream = self._stream
    unlock_error: OSError | None = None
    try:
      if os.name == "nt":
        import msvcrt

        descriptor = stream.fileno()
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
      else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError as error:
      unlock_error = error
    finally:
      stream.close()
      self._stream = None

    if unlock_error is not None:
      message = f"Could not release workspace lock {self.path}: {unlock_error}"
      if exc is not None:
        exc.add_note(message)
        return
      raise WorkspaceError(message) from unlock_error
