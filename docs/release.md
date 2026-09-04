# Maintainer release procedure

1. Confirm the `ebook-tts` package name and the public repository URLs in
   `pyproject.toml`. Verify that GitHub private vulnerability reporting is
   enabled for `Digital-Defiance/ebook-tts`.
2. Review dependency pins, provider/API documentation, version, and release tag.
3. Run the complete local gate without provider calls:

   ```bash
   UV_OFFLINE=1 uv lock --check
   UV_OFFLINE=1 uv sync --all-extras
   UV_OFFLINE=1 uv run pytest
   UV_OFFLINE=1 uv run python -m compileall -q src tests
   uv build
   ```

4. Install the wheel and sdist independently—not the source tree—into separate
   clean environments. For each installation, run:

   ```bash
   ebook-tts --version
   ebook-tts --help
   python -c "from importlib.resources import files; assert (files('ebook_tts') / 'schemas' / 'plan-v1.schema.json').is_file()"
   ```

   Also import `ebook_tts.workspace.request_identity` to catch missing package
   modules. Do not use an unpublished fixture path in this smoke test.
5. Inspect wheel and sdist member lists. Confirm that both contain the license,
   schemas, and all runtime modules, and contain no `.epub`, audio, workspace,
   quarantine, dotenv, config, or private provenance files.
6. Run a private live smoke request only with a dedicated low-cost account and
   explicit authorization. Public CI never does this.
7. Run the workflow on a public-domain book and inspect all selected outputs in
   their target applications. Keep private production regression material out
   of the repository and CI logs.
8. Update `CHANGELOG.md`, version, schemas, and documentation. Review the staged
   diff and secret/privacy scan before creating a commit or tag.
9. Create a signed version tag and GitHub release. The release job tests source,
   builds once, clean-installs and smoke-tests both wheel and sdist, then uploads
   those exact artifacts. PyPI publication uses the protected `pypi` environment
   and Trusted Publishing.
10. Verify PyPI metadata and project links, download both published distribution
    files, compare them with the release artifacts, and install each in another
    clean environment.

## Versioning

Use semantic versioning. Manifest-format compatibility is separate from package
versioning: incompatible persisted-record changes increment `manifest_version`
and require explicit migration behavior.

## Rollback

Never replace an existing release file. Yank a broken PyPI version, document the
reason, fix forward with a new version, and preserve user workspaces. Never ask
users to delete ambiguous paid-request evidence as part of an upgrade.
