Contributing Guide
==================

Thanks for your interest in improving the LEGO Inventory & Drawer Planner! This guide explains how to set up your environment, run the tools, add tests, and submit pull requests.

Project layout
--------------
- lego_inventory.py — Fetch sets from Rebrickable, parse dimensions, export inventory.
- lego_sorter.py — Pack parts by color, optimize storage units, export plan and purchase order.
- billy-fitting.py — Render/validate Infinity Hearts organizers inside 2× BILLY 80×106 cabinets.
- trofast-fitting.py — Render/validate IKEA TROFAST frames and baskets.
- storage_system.yaml — Infinity Hearts storage model; storage_trofast.yaml — TROFAST model.
- output/ — Timestamped artifacts (created at runtime).

Prereqs
-------
- Python 3.10+
- Install dependencies:
  - python -m pip install -r requirements.txt
- Optional: Rebrickable API key for live inventory fetches (place in .env as REBRICKABLE_API_KEY=...).

Quick recipes
-------------
- Demo without API key using the bundled sample inventory:
  - Trofast (Good Rare Split):
    - python lego_sorter.py --json sample_aggregated_inventory.json --preset-trofast-rare-split --output-dir output
  - BILLY (Structured Coding):
    - python lego_sorter.py --json sample_aggregated_inventory.json --preset-billy-structured --output-dir output

Dev workflow
------------
1) Create a virtualenv, install requirements.
2) Make focused changes; run the relevant scripts with --output-dir output.
3) Run tests: pytest -q
4) Lint/format: ruff check . --fix && black .
5) Update README.md and AGENTS.md if flags/behaviors change; keep presets documented.
6) Open a PR with a clear summary, before/after details, and sample commands.

Coding style
------------
- PEP 8 + Black (88 cols). Use type hints where practical.
- Keep functions small and testable; avoid inline one-off scripts in tests.
- Prefer pathlib over os.path, f-strings, and early returns.

Tests
-----
- Use pytest under tests/.
- Focus on: dimension parsing, packing helpers, color pooling, optimizer behavior, and YAML export structure.

Docs & metadata
---------------
- Keep README.md user-focused; link to IKEA product searches for TROFAST/BILLY items.
- Ensure new CLI flags are captured in run metadata (sidecar JSON + purchase-order YAML).

Brand usage & trademarks
------------------------
- LEGO is a trademark of the LEGO Group of companies which does not sponsor, authorize or endorse this project.
- IKEA is a trademark of Inter IKEA Systems B.V. Product names/SKUs are referenced only for identification purposes.
- Do not add brand logos or copyrighted assets to the repository. Keep examples and screenshots neutral or clearly attributed.

Code of conduct
---------------
- Be respectful and inclusive. Report issues via GitHub issues.

License
-------
- By contributing, you agree your contributions will be licensed under the MIT License (see LICENSE).
