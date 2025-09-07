# Repository Guidelines

## Project Structure & Module Organization
- Root-level scripts: `lego_inventory.py` (inventory aggregation) and `lego_sorter.py` (sorting/labels).
- Visualizers: `billy-fitting.py` (Infinity Hearts into 2× BILLY 80×106) and `trofast-fitting.py` (TROFAST frames/baskets).
- Storage models: `storage_system.yaml` (Infinity Hearts) and `storage_trofast.yaml` (TROFAST).
- Data and assets: Excel/JSON artifacts in the repo root; optional `images/` and `ldraw_parts/` references. Timestamped copies live in `output/`.
- Tests live in `tests/` (create if missing): use `tests/test_*.py` and mirrors of module names.

## Build, Test, and Development Commands
- Install deps: `python -m pip install -r requirements.txt`.
- Run inventory: `python lego_inventory.py`.
- Run sorter tools: `python lego_sorter.py`.
- Visual layouts: `python billy-fitting.py --source <purchase-order.md> --output-dir output` or `python trofast-fitting.py --source <purchase-order.md> --output-dir output`.
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

## Agent-Specific Guidance (Codex CLI)
- Always use `apply_patch` to edit files. Prefer `rg` for searching; read files in ≤250 line chunks.
- Maintain timestamped outputs and run metadata:
  - Sorter writes `output/{TS}-{storage_label}-run_meta.json` and embeds a YAML summary+meta in the purchase order.
  - Container plans include a “Run Settings” section for quick comparisons.
- When adding CLI flags or presets, update README.md and this file; ensure new args are recorded in the run meta.
- Use `--output-dir output` for all generated artifacts; pass it to visualizers.
- Use the `update_plan` tool and keep exactly one step `in_progress`.

### Presets (Aliases)
- Trofast — Good Rare Split (C): `--preset-trofast-rare-split`
  - Expands to: `--storage storage_trofast.yaml --disable-1310 --exclude-duplo --mix-transparents --mix-rare --rare-threshold 0.15 --min-fill 0.5 --max-fill 0.85 --merge-trans-into-rare --pack-strategy balanced --run-trofast` (PDF enabled).
- BILLY — Structured Coding: `--preset-billy-structured`
  - Expands to: `--storage storage_system.yaml --disable-1310 --exclude-duplo --mix-transparents --mix-rare --rare-threshold 0.45 --min-fill 0.5 --max-fill 1.0 --pack-strategy greedy --run-billy` (PDF enabled).

### TROFAST Mode
- Only TROFAST_SHALLOW baskets are modeled; frames are frame-only SKUs; sorter buys only needed baskets + minimal frames.
- Visualizer uses equal visual rows, fills from bottom, and shows disabled slots where rails don’t exist.
- Exclude DUPLO with `--exclude-duplo` by default for TROFAST exercises.

### BILLY Mode
- For 2-cabinet limit exercises, disable 1310 (`--disable-1310`) to avoid full-shelf consumption unless explicitly requested.
- Use billy-fitting output to validate: prefer zero unplaced overflow; “top_overflow” indicates items placed on cabinet tops.

### Parameter Sweeps
- Keep run count reasonable (operator may impose limits; default ≤50).
- Capture results in a single JSON (e.g., `output/{TS}-*_sweep_results.json`) and provide a concise markdown report with the best mixes.
- Parse metrics from the purchase-order YAML and container plan (distinct colors, pooled buckets, utilization, fill stats).

### Known Pitfalls
- Ensure module-level globals are set when mutated in `main()` (e.g., declare `global PACK_MAX_FILL, CAPACITY`) to avoid `UnboundLocalError`.
- ReportLab missing → PDF export is skipped with a warning; don’t fail runs.
- Cairo/Pillow missing → PNG diagram generation may be skipped; SVG files are still written.

## Trademarks
- LEGO is a trademark of the LEGO Group of companies which does not sponsor, authorize or endorse this project.
- IKEA is a trademark of Inter IKEA Systems B.V. Product names and SKUs are referenced only for identification. Avoid adding brand assets; all trademarks remain with their owners.
