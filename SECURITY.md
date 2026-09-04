# Security policy

## Supported versions

Until 1.0, only the latest released minor version receives security fixes.

## Reporting

Do not open a public issue for vulnerabilities involving archive traversal,
secret disclosure, command execution, symlink/TOCTOU writes, provider billing,
or checkpoint bypass. Submit a private report through the repository's
[security advisory form](https://github.com/Digital-Defiance/ebook-tts/security/advisories/new).
If private vulnerability reporting is unavailable, contact the maintainer
through a monitored address published on the repository profile rather than
posting exploit details publicly.

Include a minimal synthetic reproduction. Never attach a private EPUB, API key,
provider response, production transcript, or generated commercial audiobook.

## Security boundaries

- EPUB and web/provider responses are untrusted input.
- API keys belong only in environment variables or an external secret manager.
- Workspaces may contain copyrighted normalized text and raw transcripts; do not
  sync or publish them unintentionally.
- `--allow-unvalidated`, `--allow-failed-qa`, `--yes`, and retry authorization
  are deliberate trust overrides.
- ebook-tts does not remove or bypass DRM.

The project validates ZIP paths and resource limits, avoids shell command
interpolation, keeps paths relative in portable manifests, and fails closed on
ambiguous paid requests. These controls reduce risk but do not make arbitrary
files or provider accounts inherently trusted.
