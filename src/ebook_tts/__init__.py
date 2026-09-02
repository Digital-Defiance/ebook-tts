"""Build validated audiobooks from DRM-free EPUB publications."""

from importlib.metadata import PackageNotFoundError, version

try:
  __version__ = version("ebook-tts")
except PackageNotFoundError:
  __version__ = "0.1.0"

__all__ = ["__version__"]
