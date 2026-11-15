install:
    uv tool install . --force --refresh

sync:
    uv sync

check: pyright ruff biome

pyright:
    uv run pyright

ruff:
    ruff check

biome:
    biome check --error-on-warnings
