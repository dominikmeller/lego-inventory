# LEGO Inventory & Drawer Planner

A small toolkit to fetch LEGO set inventories, cache part images, and plan color‑sorted storage across drawer units with cost optimization.

## Project Structure
- `lego_inventory.py`: Fetches sets from Rebrickable, parses dimensions, caches images, exports inventory (`.xlsx`, `.md`, `.json`).
- `lego_sorter.py`: Packs parts by color into configurable drawers, optimizes required units, and exports plan/purchase order.
- `billy-fitting.py`: Renders and validates Infinity Hearts organizers inside 2× BILLY 80×106 cm cabinets (front-view SVG/PNG).
- `trofast-fitting.py`: Renders and validates IKEA TROFAST frames and baskets (front-view SVG/PNG).
- `requirements.txt`: Python dependencies.
- `storage_system.yaml`: Configurable storage model for Infinity Hearts (drawer types, units, prices, links).
- `storage_trofast.yaml`: TROFAST storage model (mesh basket 42×30×10 cm; 99×44×94 cm frame with slots).
- Outputs: written to the repo root and timestamped copies to `output/` (see `--output-dir`).

## Install
- Python 3.10+ recommended.
- `python -m pip install -r requirements.txt`
- Set Rebrickable API key in `.env` as `REBRICKABLE_API_KEY=...` (or type when prompted).

## Quick Start
- Prepare sets: create `sets.txt` with one set per line (e.g., `31129`).
- Run inventory: `python lego_inventory.py` (prompts to run sorter when done).
- Or run explicitly: `python lego_sorter.py --json aggregated_inventory.json`.
- Timestamped copies are also saved under `output/{YYYYMMDD-HHMMSS}-*.{md,pdf,json,svg,png}`.

## lego_inventory.py
- Key flags:
  - `--sets-file <path>`: read set numbers (default `sets.txt`).
  - `--no-images`: skip image downloads (use cached if present).
  - `--refresh-images`: force re-download images.
  - `--refresh-cache`: refresh API cache (set details/parts) instead of using `cache/`.
  - `--no-prompt`: do not ask to run sorter after export.
  - `--verbose | --quiet | --progress-json <path>`: script-level progress controls.
- Exports:
  - `lego_inventory.xlsx` (Inventory, Aggregated sheets)
  - `lego_inventory.md` (human-readable list)
  - `aggregated_inventory.json` (canonical input for sorter)
  - Timestamped copies in `output/` (use `--output-dir`)
- Images cache under `images/{part_id}_{color_id}.jpg`.

## lego_sorter.py
- Packing: color‑first, per-piece volume, conservative fit; strategies:
  - `--pack-strategy greedy` (default): minimize new drawers, prefer smaller.
  - `--pack-strategy balanced`: reuse any existing fitting drawer before opening new; tie-break by best fill ratio.
- Outputs:
  - `container_plan.md` + `container_plan.pdf` (with per-item volumes and per-drawer totals/OK checks)
  - `purchase-order.md` with drawer usage, costs, links, and a machine-readable YAML summary (incl. volume and run meta)
  - Timestamped copies under `output/` via `--output-dir` (default: `output`)
- Flags:
  - `--json <path>` select input JSON (default `aggregated_inventory.json`).
  - `--output-dir <dir>` write timestamped outputs into a directory (default `output`).
  - `--no-pdf`, `--no-md`, `--purchase-only` to limit exports.
  - `--verbose | --quiet | --progress-json <path>` for progress.
  - `--storage <path>` to load a custom storage system YAML (default `storage_system.yaml`).
  - `--price-1310 <PLN>` override 1310 price (default 138.0); 1310 drawers enabled by default.
  - `--cost-optimisation` evaluate all rack combinations, pick cheapest; writes a CSV summary.
  - `--compare-out <csv>` path for rack mix comparison (default `racks_compare.csv`).
  - Color pooling and fill controls:
    - `--mix-transparents` pool all transparent colors into a `TRANSPARENT` bucket.
    - `--mix-rare` pool rare colors into a `RARE` bucket; `--rare-threshold <frac>` sets rarity as a fraction of the smallest drawer capacity.
    - `--merge-trans-into-rare` if the transparent pool cannot reach `--min-fill`.
    - `--min-fill <0..1>` enforce minimum per-drawer fill for shared buckets; `--max-fill <0..1>` cap packing headroom.
  - Exclusions and reports: `--exclude-duplo` and `--duplo-report <name>`.
  - Visualizers: `--run-billy` and/or `--run-trofast` to auto-generate cabinet/frame diagrams.

### Run Metadata
- Each run writes a sidecar JSON: `output/{TS}-{storage_label}-run_meta.json` with the exact command, parsed args, storage config, git info, and inventory hash.
- The purchase order embeds a “Machine-Readable Summary (YAML)” block with `solution`, `drawer_usage`, `volume_mm3`, and `meta`.
- The container plan includes a “Run Settings” section for quick diffing of key knobs.

