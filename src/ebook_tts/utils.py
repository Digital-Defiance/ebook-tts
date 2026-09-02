"""Small deterministic filesystem, hashing, and naming helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
  """Serialize JSON deterministically for fingerprints."""
  return json.dumps(
      value,
      ensure_ascii=False,
      sort_keys=True,
      separators=(",", ":"),
      allow_nan=False,
  )


def jsonable(value: Any) -> Any:
  """Convert dataclasses, paths, tuples, and mappings to JSON-compatible data."""
  if is_dataclass(value):
    return jsonable(asdict(value))
  if isinstance(value, Path):
    return str(value)
  if isinstance(value, dict):
    return {str(key): jsonable(item) for key, item in value.items()}
  if isinstance(value, (tuple, list)):
    return [jsonable(item) for item in value]
  return value


def sha256_bytes(value: bytes) -> str:
  return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
  return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open("rb") as stream:
    for block in iter(lambda: stream.read(1024 * 1024), b""):
      digest.update(block)
  return digest.hexdigest()


def utc_now() -> str:
  return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(value: str, *, fallback: str = "book") -> str:
  """Create a portable ASCII filename component."""
  normalized = unicodedata.normalize("NFKD", value)
  ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
  slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_value).strip("-").lower()
  return slug or fallback


def track_stem(track_number: int, title: str) -> str:
  title_slug = slugify(title, fallback="section").replace("-", "_")
  return f"{track_number:03d}_{title_slug}"


def fsync_directory(path: Path) -> None:
  """Best-effort persistence of directory entries on supported platforms."""
  if os.name == "nt":
    return
  flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
  descriptor = os.open(path, flags)
  try:
    os.fsync(descriptor)
  finally:
    os.close(descriptor)


def atomic_write_bytes(path: Path, value: bytes, *, mode: int = 0o600) -> None:
  """Write and atomically publish one file in its destination directory."""
  path.parent.mkdir(parents=True, exist_ok=True)
  descriptor, temporary_name = tempfile.mkstemp(
      prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
  )
  temporary = Path(temporary_name)
  try:
    os.chmod(temporary, mode)
    with os.fdopen(descriptor, "wb") as stream:
      stream.write(value)
      stream.flush()
      os.fsync(stream.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)
  finally:
    if temporary.exists():
      temporary.unlink()


def atomic_write_text(path: Path, value: str, *, mode: int = 0o600) -> None:
  atomic_write_bytes(path, value.encode("utf-8"), mode=mode)


def atomic_write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
  payload = json.dumps(
      jsonable(value),
      ensure_ascii=False,
      indent=2,
      allow_nan=False,
  ) + "\n"
  atomic_write_text(path, payload, mode=mode)


def _reject_nonfinite_json(value: str) -> Any:
  raise ValueError(f"non-finite JSON number {value!r} is forbidden")


def load_json(path: Path) -> dict[str, Any]:
  try:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=_reject_nonfinite_json,
    )
  except (OSError, ValueError) as exc:
    from .errors import WorkspaceError

    raise WorkspaceError(f"Invalid JSON file {path}: {exc}") from exc
  if not isinstance(value, dict):
    from .errors import WorkspaceError

    raise WorkspaceError(f"Expected a JSON object in {path}.")
  return value


def ensure_relative(path: Path, root: Path) -> str:
  """Return a portable relative path and reject manifest path leakage."""
  try:
    return path.resolve().relative_to(root.resolve()).as_posix()
  except ValueError as exc:
    from .errors import WorkspaceError

    raise WorkspaceError(f"Artifact {path} is outside workspace {root}.") from exc
