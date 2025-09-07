#!/usr/bin/env python3
"""
TROFAST Fitting Planner

Reads a purchase-order markdown (from lego_sorter.py) and produces a validated
layout for IKEA TROFAST frames and baskets, plus a front-view diagram (SVG + PNG).

Usage:
  python trofast-fitting.py --source path/to/purchase-order.md

Outputs to stdout the following sections:
  A) VALIDATION
  B) LAYOUT (YAML)
  C) FRONT DIAGRAM (SVG)
  D) BILL OF MATERIALS
  E) TEXT SUMMARY

Saves:
  trofast_fitting.svg and trofast_fitting.png in the current directory.

Trademarks & Disclaimer
- LEGO is a trademark of the LEGO Group of companies which does not sponsor, authorize or endorse this project.
- IKEA is a trademark of Inter IKEA Systems B.V. referenced for compatibility and informational purposes only.
- All product names, SKUs, and brand references are used solely to describe interoperability targets; all trademarks are the property of their respective owners.
- This tool distributes no copyrighted content from those brands and is intended for personal, non‑commercial planning and educational use.
"""

from __future__ import annotations

import argparse
import math
import re
from dataclasses import dataclass
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None


# ---------------- Fixed assumptions (tunable constants) ----------------
FRAME_COLS = 3   # TROFAST floor frame columns
# Visual per-column shallow basket slots (real-life rail layout):
# Left column: 6, middle: 4, right: 2 → total 12
COL_SLOTS: List[int] = [6, 4, 2]
SLOTS_PER_FRAME_VISUAL = sum(COL_SLOTS)
# Use uniform visual rows across columns so all drawers look the same size
VIS_ROWS = max(COL_SLOTS)

# Visual parameters
FRAME_W = 180
FRAME_H = 300
MARGIN = 20
SLOT_GAP_X = 6
SLOT_GAP_Y = 6

COLOR_USED = "#D1E9FF"
COLOR_EMPTY = "#FFFFFF"
COLOR_FRAME = "#000000"


# ---------------- Parsing ----------------
YAML_SECTION_HEADER = "## Machine-Readable Summary (YAML)"


def _extract_yaml_block(md: str) -> Optional[str]:
    if YAML_SECTION_HEADER not in md:
        return None
    # Find first fenced ```yaml block after the header
    pos = md.index(YAML_SECTION_HEADER)
    rest = md[pos:]
    m = re.search(r"```yaml\n([\s\S]*?)```", rest)
    if not m:
        return None
    return m.group(1)


def parse_source(path: str) -> Tuple[str, int, str, int, Dict[str, Dict], int]:
    """Parse purchase-order.md and extract TROFAST info.

    Returns (frame_code, frames_count, basket_kind, baskets_total, racks_info)
    """
    text = Path(path).read_text(encoding="utf-8")
    yml_txt = _extract_yaml_block(text)
    if yaml is None or not yml_txt:
        raise RuntimeError("Could not parse machine-readable summary from purchase-order.md")
    data = yaml.safe_load(yml_txt) or {}
    units: Dict[str, int] = {str(k): int(v) for k, v in (data.get("solution", {}).get("units", {}) or {}).items()}
    racks: Dict[str, Dict] = (data.get("racks", {}) or {})
    drawer_usage: Dict[str, int] = {str(k): int(v) for k, v in (data.get("drawer_usage", {}) or {}).items()}

    # Identify basket kind (prefer explicit TROFAST_SHALLOW if present)
    basket_kind = None
    for k in sorted(drawer_usage.keys()):
        if k.startswith("TROFAST_") and "FRAME" not in k:
            basket_kind = k
            break
    if not basket_kind:
        # fallback: search in units
        for k in sorted(units.keys()):
            if k.startswith("TROFAST_") and "FRAME" not in k:
                basket_kind = k
                break
    if not basket_kind:
        raise RuntimeError("Could not identify TROFAST basket kind from purchase-order")

    baskets_total = units.get(basket_kind, drawer_usage.get(basket_kind, 0))

    # Identify frame code by racks entry whose drawers include the basket_kind
    frame_code = None
    per_frame_slots = SLOTS_PER_FRAME_VISUAL
    for code, info in racks.items():
        drawers = info.get("drawers", {}) or {}
        if isinstance(drawers, dict) and drawers.get(basket_kind, 0) > 0:
            frame_code = str(code)
            per_frame_slots = int(drawers[basket_kind])
            break
    if not frame_code:
        # fallback by name
        for code in units.keys():
            if str(code).startswith("TROFAST_FRAME"):
                frame_code = str(code)
                break
    if not frame_code:
        frame_code = "TROFAST_FRAME"

    frames_count = int(units.get(frame_code, 0))
    if frames_count <= 0 and baskets_total > 0:
        frames_count = math.ceil(baskets_total / max(1, per_frame_slots))

    # return also racks info for external dims and price if needed
    # Extract per-frame slots from racks (rack model), if present
    rack_slots = 0
    if frame_code in racks and isinstance(racks.get(frame_code, {}).get("drawers"), dict):
        rack_slots = int(racks[frame_code]["drawers"].get(basket_kind, 0))
    return frame_code, frames_count, str(basket_kind), int(baskets_total), racks, rack_slots


