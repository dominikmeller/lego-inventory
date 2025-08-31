# Repository Guidelines

## Project Structure & Module Organization
- Root-level scripts: `lego_inventory.py` (inventory aggregation) and `lego_sorter.py` (sorting/labels).
- Data and assets: Excel/JSON artifacts in the repo root; optional `images/` and `ldraw_parts/` references.
- Tests live in `tests/` (create if missing): use `tests/test_*.py` and mirrors of module names.

## Build, Test, and Development Commands
- Install deps: `python -m pip install -r requirements.txt`.
- Run inventory: `python lego_inventory.py`.
- Run sorter tools: `python lego_sorter.py`.
- All tests: `pytest -q`; single test: `pytest tests/<file>::<TestClass>::<test_name>`.
- Lint: `ruff check .` (autofix with `ruff check . --fix`).
- Format: `black .` or `ruff format .`.
- Type-check: `mypy .`.

## Coding Style & Naming Conventions
- PEP 8 + Black (88 cols). Use type hints (e.g., `pandas.DataFrame`).
- Naming: functions/vars `snake_case`, classes `PascalCase`, constants `UPPER_CASE`.
- Imports grouped: stdlib, third-party, local (Ruff enforced). Avoid wildcard imports.
- HTTP: call `response.raise_for_status()` and raise explicit exceptions. Prefer early returns and f-strings.

## Testing Guidelines
- Framework: `pytest` with descriptive tests in `tests/test_*.py`.
- Focus coverage on data parsing, I/O boundaries, and request handling.
- Use fixtures/sample files; keep tests isolated from real caches and network.

## Commit & Pull Request Guidelines
- Commits: clear, present-tense summaries; Conventional Commits preferred (e.g., `feat: add sorter module`).
- PRs: include purpose, linked issues, and repro steps or sample commands.
- Pre-PR checklist: `pytest -q`, `ruff check .`, `black .`, and `mypy .` pass; tests updated when behavior changes.
- Keep diffs focused and small; document assumptions in the PR description.

## Security & Configuration Tips
- Never commit secrets; load via environment variables (`.env` for local only).
- Avoid committing large generated artifacts/caches; prefer reproducible generation via scripts.

