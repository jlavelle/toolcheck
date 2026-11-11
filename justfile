install:
    uv tool install . --force --refresh

sync:
    uv sync

check: pyright ruff

pyright:
    uv run pyright

ruff:
    ruff check