# ---------------- Layout computation ----------------
def build_layout(frames: int, baskets: int) -> Dict:
    """Fill baskets across frames with visual column capacities [6,4,2].

    - All columns rendered with VIS_ROWS rows so every slot has equal size.
    - Fill from the bottom up within each column; rows above column capacity are disabled.
    """
    frames_yaml: List[Dict] = []
    remain = baskets
    for idx in range(frames):
        columns: List[List[str]] = []  # each column is list bottom→top? We'll store top→bottom for drawing
        used_per_col: List[int] = []
        filled = 0
        for c, cap in enumerate(COL_SLOTS):
            use = min(remain, cap)
            # Build a visual column of VIS_ROWS rows top→bottom
            # Mark rows above capacity as DISABLED; within capacity default to EMPTY; then fill from bottom
            col = ["EMPTY" for _ in range(VIS_ROWS)]
            # Rows indices within capacity region (top→bottom): indices VIS_ROWS-cap .. VIS_ROWS-1 are valid
            # Mark rows 0 .. VIS_ROWS-cap-1 as DISABLED
            disabled_upto = max(0, VIS_ROWS - cap)
            for r in range(disabled_upto):
                col[r] = "DISABLED"
            # Fill from bottom among valid rows
            filled_count = 0
            r = VIS_ROWS - 1
            while filled_count < use and r >= disabled_upto:
                col[r] = "SHALLOW"
                filled_count += 1
                r -= 1
            columns.append(col)
            used_per_col.append(use)
            filled += use
            remain -= use
        frames_yaml.append({
            "index": idx + 1,
            "columns": columns,  # top→bottom per column
            "used_per_col": used_per_col,
            "capacity_per_col": COL_SLOTS,
            "filled": filled,
            "capacity": SLOTS_PER_FRAME_VISUAL,
            "visual_rows": VIS_ROWS,
        })
    return {"frames": frames_yaml, "baskets_total": baskets, "visual_slots_per_frame": SLOTS_PER_FRAME_VISUAL, "col_slots": COL_SLOTS, "visual_rows": VIS_ROWS}


