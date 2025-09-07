#!/usr/bin/env python3
"""
Billy Fitting Planner

Reads a purchase-order markdown (from lego_sorter.py) and produces a validated
storage layout for Infinity Hearts organizer modules inside two IKEA BILLY
80×106 cm cabinets.

Usage:
  python billy-fitting.py --source purchase-order.md

Outputs to stdout the following sections:
  A) VALIDATION
  B) LAYOUT (YAML) — machine-readable
  C) FRONT DIAGRAM (SVG)
  D) BILL OF MATERIALS
  E) TEXT SUMMARY

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
MAX_TOTAL_WIDTH_CM = 160.0
CABINET_MODEL = "BILLY 80x106"
USABLE_SHELF_WIDTH_CM = 76.0
USABLE_SHELF_DEPTH_CM = 26.0
LEVELS_PER_CABINET = 4  # 4 usable levels per cabinet
NUM_CABINETS = 2        # Two cabinets side-by-side → 160 cm total width

# Slot color mapping per SKU
COLOR_MAP: Dict[str, str] = {
    "520": "#D1E9FF",     # light blue
    "5244": "#FFE9A8",    # light yellow
    "1310": "#FFD1D1",    # light red
    "EMPTY": "#FFFFFF",    # white
}


@dataclass
class Module:
    code: str
    count: int
    length_cm: float  # Długość (left-right across shelf)
    depth_cm: float   # Szerokość (front-back)
    height_cm: float  # Wysokość (vertical)

    def per_shelf_capacity(self) -> int:
        return int(USABLE_SHELF_WIDTH_CM // self.length_cm)


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


def parse_source(path: str) -> Tuple[Dict[str, int], Dict[str, Tuple[float, float, float]], List[str]]:
    """Parse purchase-order.md.

    Returns (units_per_code, dims_per_code_cm[L,W,H], notes)
    """
    text = Path(path).read_text(encoding="utf-8")
    notes: List[str] = []

    # Defaults per brief if not present in source
    default_dims = {
        "520": (37.8, 15.4, 18.9),
        "5244": (37.8, 15.4, 18.9),
        "1310": (44.9, 18.0, 24.7),
    }

    # Prefer YAML block
    yml_txt = _extract_yaml_block(text)
    units: Dict[str, int] = {}
    dims: Dict[str, Tuple[float, float, float]] = {}
    parsed_yaml = False
    if yaml is not None and yml_txt:
        try:
            data = yaml.safe_load(yml_txt)
            sol = (data or {}).get("solution", {})
            racks = (data or {}).get("racks", {})
            units = {str(k): int(v) for k, v in sol.get("units", {}).items()}
            for code, info in racks.items():
                ext = info.get("external_cm")
                if isinstance(ext, list) and len(ext) == 3:
                    dims[str(code)] = (float(ext[0]), float(ext[1]), float(ext[2]))
            for code in units.keys():
                if code not in dims and code in default_dims:
                    dims[code] = default_dims[code]
            parsed_yaml = True
        except Exception:
            parsed_yaml = False

    if not parsed_yaml:
        # Fallback: parse Units to purchase lines and embedded dimensions "— L×W×H cm"
        units_pat = re.compile(r"^-\s*(\d{3,4})\b.*?\*\*(\d+)\*\*.*$", re.MULTILINE)
        dim_pat = re.compile(r"—\s*([0-9]+(?:\.[0-9]+)?)×([0-9]+(?:\.[0-9]+)?)×([0-9]+(?:\.[0-9]+)?)\s*cm")
        for m in units_pat.finditer(text):
            code, cnt = m.group(1), int(m.group(2))
            units[code] = cnt
            # Try to find dims on the same line
            line = m.group(0)
            dm = dim_pat.search(line)
            if dm:
                dims[code] = (float(dm.group(1)), float(dm.group(2)), float(dm.group(3)))
        for code, d in default_dims.items():
            if code in units and code not in dims:
                dims[code] = d

    # Sanity: ensure we only keep known SKUs
    units = {k: v for k, v in units.items() if v > 0 and k in ("520", "5244", "1310")}
    dims = {k: dims.get(k, default_dims[k]) for k in units.keys()}

    # Note differences from provided defaults
    for k, d in dims.items():
        if k in default_dims and any(abs(d[i] - default_dims[k][i]) > 1e-6 for i in range(3)):
            notes.append(f"SKU {k} dimensions differ from defaults: {default_dims[k]} → {d}")

    return units, dims, notes


# ---------------- Validation ----------------
def validate_modules(units: Dict[str, int], dims: Dict[str, Tuple[float, float, float]]) -> Tuple[Dict[str, int], List[str]]:
    modules: Dict[str, Module] = {}
    errors: List[str] = []
    for code, cnt in units.items():
        L, W, H = dims[code]
        m = Module(code=code, count=cnt, length_cm=L, depth_cm=W, height_cm=H)
        cap = m.per_shelf_capacity()
        if cap < 1:
            errors.append(f"{code}: Długość {L} cm too wide for 76 cm shelf")
        if code in ("520", "5244") and 2 * L > USABLE_SHELF_WIDTH_CM + 1e-6:
            errors.append(f"{code}: 2 × Długość {L} cm exceeds 76 cm width")
        if W > USABLE_SHELF_DEPTH_CM + 1e-6:
            errors.append(f"{code}: Szerokość {W} cm exceeds 26 cm depth")
        modules[code] = m
    return {k: modules[k].per_shelf_capacity() for k in modules}, errors


# ---------------- Layout computation ----------------
def compute_layout(units: Dict[str, int], dims: Dict[str, Tuple[float, float, float]], per_cap: Dict[str, int]) -> Tuple[Dict, List[str]]:
    notes: List[str] = []

    # Determine inside capacity
    total_slots = NUM_CABINETS * LEVELS_PER_CABINET * 2  # two side-by-side per shelf

    # Handle 1310: prefer on top; if cannot fit on top (width), place inside (full shelf)
    count_520 = units.get("520", 0)
    count_5244 = units.get("5244", 0)
    count_1310 = units.get("1310", 0)
    L1310 = dims.get("1310", (0, 0, 0))[0]

    # Top overflow packing (two tops, each 80 cm wide, total 160 cm)
    top_left: List[str] = []
    top_right: List[str] = []
    top_left_width = 0.0
    top_right_width = 0.0

    def place_top(code: str, L: float) -> bool:
        nonlocal top_left_width, top_right_width
        # choose side with more remaining width
        remaining_left = 80.0 - top_left_width
        remaining_right = 80.0 - top_right_width
        if remaining_left >= remaining_right:
            if L <= remaining_left + 1e-6:
                top_left.append(code)
                top_left_width += L
                return True
            if L <= remaining_right + 1e-6:
                top_right.append(code)
                top_right_width += L
                return True
        else:
            if L <= remaining_right + 1e-6:
                top_right.append(code)
                top_right_width += L
                return True
            if L <= remaining_left + 1e-6:
                top_left.append(code)
                top_left_width += L
                return True
        return False

    # Place 1310 on top first
    inside_1310 = 0
    for _ in range(count_1310):
        if L1310 > 0 and place_top("1310", L1310):
            continue
        # If cannot place on top, plan to place inside (full shelf)
        inside_1310 += 1
    if inside_1310:
        notes.append(f"Placed {inside_1310}× 1310 inside (full shelf each) as top width was insufficient")

    # Internal shelves structure: two cabinets × 4 levels × (left,right)
    shelves = {
        0: {lvl: {"left": "EMPTY", "right": "EMPTY"} for lvl in range(1, LEVELS_PER_CABINET + 1)},
        1: {lvl: {"left": "EMPTY", "right": "EMPTY"} for lvl in range(1, LEVELS_PER_CABINET + 1)},
    }

    # Deduct shelves consumed by inside 1310 (each consumes both slots on a level)
    # Prefer placing inside-1310 on top inside levels (level 4) then level 1
    levels_order_for_1310 = [(cab, 4) for cab in (0, 1)] + [(cab, 1) for cab in (0, 1)] + [(cab, 3) for cab in (0, 1)] + [(cab, 2) for cab in (0, 1)]
    li = 0
    while inside_1310 > 0 and li < len(levels_order_for_1310):
        cab, lvl = levels_order_for_1310[li]
        if shelves[cab][lvl]["left"] == "EMPTY" and shelves[cab][lvl]["right"] == "EMPTY":
            shelves[cab][lvl]["left"] = "1310"
            shelves[cab][lvl]["right"] = "1310"
            total_slots -= 2
            inside_1310 -= 1
        li += 1

    # Place 5244 on middle levels first (level 2 & 3), then others
    def fill_sku(code: str, count: int) -> int:
        # fills across both cabs for specified level ordering
        nonlocal total_slots
        levels_order = [2, 3, 1, 4]
        for lvl in levels_order:
            for cab in (0, 1):
                for side in ("left", "right"):
                    if count <= 0:
                        return 0
                    if shelves[cab][lvl][side] == "EMPTY":
                        shelves[cab][lvl][side] = code
                        count -= 1
                        total_slots -= 1
        return count

    remaining_5244 = fill_sku("5244", count_5244)

    # Fill remaining with 520
    def fill_any(code: str, count: int) -> int:
        nonlocal total_slots
        for lvl in (1, 2, 3, 4):
            for cab in (0, 1):
                for side in ("left", "right"):
                    if count <= 0:
                        return 0
                    if shelves[cab][lvl][side] == "EMPTY":
                        shelves[cab][lvl][side] = code
                        count -= 1
                        total_slots -= 1
        return count

    remaining_520 = fill_any("520", count_520)

    # Overflow handling: prefer to overflow 520 first
    overflow: List[str] = []
    # First, place overflow on top (respecting per-cabinet width)
    if remaining_520 > 0:
        L520 = dims["520"][0]
        while remaining_520 > 0 and place_top("520", L520):
            remaining_520 -= 1
    if remaining_5244 > 0:
        L5244 = dims["5244"][0]
        while remaining_5244 > 0 and place_top("5244", L5244):
            remaining_5244 -= 1
    # Any still remaining are unplaced overflow
    overflow += ["520"] * remaining_520 + ["5244"] * remaining_5244
    if remaining_520 or remaining_5244:
        notes.append("Top width insufficient for all overflow; some items remain unplaced")

    # Build YAML layout structure
    cabinets_yaml: List[Dict] = []
    for cab in (0, 1):
        shelves_list = []
        for lvl in range(1, LEVELS_PER_CABINET + 1):
            shelves_list.append(
                {
                    "level": lvl,
                    "left": shelves[cab][lvl]["left"],
                    "right": shelves[cab][lvl]["right"],
                }
            )
        top_items = [x for x in (top_left if cab == 0 else top_right)]
        cabinets_yaml.append(
            {
                "model": CABINET_MODEL,
                "usable_width_cm": USABLE_SHELF_WIDTH_CM,
                "usable_depth_cm": USABLE_SHELF_DEPTH_CM,
                "levels": LEVELS_PER_CABINET,
                "shelves": shelves_list,
                "top_overflow": top_items,
            }
        )

    used_slots = sum(
        1
        for cab in (0, 1)
        for lvl in range(1, LEVELS_PER_CABINET + 1)
        for side in ("left", "right")
        if shelves[cab][lvl][side] != "EMPTY"
    )

    layout_yaml = {
        "cabinets": cabinets_yaml,
        "summary": {
            "total_internal_slots": NUM_CABINETS * LEVELS_PER_CABINET * 2,
            "used_internal_slots": used_slots,
            "overflow": {
                "items": overflow,
                "rationale": "Overflow 520 first; 5244 if needed; 1310 prefers top. Respect 160 cm width.",
            },
        },
    }

    return layout_yaml, notes


# ---------------- SVG front view ----------------
def render_svg(layout_yaml: Dict, dims: Dict[str, Tuple[float, float, float]]) -> str:
    # Simple schematic drawing; not to scale.
    cab_w = 180
    cab_h = 260
    margin = 20
    shelf_gap = cab_h // (LEVELS_PER_CABINET + 1)
    stroke_base = 'stroke="black" stroke-width="1"'
    text_style = 'font-family="Arial" font-size="10"'

    width = cab_w * 2 + margin * 3
    # Legend height will be computed based on line count; start with a buffer
    legend_line_h = 16
    # Build legend lines first to size canvas appropriately
    legend_lines: List[str] = []
    for code in ("520", "5244", "1310"):
        if code in dims:
            L, W, H = dims[code]
            legend_lines.append(f"{code} — Długość {L:.1f} cm")
            legend_lines.append(f"Szerokość {W:.1f} cm • Wysokość {H:.1f} cm")
            legend_lines.append("")  # spacer line
    if legend_lines and legend_lines[-1] == "":
        legend_lines.pop()

    legend_box_h = max(legend_line_h * (len(legend_lines) + 2), 40)
    height = cab_h + legend_box_h + margin * 2

    def rect(x, y, w, h, label, fill_color):
        return (
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" {stroke_base} fill="{fill_color}"/>'
            f'<text x="{x + w/2}" y="{y + h/2}" {text_style} text-anchor="middle" dominant-baseline="middle">{label}</text>'
        )

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">']

    # Draw two cabinets
    for i in range(2):
        x0 = margin + i * (cab_w + margin)
        y0 = margin
        parts.append(f'<rect x="{x0}" y="{y0}" width="{cab_w}" height="{cab_h}" {stroke_base} fill="white"/>')
        # Shelves
        for lvl in range(LEVELS_PER_CABINET):
            y = y0 + (lvl + 1) * shelf_gap
            parts.append(f'<line x1="{x0}" y1="{y}" x2="{x0 + cab_w}" y2="{y}" {stroke_base} />')
        # Slots
        shelves = layout_yaml["cabinets"][i]["shelves"]
        for idx, shelf in enumerate(shelves):
            y = y0 + idx * shelf_gap + 5
            left_label = shelf["left"]
            right_label = shelf["right"]
            left_fill = COLOR_MAP.get(left_label, "#FFFFFF")
            right_fill = COLOR_MAP.get(right_label, "#FFFFFF")
            parts.append(rect(x0 + 5, y, cab_w/2 - 10, shelf_gap - 10, left_label, left_fill))
            parts.append(rect(x0 + cab_w/2 + 5, y, cab_w/2 - 10, shelf_gap - 10, right_label, right_fill))
        # Top overflow
        top_items = layout_yaml["cabinets"][i]["top_overflow"]
        if top_items:
            ox = x0
            oy = y0 - 15
            for item in top_items:
                parts.append(rect(ox + 5, oy, 40, 12, item, COLOR_MAP.get(item, "#FFFFFF")))
                ox += 45

    # Legend
    lx = margin
    ly = cab_h + margin + 10
    parts.append(f'<rect x="{lx}" y="{ly-12}" width="{width - 2*margin}" height="{legend_box_h}" {stroke_base} fill="white"/>')
    tx = lx + 8
    ty = ly
    # Add color swatches and text for each SKU block (2 lines per SKU)
    idx = 0
    for code in ("520", "5244", "1310"):
        if code in dims:
            fill = COLOR_MAP.get(code, "#FFFFFF")
            # swatch
            parts.append(f'<rect x="{tx}" y="{ty-10}" width="12" height="12" {stroke_base} fill="{fill}"/>')
            # text lines
            L, W, H = dims[code]
            parts.append(f'<text x="{tx + 18}" y="{ty}" {text_style} text-anchor="start">{code} — Długość {L:.1f} cm</text>')
            ty += legend_line_h
            parts.append(f'<text x="{tx + 18}" y="{ty}" {text_style} text-anchor="start">Szerokość {W:.1f} cm • Wysokość {H:.1f} cm</text>')
            ty += legend_line_h
            idx += 1
    
    parts.append("</svg>")
    return "".join(parts)


def render_png(layout_yaml: Dict, dims: Dict[str, Tuple[float, float, float]], out_path: str) -> bool:
    """Attempt to create a PNG front diagram.

    Tries cairosvg first (from the SVG string). If unavailable, draws a
    simplified diagram with Pillow. Returns True on success.
    """
    # Try cairosvg conversion
    try:
        import cairosvg  # type: ignore

        svg_str = render_svg(layout_yaml, dims)
        cairosvg.svg2png(bytestring=svg_str.encode("utf-8"), write_to=out_path)
        return True
    except Exception:
        pass

    # Fallback: draw with Pillow
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore
    except Exception:
        return False

    cab_w = 180
    cab_h = 260
    margin = 20
    shelf_gap = cab_h // (LEVELS_PER_CABINET + 1)
    width = cab_w * 2 + margin * 3
    height = cab_h + 120 + margin * 2

    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    def _measure(txt: str) -> Tuple[int, int]:
        if not font:
            return (0, 0)
        try:
            # Pillow >= 8: use textbbox
            bbox = d.textbbox((0, 0), txt, font=font)
            return (int(bbox[2] - bbox[0]), int(bbox[3] - bbox[1]))
        except Exception:
            try:
                # Fallback legacy
                return d.textsize(txt, font=font)  # type: ignore[attr-defined]
            except Exception:
                return (len(txt) * 6, 10)

    def draw_rect(x, y, w, h, label, fill_color):
        x, y, w, h = int(x), int(y), int(w), int(h)
        d.rectangle([x, y, x + w, y + h], outline="black", fill=fill_color, width=1)
        if label and font:
            tw, th = _measure(label)
            d.text((x + w // 2 - tw // 2, y + h // 2 - th // 2), label, fill="black", font=font)

    # Cabinets and shelves
    for i in range(2):
        x0 = margin + i * (cab_w + margin)
        y0 = margin
        d.rectangle([x0, y0, x0 + cab_w, y0 + cab_h], outline="black", fill="white", width=1)
        for lvl in range(LEVELS_PER_CABINET):
            y = y0 + (lvl + 1) * shelf_gap
            d.line([x0, y, x0 + cab_w, y], fill="black", width=1)
        shelves = layout_yaml["cabinets"][i]["shelves"]
        for idx, shelf in enumerate(shelves):
            y = y0 + idx * shelf_gap + 5
            left_label = shelf["left"]
            right_label = shelf["right"]
            draw_rect(x0 + 5, y, cab_w / 2 - 10, shelf_gap - 10, left_label, COLOR_MAP.get(left_label, "#FFFFFF"))
            draw_rect(x0 + cab_w / 2 + 5, y, cab_w / 2 - 10, shelf_gap - 10, right_label, COLOR_MAP.get(right_label, "#FFFFFF"))
        # Top overflow
        top_items = layout_yaml["cabinets"][i]["top_overflow"]
        if top_items:
            ox = x0
            oy = y0 - 15
            for item in top_items:
                draw_rect(ox + 5, oy, 40, 12, item, COLOR_MAP.get(item, "#FFFFFF"))
                ox += 45

    # Legend
    lx = margin
    ly = cab_h + margin + 10
    legend_lines: List[str] = []
    for code in ("520", "5244", "1310"):
        if code in dims and font:
            L, W, H = dims[code]
            legend_lines.append(f"{code} — Długość {L:.1f} cm")
            legend_lines.append(f"Szerokość {W:.1f} cm • Wysokość {H:.1f} cm")
            legend_lines.append("")
    if legend_lines and legend_lines[-1] == "":
        legend_lines.pop()
    # Determine legend box height based on lines
    line_h = 14
    box_h = max(line_h * (len(legend_lines) + 2), 40)
    d.rectangle([lx, ly - 12, width - margin, ly - 12 + box_h], outline="black", fill="white", width=1)
    tx = lx + 8
    ty = ly
    # Color swatches + text
    for code in ("520", "5244", "1310"):
        if code in dims and font:
            L, W, H = dims[code]
            # swatch
            d.rectangle([tx, ty - 10, tx + 12, ty + 2], outline="black", fill=COLOR_MAP.get(code, "#FFFFFF"), width=1)
            # text lines
            d.text((tx + 18, ty), f"{code} — Długość {L:.1f} cm", fill="black", font=font)
            ty += line_h
            d.text((tx + 18, ty), f"Szerokość {W:.1f} cm • Wysokość {H:.1f} cm", fill="black", font=font)
            ty += line_h

    img.save(out_path)
    return True


# ---------------- Main ----------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Fit Infinity Hearts organizers into 2× BILLY 80×106 cabinets.")
    ap.add_argument("--source", default="purchase-order.md", help="Path to purchase-order.md from sorter")
    ap.add_argument("--output-dir", default="output", help="Directory to write SVG/PNG outputs (default: output)")
    ap.add_argument("--label", default=None, help="Optional label to include in filenames; defaults parsed from --source")
    args = ap.parse_args()

    units, dims, parse_notes = parse_source(args.source)

    # Build Module objects and validate
    per_cap, errors = validate_modules(units, dims)

    # PREPARE VALIDATION TEXT
    lines_valid: List[str] = []
    if parse_notes:
        for n in parse_notes:
            lines_valid.append(f"NOTE: {n}")
    for code, cnt in units.items():
        L, W, H = dims[code]
        cap = per_cap[code]
        lines_valid.append(
            f"{code}: Długość {L:.1f} cm, Szerokość {W:.1f} cm, Wysokość {H:.1f} cm → per_shelf_capacity={cap}"
        )
        if code in ("520", "5244"):
            lines_valid.append(f"  Width check: 2×{L:.1f} ≤ 76.0 → {'OK' if 2*L <= 76.0+1e-6 else 'FAIL'}")
        lines_valid.append(f"  Depth check: {W:.1f} ≤ 26.0 → {'OK' if W <= 26.0+1e-6 else 'FAIL'}")
        if code in ("520", "5244"):
            lines_valid.append("  Height guidance: needs ~21–22 cm (18.9 cm module)")
        if code == "1310" and cnt > 0:
            lines_valid.append("  Height guidance: 24.7 cm requires ~26 cm if placed inside; prefer top placement")

    if errors:
        print("A) VALIDATION\n" + "\n".join(lines_valid) + "\n\nERRORS:\n- " + "\n- ".join(errors))
        return

    # Compute layout
    layout_yaml, layout_notes = compute_layout(units, dims, per_cap)

    # Render YAML
    if yaml is not None:
        layout_yaml_str = yaml.safe_dump(layout_yaml, sort_keys=False)
    else:
        # basic fallback
        layout_yaml_str = str(layout_yaml)

    # Render SVG and write file (timestamped in output dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    # Derive label from source path if not provided
    label = (args.label or "").strip() if args.label else None
    if not label:
        stem = Path(args.source).stem
        m = re.search(r"-purchase-order-(.+)$", stem)
        label = m.group(1) if m else stem
    try:
        import re as _re
        label = _re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-") or "billy"
    except Exception:
        label = "billy"
    svg_path = out_dir / f"{ts}-billy_fitting-{label}.svg"
    png_path = out_dir / f"{ts}-billy_fitting-{label}.png"

    svg_str = render_svg(layout_yaml, dims)
    svg_saved = False
    try:
        svg_path.write_text(svg_str, encoding="utf-8")
        svg_saved = True
    except Exception:
        svg_saved = False

    # Bill of Materials
    bom_lines = [
        f"- Cabinets: {NUM_CABINETS}× {CABINET_MODEL}",
        f"- Extra shelves: +1 per cabinet to reach {LEVELS_PER_CABINET} levels",
    ]
    overflow_items = layout_yaml.get("summary", {}).get("overflow", {}).get("items", [])
    if overflow_items:
        bom_lines.append(f"- Overflow on tops: {', '.join(overflow_items)}")

    # Text summary
    summary_text = (
        "Placed 5244 primarily on middle levels for ergonomics, balanced left/right across two cabinets. "
        "Filled remaining internal slots with 520. Preferred placing any 1310 on cabinet tops to preserve internal capacity; "
        "if top width was insufficient, used full-shelf inside placement. Overflow favors 520 first while respecting the 160 cm total width."
    )

    # Emit all sections
    print("A) VALIDATION")
    print("\n".join(lines_valid))

    print("\nB) LAYOUT (YAML)")
    print(layout_yaml_str.strip())

    print("\nC) FRONT DIAGRAM (SVG)")
    print(svg_str)
    if svg_saved:
        print(f"\n[Saved {svg_path.name} in {out_dir}]")
    else:
        print("\n[Could not write billy_fitting.svg]")
    # Generate PNG file from the diagram
    png_ok = render_png(layout_yaml, dims, str(png_path))
    if png_ok:
        print(f"\n[Saved {png_path.name} in {out_dir}]")
    else:
        print("\n[Could not write billy_fitting.png — install cairosvg or Pillow]")

    print("\nD) BILL OF MATERIALS")
    print("\n".join(bom_lines))

    print("\nE) TEXT SUMMARY")
    print(summary_text)


if __name__ == "__main__":
    main()
