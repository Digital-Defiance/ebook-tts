#!/usr/bin/env python3
"""Compatibility launcher for source checkouts.

Prefer the installed ``ebook-tts`` command. This file keeps ``python process.py``
working for contributors without retaining any book-specific behavior.
"""

from __future__ import annotations

import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent / "src"
if str(SOURCE) not in sys.path:
  sys.path.insert(0, str(SOURCE))

from ebook_tts.cli import main  # noqa: E402


if __name__ == "__main__":
  raise SystemExit(main())