### Presets (Aliases)
- Trofast — Good Rare Split (C):
  - `--preset-trofast-rare-split`
  - Expands to: `--storage storage_trofast.yaml --disable-1310 --exclude-duplo --mix-transparents --mix-rare --rare-threshold 0.15 --min-fill 0.5 --max-fill 0.85 --merge-trans-into-rare --pack-strategy balanced --run-trofast`
- BILLY — Structured Coding:
  - `--preset-billy-structured`
  - Expands to: `--storage storage_system.yaml --disable-1310 --exclude-duplo --mix-transparents --mix-rare --rare-threshold 0.45 --min-fill 0.5 --max-fill 1.0 --pack-strategy greedy --run-billy`

## Drawer Model & Costs
- Usable fill factor `UTIL = 0.80`.
- Internal drawer dims (mm) defaults:
  - SMALL `133×62×37`, MED `133×133×37`, DEEP `133×133×80`
  - 1310 system drawers: S1310 `160×86×39`, L1310 `223×160×39`, L1310_DEEP `223×160×85`
- Units and prices are defined in `storage_system.yaml` and rendered dynamically in `purchase-order.md`.

### TROFAST Mode
- Use `storage_trofast.yaml` to enable an IKEA TROFAST-based model:
  - Drawer type: `TROFAST_SHALLOW` (mesh 42×30×10 cm, 80% util volume).
  - Frame: `TROFAST_FRAME_99x44x94` with slots for shallow baskets; frame-only pricing (buy baskets separately).
- Sorter buys only needed baskets and minimum frames to host them.
- Visualizer: `trofast-fitting.py --source <purchase-order.md> [--output-dir output]` renders a front view; fills slots bottom-up with equal visual rows.

## Pipeline Overview
- Inventory pipeline:
  - Load set list (CLI or `sets.txt`), read API key from `.env`.
  - Use on-disk API cache in `cache/` when available; only sleep between API calls when not cached.
  - Fetch set details and parts, infer dimensions from names, cache images to `images/`.
  - Export `.xlsx`, `.md`, and `aggregated_inventory.json` (the sorter input).
- Sorter pipeline:
  - Load aggregated JSON, normalize dimensions, compute per-piece volumes.
  - Pack by color into configured drawer kinds; accurate 1310 sizes supported.
  - Optimise unit purchase:
    - Default: mixed 520/5244 (and 1310 for its kinds).
    - With `--cost-optimisation`: evaluate all rack subsets from YAML, repack per subset, and choose the lowest cost plan.
  - Export Markdown/PDF plan and a purchase order with dynamic unit composition, links, and costs.
  - Timestamped names + run meta sidecars are written to `output/`.

## Design Choices
- Conservative fit: axis-aligned compare of known dims; fallback dims for unknowns; tyre/wheel mm parsing; studs→mm mapping.
- Utilisation factor: 0.80 of nominal drawer volume to avoid overfill.
- Deterministic ordering: stable sorts for parts, colors, and output items for reproducibility.
- Config-driven storage: all drawer kinds and racks come from YAML (with sensible built-ins).
- Accurate 1310 modelling: distinct kind sizes (S1310/L1310/L1310_DEEP) and rack composition.
- Graceful deps: PDF export degrades if ReportLab missing; YAML overrides require PyYAML.
- Progress: `--verbose/--quiet/--progress-json` for both scripts.
 - Timestamped outputs and run metadata to enable repeatable tuning and comparisons.

## Rebrickable API Token (quick tutorial)
1) Create an account at https://rebrickable.com, then go to Account → My Profile → API.
2) Create an API Key. Copy the generated key string.
3) Save it locally so scripts can pick it up:
   - Put it in `.env` at the repo root:
     - `REBRICKABLE_API_KEY=your_api_key_here`
   - Or export in your shell before running:
     - `export REBRICKABLE_API_KEY=your_api_key_here`
4) Run the inventory script. If the key is not set, it will prompt you to enter it interactively.

Optional screenshots to include (place in `docs/screenshots/` and reference here):
- docs/screenshots/rebrickable-api-nav.png (where to find the API page)
- docs/screenshots/rebrickable-api-key.png (example API key view)

## Testing & Quality
- Tests (if present): `pytest -q`
- Lint/format: `ruff check .` (use `--fix`), `black .`
- Type-check: `mypy .`

## Troubleshooting
- Missing API key: set `REBRICKABLE_API_KEY` in `.env` or provide interactively.
- PDF export disabled: install ReportLab (in `requirements.txt`) or run with `--no-pdf`.
- YAML not applied: ensure `PyYAML` is installed (via requirements) and `storage_system.yaml` is present/valid.
- Very large parts (e.g., hulls/baseplates) may not fit any drawer; they are reported and skipped in packing.
 - ReportLab missing: PDF export prints a warning and is skipped; install `reportlab` from `requirements.txt`.
 - Cairo/Pillow missing: diagram PNG generation falls back or is skipped; SVGs are still saved.
