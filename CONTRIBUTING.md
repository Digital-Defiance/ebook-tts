# Contributing

Thank you for improving ebook-tts.

## Setup

```bash
uv sync --extra dev
uv run pytest
```

`ffmpeg` and `ffprobe` enable media integration tests; tests skip those cases if
the tools are unavailable. Normal tests must never contact a paid provider.

## Rules

- Add synthetic or clearly public-domain fixtures only.
- Never commit EPUBs, extracted commercial text, generated production audio,
  API credentials, workspace manifests from private books, or provider history.
- Keep paid-operation retries in the durable state machine, not provider clients.
- New manifest shapes need a version/schema and compatibility discussion.
- Preserve exact completed checkpoints through errors and migrations.
- Add unit tests plus failure-injection coverage for billing-sensitive changes.
- Keep `inspect` read-only and `plan` network-free.

## Pull requests

Explain user-visible behavior, persisted-format impact, provider billing impact,
and tests performed. Keep unrelated changes separate. Run `uv run pytest` and
`uv build` before requesting review.

By contributing, you agree that your contribution is licensed under the MIT License.
