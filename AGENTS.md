# AGENTS QUICK REFERENCE

This file summarises essential build, lint, test and style conventions for autonomous agents working in this repository.

### 🛠️ Build / Run / Test
1. **Install deps**: `python -m pip install -r requirements.txt` (pandas, requests, openpyxl, pytest, ruff, black, mypy)
2. **Run script**: `python lego-inventory.py`
3. **All tests**: `pytest -q`
4. **Single test**: `pytest tests/<file>::<TestClass>::<test_name>`
5. **Lint**: `ruff check .`  – autofix with `ruff check . --fix`
6. **Format**: `black .` *or* `ruff format .`
7. **Type-check**: `mypy .`
8. **Pre-commit** (if configured): `pre-commit run -a`

### ✨ Code Style Guidelines
1. Follow **PEP-8** plus:
2. Keep imports grouped: stdlib, third-party, local; use `ruff` to enforce.
3. Line length **88 chars** (Black default).
4. Use **type hints** everywhere; prefer `pandas.DataFrame` etc.
5. Functions & variables = `snake_case`; classes = `PascalCase`; constants = `UPPER_CASE`.
6. Avoid wildcard imports; import only what you need.
7. Raise explicit exceptions; check HTTP with `response.raise_for_status()`.
8. Never commit secrets – load via `dotenv` or env vars.
9. Return early to reduce nesting.
10. Prefer f-strings for interpolation.

_No Cursor or Copilot rule files are present in this repo at the time of writing._