# ---------------- SVG/PNG rendering ----------------
def render_svg(layout: Dict, title: str = "TROFAST Plan") -> str:
    frames = layout.get("frames", [])
    n = len(frames)
    if n <= 0:
        return "<svg xmlns=\"http://www.w3.org/2000/svg\"></svg>"
    per_row = 2 if n > 1 else 1
    rows = math.ceil(n / per_row)
    width = per_row * FRAME_W + (per_row + 1) * MARGIN
    height = rows * FRAME_H + (rows + 2) * MARGIN + 20

    def rect(x, y, w, h, fill, stroke=COLOR_FRAME, label: Optional[str] = None):
        out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" stroke="{stroke}" stroke-width="1"/>']
        if label:
            out.append(f'<text x="{x + w/2}" y="{y + h/2}" font-size="10" text-anchor="middle" dominant-baseline="middle">{label}</text>')
        return "".join(out)

    parts: List[str] = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">']
    parts.append(f'<text x="{MARGIN}" y="{MARGIN}" font-size="14" font-weight="bold">{title}</text>')

    for i, fr in enumerate(frames):
        row = i // per_row
        col = i % per_row
        x0 = MARGIN + col * (FRAME_W + MARGIN)
        y0 = MARGIN + 20 + row * (FRAME_H + MARGIN)
        # Frame outline
        parts.append(rect(x0, y0, FRAME_W, FRAME_H, COLOR_EMPTY))
        # Slots (uniform visual rows per column; top→bottom)
        inner_x = x0 + 10
        inner_y = y0 + 10
        col_w = (FRAME_W - 20 - (FRAME_COLS - 1) * SLOT_GAP_X) / FRAME_COLS
        slot_h = (FRAME_H - 20 - (VIS_ROWS - 1) * SLOT_GAP_Y) / max(1, VIS_ROWS)
        columns = fr.get("columns", [])
        for c in range(FRAME_COLS):
            col = columns[c] if c < len(columns) else []
            for r in range(VIS_ROWS):
                cell = col[r] if r < len(col) else "DISABLED"
                if cell == "SHALLOW":
                    fill = COLOR_USED
                elif cell == "DISABLED":
                    fill = "#EEEEEE"
                else:
                    fill = COLOR_EMPTY
                x = inner_x + c * (col_w + SLOT_GAP_X)
                y = inner_y + r * (slot_h + SLOT_GAP_Y)
                parts.append(rect(x, y, col_w, slot_h, fill))
        # Label
        parts.append(f'<text x="{x0 + FRAME_W/2}" y="{y0 - 4}" font-size="10" text-anchor="middle">Frame {fr.get("index", i+1)} — {fr.get("filled",0)}/{fr.get("capacity",SLOTS_PER_FRAME_VISUAL)} baskets</text>')

    parts.append("</svg>")
    return "".join(parts)


