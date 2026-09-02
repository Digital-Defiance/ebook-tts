# Maintainer release procedure

1. Confirm the proposed package name is available and update project URLs in
   `pyproject.toml` when the public repository exists.
2. Review dependency pins and provider/API documentation.
3. Run the complete local gate:

   ```bash
   uv lock
   uv sync --all-extras
   uv run pytest
   uv run python -m compileall -q src
   uv build
   ```

4. Install the wheel, not the source tree, in a clean environment and run:

   ```bash
   ebook-tts --version
   ebook-tts --help
   ebook-tts inspect tests/fixtures/example.epub  # when a fixture is published
   ```

   The automated suite generates its EPUB fixture dynamically; maintainers may
   use any explicitly public-domain EPUB for this manual check.
5. Run a private live smoke request only with a dedicated low-cost account and
   explicit authorization. Public CI never does this.
6. Run the full workflow on a public-domain book and inspect both archives in
   their target applications.
7. Run the same code against private production regression material. Never add
   copyrighted text/audio or API credentials to this repository or CI logs.
8. Update `CHANGELOG.md`, version in `pyproject.toml`, and package schemas/docs.
9. Build and inspect wheel/sdist contents. Confirm no `.epub`, audio, workspace,
   quarantine, or environment files are included.
10. Create a signed version tag and GitHub release. The release workflow builds,
    tests, uploads artifacts, and can publish through the protected `pypi`
    environment using Trusted Publishing.
11. Verify the PyPI metadata and install the published wheel in another clean
    environment.

## Versioning

Use semantic versioning. Manifest-format compatibility is separate from package
versioning: incompatible persisted-record changes increment `manifest_version`
and require explicit migration behavior.

## Rollback

Never replace an existing release file. Yank a broken PyPI version, document the
reason, fix forward with a new version, and preserve user workspaces. Never ask
users to delete ambiguous paid-request evidence as part of an upgrade.
