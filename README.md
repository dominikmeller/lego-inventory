# LEGO Inventory & Drawer Planner

A small toolkit to fetch LEGO set inventories, cache part images, and plan color‑sorted storage across drawer units with cost optimization.

## Project Structure
- `lego_inventory.py`: Fetches sets from Rebrickable, parses dimensions, caches images, exports inventory (`.xlsx`, `.md`, `.json`).
- `lego_sorter.py`: Packs parts by color into SMALL/MED/DEEP drawers, optimizes required units, and exports plan/purchase order.
- `requirements.txt`: Python dependencies.
- Outputs (created on run): `lego_inventory.xlsx`, `lego_inventory.md`, `aggregated_inventory.json`, `container_plan.md`, `container_plan.pdf`, `purchase-order.md`, `images/`.

## Install
- Python 3.10+ recommended.
- `python -m pip install -r requirements.txt`
- Set Rebrickable API key in `.env` as `REBRICKABLE_API_KEY=...` (or type when prompted).

## Quick Start
- Prepare sets: create `sets.txt` with one set per line (e.g., `31129`).
- Run inventory: `python lego_inventory.py` (prompts to run sorter when done).
- Or run explicitly: `python lego_sorter.py --json aggregated_inventory.json`.

## lego_inventory.py
- Key flags:
  - `--sets-file <path>`: read set numbers (default `sets.txt`).
  - `--no-images`: skip image downloads (use cached if present).
  - `--refresh-images`: force re-download images.
  - `--no-prompt`: do not ask to run sorter after export.
  - `--verbose | --quiet | --progress-json <path>`: script-level progress controls.
- Exports:
  - `lego_inventory.xlsx` (Inventory, Aggregated sheets)
  - `lego_inventory.md` (human-readable list)
  - `aggregated_inventory.json` (canonical input for sorter)
- Images cache under `images/{part_id}_{color_id}.jpg`.

## lego_sorter.py
- Packing: color‑first, per-piece volume, conservative fit; strategies:
  - `--pack-strategy greedy` (default): minimize new drawers, prefer smaller.
  - `--pack-strategy balanced`: reuse any existing fitting drawer before opening new; tie-break by best fill ratio.
- Outputs:
  - `container_plan.md` (+ optional `container_plan.pdf`)
  - `purchase-order.md` with drawer usage, costs, and shop links
- Flags:
  - `--json <path>` select input JSON (default `aggregated_inventory.json`).
  - `--no-pdf`, `--no-md`, `--purchase-only` to limit exports.
  - `--verbose | --quiet | --progress-json <path>` for progress.

## Drawer Model & Costs
- Usable fill factor `UTIL = 0.80`.
- Internal drawer dims (mm): SMALL `133×62×37`, MED `133×133×37`, DEEP `133×133×80`.
- Units and prices (`purchase-order.md` includes links):
  - 520: 20× SMALL — 101.00 PLN — https://rito.pl/szufladki-system-z-szufladami-organizer/35572-infinity-hearts-system-szuflad-organizer-regal-z-szufladami-plastik-520-20-szuflad-378x154x189cm-5713410019740.html
  - 5244: 4× SMALL, 4× MED, 2× DEEP — 95.00 PLN — https://rito.pl/szufladki-system-z-szufladami-organizer/35574-infinity-hearts-system-szuflad-organizer-regal-z-szufladami-plastik-5244-10-szuflad-378x154x189cm-5713410019764.html
- Tweak constants in `lego_sorter.py` if your hardware differs.

## Testing & Quality
- Tests (if present): `pytest -q`
- Lint/format: `ruff check .` (use `--fix`), `black .`
- Type-check: `mypy .`

## Troubleshooting
- Missing API key: set `REBRICKABLE_API_KEY` in `.env` or provide interactively.
- PDF export disabled: install ReportLab (in `requirements.txt`) or run with `--no-pdf`.
- Very large parts (e.g., hulls/baseplates) may not fit any drawer; they are reported and skipped in packing.
