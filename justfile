install:
    uv tool install .

sync:
    uv sync

check: pyright ruff

pyright:
    uv run pyright

ruff:
    uv run ruff check
