.PHONY: sync test check build

sync:
	uv sync --all-extras

test:
	uv run pytest

check:
	uv run python -m compileall -q src
	uv run pytest

build: check
	uv build