def render_png(layout: Dict, out_path: str) -> bool:
    try:
        import cairosvg  # type: ignore
        svg_str = render_svg(layout)
        cairosvg.svg2png(bytestring=svg_str.encode("utf-8"), write_to=out_path)
        return True
    except Exception:
        pass
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore
    except Exception:
        return False

    frames = layout.get("frames", [])
    n = len(frames)
    if n <= 0:
        return False
    per_row = 2 if n > 1 else 1
    rows = math.ceil(n / per_row)
    width = per_row * FRAME_W + (per_row + 1) * MARGIN
    height = rows * FRAME_H + (rows + 2) * MARGIN + 20

    img = Image.new("RGB", (int(width), int(height)), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    def draw_rect(x, y, w, h, fill_color, outline=COLOR_FRAME):
        d.rectangle([int(x), int(y), int(x + w), int(y + h)], outline=outline, fill=fill_color, width=1)

    # Title
    if font:
        d.text((MARGIN, MARGIN), "TROFAST Plan", fill="black", font=font)

    for i, fr in enumerate(frames):
        row = i // per_row
        col = i % per_row
        x0 = MARGIN + col * (FRAME_W + MARGIN)
        y0 = MARGIN + 20 + row * (FRAME_H + MARGIN)
        draw_rect(x0, y0, FRAME_W, FRAME_H, COLOR_EMPTY)
        inner_x = x0 + 10
        inner_y = y0 + 10
        col_w = (FRAME_W - 20 - (FRAME_COLS - 1) * SLOT_GAP_X) / FRAME_COLS
        slot_h = (FRAME_H - 20 - (VIS_ROWS - 1) * SLOT_GAP_Y) / max(1, VIS_ROWS)
        columns = fr.get("columns", [])
        for c in range(FRAME_COLS):
            col = columns[c] if c < len(columns) else []
            for r in range(VIS_ROWS):
                cell = col[r] if r < len(col) else "DISABLED"
                if cell == "SHALLOW":
                    fill = COLOR_USED
                elif cell == "DISABLED":
                    fill = "#EEEEEE"
                else:
                    fill = COLOR_EMPTY
                x = inner_x + c * (col_w + SLOT_GAP_X)
                y = inner_y + r * (slot_h + SLOT_GAP_Y)
                draw_rect(x, y, col_w, slot_h, fill)
        # Label
        if font:
            txt = f"Frame {fr.get('index', i+1)} — {fr.get('filled',0)}/{fr.get('capacity',SLOTS_PER_FRAME_VISUAL)} baskets"
            d.text((x0 + FRAME_W/2 - 60, y0 - 12), txt, fill="black", font=font)

    img.save(out_path)
    return True


# ---------------- Main ----------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Lay out TROFAST baskets into frames; generate SVG+PNG front view.")
    ap.add_argument("--source", default="purchase-order.md", help="Path to purchase-order.md from sorter")
    ap.add_argument("--output-dir", default="output", help="Directory to write SVG/PNG outputs (default: output)")
    ap.add_argument("--label", default=None, help="Optional label to include in filenames; defaults parsed from --source")
    args = ap.parse_args()

    frame_code, frames_count, basket_kind, baskets_total, racks, rack_slots = parse_source(args.source)
    # Derive label and timestamp early for titles and filenames
    ts = time.strftime("%Y%m%d-%H%M%S")
    label = (args.label or "").strip() if args.label else None
    if not label:
        stem = Path(args.source).stem
        m = re.search(r"-purchase-order-(.+)$", stem)
        label = m.group(1) if m else stem
    try:
        import re as _re
        label = _re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-") or "trofast"
    except Exception:
        label = "trofast"
    # Per-frame basket capacity
    per_frame_slots = rack_slots or SLOTS_PER_FRAME_VISUAL

    # VALIDATION section
    lines_valid: List[str] = []
    lines_valid.append(f"Frame code: {frame_code}  |  Frames: {frames_count}")
    lines_valid.append(f"Basket kind: {basket_kind}  |  Baskets total: {baskets_total}")
    lines_valid.append(f"Rack slots per frame (model): {per_frame_slots or 0}  |  Visual columns: {COL_SLOTS} (sum={SLOTS_PER_FRAME_VISUAL})")
    frames_min = math.ceil(baskets_total / max(1, SLOTS_PER_FRAME_VISUAL)) if baskets_total > 0 else 0
    lines_valid.append(f"Frames required by count: {frames_min}")
    if frames_count < frames_min:
        lines_valid.append("WARNING: Fewer frames than required by basket count; layout will overflow")

    # Build layout
    layout = build_layout(max(frames_count, frames_min), baskets_total)

    # Print A/B/C/D/E sections
    print("A) VALIDATION")
    print("\n".join(lines_valid))

    print("\nB) LAYOUT (YAML)")
    if yaml is not None:
        print(yaml.safe_dump(layout, sort_keys=False).strip())
    else:
        print(str(layout))

    print("\nC) FRONT DIAGRAM (SVG)")
    svg_title = f"TROFAST Plan — {label} — {ts}"
    svg_str = render_svg(layout, title=svg_title)
    print(svg_str)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    svg_path = out_dir / f"{ts}-trofast_fitting-{label}.svg"
    png_path = out_dir / f"{ts}-trofast_fitting-{label}.png"
    try:
        svg_path.write_text(svg_str, encoding="utf-8")
        print(f"\n[Saved {svg_path.name} in {out_dir}]")
    except Exception:
        print("\n[Could not write trofast_fitting.svg]")
    png_ok = render_png(layout, str(png_path))
    if png_ok:
        print(f"\n[Saved {png_path.name} in {out_dir}]")
    else:
        print("\n[Could not write trofast_fitting.png — install cairosvg or Pillow]")

    # Bill of materials
    print("\nD) BILL OF MATERIALS")
    bom_lines = [
        f"- Frames: {max(frames_count, frames_min)}× {frame_code}",
        f"- Baskets: {baskets_total}× {basket_kind}",
    ]
    print("\n".join(bom_lines))

    print("\nE) TEXT SUMMARY")
    print(
        "Filled frames column-by-column with shallow baskets. Diagram shows used slots in blue. "
        "Increase frames to reduce column height or add mix of deep baskets if needed."
    )


if __name__ == "__main__":
    main()
