"""User-actionable exception hierarchy."""


class EbookTTSError(RuntimeError):
  """Base class for expected command failures."""


class ConfigError(EbookTTSError):
  """Configuration is missing, invalid, or incompatible."""


class EpubError(EbookTTSError):
  """The input EPUB is malformed, unsafe, unsupported, or empty."""


class WorkspaceError(EbookTTSError):
  """The build workspace is invalid or conflicts with immutable state."""


class ProviderError(EbookTTSError):
  """A TTS or STT provider operation could not be completed safely."""


class AmbiguousRequestError(ProviderError):
  """A paid request may have succeeded and must not be retried automatically."""


class MediaError(EbookTTSError):
  """Audio inspection, assembly, or signal analysis failed."""


class QualityError(EbookTTSError):
  """A deterministic release quality gate failed."""


class PackagingError(EbookTTSError):
  """A distribution artifact could not be built or verified."""
