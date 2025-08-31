#!/usr/bin/env python3
"""
LEGO Sorter (Color-sorted, cost-optimized, single-column PDF)

- Uses aggregated_inventory.json from lego_inventory.py
- Fixes parsing bug: mm for Tyre/Wheel, studs→mm for brick/plate/tile.
- If height is missing but L/W known → H = 5 mm (requested).
- If no dimensions at all → conservative fallback volume of 2×4×2 studs.

Packing:
- Drawer types (internal, mm), 80% usable volume:
    SMALL : 133 x 62 x 37
    MED   : 133 x 133 x 37
    DEEP  : 133 x 133 x 80
- Color-pure drawers.
- Promotion heuristic minimizes new drawers required across types.
- Single-column PDF with thumbnails.

CLI:
  python lego_sorter.py --json aggregated_inventory.json
"""

import argparse
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

# ---------- Drawer definitions (mm) ----------
UTIL = 0.80  # 80% usable fill

SMALL_DIMS = (133.0, 62.0, 37.0)
MED_DIMS   = (133.0, 133.0, 37.0)
DEEP_DIMS  = (133.0, 133.0, 80.0)

DRAWER_TYPES = {
    "SMALL": {"dims": SMALL_DIMS},
    "MED":   {"dims": MED_DIMS},
    "DEEP":  {"dims": DEEP_DIMS},
}
def capacity_mm3(dims: Tuple[float, float, float]) -> float:
    L, W, H = dims
    return L * W * H * UTIL
CAPACITY = {k: capacity_mm3(v["dims"]) for k, v in DRAWER_TYPES.items()}

# ---------- Racks (units) ----------
RACKS = {
    "520":  {"drawers": {"SMALL": 20, "MED": 0, "DEEP": 0}, "price_pln": 101.00},
    "5244": {"drawers": {"SMALL": 4,  "MED": 4, "DEEP": 2}, "price_pln": 95.00},
}

# ---------- Fallbacks ----------
STUD_MM = 8.0
BRICK_H_MM = 9.6
DEFAULT_H_IF_MISSING = 5.0  # <— requested assumption
FALLBACK_VOL_EACH = (2 * STUD_MM) * (4 * STUD_MM) * (2 * BRICK_H_MM)  # 9830.4 mm³

# ---------- Parsers ----------
_STUD_RE = re.compile(r'(\d+(?:/\d+)?)\s*[x×]\s*(\d+(?:/\d+)?)\s*(?:[x×]\s*(\d+(?:/\d+)?))?')
_TYRE_WHEEL_MM_RE = re.compile(
    r'(tyre|tire|wheel)[^0-9]*?(\d+(?:\.\d+)?)\s*(?:mm)?\s*[dx×]\s*(\d+(?:\.\d+)?)\s*(?:mm)?',
    re.IGNORECASE
)
def _stud_to_mm(token: str) -> float:
    if '/' in token:
        num, den = token.split('/')
        return (float(num) / float(den)) * STUD_MM
    return float(token) * STUD_MM

def infer_dims_from_name(name: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    n = name.lower().strip()

    mm_match = _TYRE_WHEEL_MM_RE.search(n)
    if mm_match:
        diameter = float(mm_match.group(2))
        width = float(mm_match.group(3))
        L, W, H = diameter, diameter, width
        if H is None:
            H = DEFAULT_H_IF_MISSING
        return L, W, H

    stud_match = _STUD_RE.search(n)
    if stud_match:
        a, b, c = stud_match.group(1), stud_match.group(2), stud_match.group(3)
        L = _stud_to_mm(a)
        W = _stud_to_mm(b)
        H = None
        if 'tile' in n or 'plate' in n:
            H = 3.2
        elif 'brick' in n and not c:
            H = 9.6
        elif c:
            if '/' in c:
                num, den = c.split('/')
                H = (float(num) / float(den)) * BRICK_H_MM
            else:
                H = float(c) * BRICK_H_MM
        if H is None:
            H = DEFAULT_H_IF_MISSING
        return L, W, H

    return (None, None, None)

# ---------- Models ----------
@dataclass
class Part:
    part_id: str
    name: str
    color: str
    color_id: int
    qty: int
    l: Optional[float]
    w: Optional[float]
    h: Optional[float]
    vol_each: Optional[float]
    image_file: str = ""

    def fits_conservative(self, drawer_dims: Tuple[float, float, float]) -> bool:
        """
        Conservative fit with partial dimensions:
        - If no dims known → allow (volume will govern).
        - If any known dim exceeds largest side → cannot fit.
        - If >=2 known dims → two largest must fit into two largest sides.
        """
        known = [x for x in (self.l, self.w, self.h) if x is not None]
        if not known:
            return True
        box = sorted(drawer_dims)
        if max(known) > box[-1]:
            return False
        if len(known) >= 2:
            k2 = sorted(known)[-2:]
            if k2[0] > box[-2] or k2[1] > box[-1]:
                return False
        return True

@dataclass
class Drawer:
    kind: str        # "SMALL" | "MED" | "DEEP"
    color: str       # color bucket
    capacity: float
    used: float = 0.0
    items: List[Dict] = field(default_factory=list)

    @property
    def remaining(self) -> float:
        return self.capacity - self.used

    def place(self, part: Part, pieces: int):
        vol = (part.vol_each or 0.0) * pieces
        self.items.append({
            "Part ID": part.part_id,
            "Part Name": part.name,
            "Color": part.color,
            "Color ID": part.color_id,
            "Qty": pieces,
            "Image File": part.image_file
        })
        self.used += vol

# ---------- Packing helpers ----------
def max_fit_by_volume(rem: float, vol_each: float) -> int:
    if not vol_each or vol_each <= 0:
        return 0
    return int(rem // vol_each)

def pieces_per_new_drawer(kind: str, vol_each: float) -> int:
    if not vol_each or vol_each <= 0:
        return 0
    return int(CAPACITY[kind] // vol_each)

# ---------- Packing (color-first, multi-type) ----------
def pack_color_bucket(parts: List[Part], color: str) -> Dict[str, List[Drawer]]:
    drawers: Dict[str, List[Drawer]] = {k: [] for k in DRAWER_TYPES.keys()}

    # Largest first by per-piece volume
    parts_sorted = sorted(parts, key=lambda p: (p.vol_each or FALLBACK_VOL_EACH), reverse=True)

    for p in parts_sorted:
        qty_left = p.qty
        fits_type = {k: p.fits_conservative(DRAWER_TYPES[k]["dims"]) for k in DRAWER_TYPES}

        while qty_left > 0:
            # Compute per-drawer capacity for feasible types
            candidates = []
            for kind in ("SMALL", "MED", "DEEP"):
                if not fits_type[kind]:
                    continue
                per_draw = max(pieces_per_new_drawer(kind, p.vol_each or FALLBACK_VOL_EACH), 1)
                # predicted new drawers if we opened only this type
                need = math.ceil(qty_left / per_draw)
                # secondary tie-break: higher per-drawer fill ratio
                fill_ratio = ((per_draw * (p.vol_each or FALLBACK_VOL_EACH)) / CAPACITY[kind])
                # tertiary tie-break: SMALL < MED < DEEP
                tie = {"SMALL": 0, "MED": 1, "DEEP": 2}[kind]
                candidates.append((need, -fill_ratio, tie, kind, per_draw))

            # If nothing fits conservatively, fall back to using a DEEP drawer anyway
            if not candidates:
                per_draw = max(pieces_per_new_drawer("DEEP", p.vol_each or FALLBACK_VOL_EACH), 1)
                candidates.append(
                    (
                        math.ceil(qty_left / per_draw),
                        -((per_draw * (p.vol_each or FALLBACK_VOL_EACH)) / CAPACITY["DEEP"]),
                        2,  # tie-breaker value for DEEP
                        "DEEP",
                        per_draw,
                    )
                )

            need, _, _, best_kind, per_draw = min(candidates, key=lambda x: (x[0], x[1], x[2]))

            # Try existing drawers (same color + kind)
            placed_some = False
            for dr in drawers[best_kind]:
                if dr.color != color:
                    continue
                cap = max_fit_by_volume(dr.remaining, p.vol_each or FALLBACK_VOL_EACH)
                if cap > 0:
                    k = min(qty_left, cap)
                    dr.place(p, k)
                    qty_left -= k
                    placed_some = True
                    if qty_left == 0:
                        break

            if qty_left == 0:
                break

            # Open a new drawer of chosen type
            new_dr = Drawer(kind=best_kind, color=color, capacity=CAPACITY[best_kind])
            drawers[best_kind].append(new_dr)
            k = min(qty_left, per_draw)
            new_dr.place(p, k)
            qty_left -= k

    return drawers

def pack_all(parts: List[Part]) -> Dict[str, Dict[str, List[Drawer]]]:
    # Ensure volume and conservative H=5mm where needed
    for p in parts:
        # If missing any dims, try to infer from name
        if any(v is None for v in (p.l, p.w, p.h)):
            Li, Wi, Hi = infer_dims_from_name(p.name)
            if p.l is None: p.l = Li
            if p.w is None: p.w = Wi
            if p.h is None: p.h = Hi

        # If we have L/W but H still missing → assume 5 mm
        if (p.l is not None or p.w is not None) and p.h is None:
            p.h = DEFAULT_H_IF_MISSING

        # Ensure per-piece volume
        if p.vol_each is None:
            if all(v is not None for v in (p.l, p.w, p.h)):
                p.vol_each = p.l * p.w * p.h
            else:
                p.vol_each = FALLBACK_VOL_EACH

    # Group by color
    by_color: Dict[str, List[Part]] = {}
    for p in parts:
        by_color.setdefault(p.color, []).append(p)

    # Pack colors in descending total volume
    color_order = sorted(by_color.keys(),
                         key=lambda c: sum((pp.vol_each or FALLBACK_VOL_EACH) * pp.qty for pp in by_color[c]),
                         reverse=True)

    packed: Dict[str, Dict[str, List[Drawer]]] = {}
    for color in color_order:
        packed[color] = pack_color_bucket(by_color[color], color)

    return packed

# ---------- Cost optimization ----------
def count_drawers(packed: Dict[str, Dict[str, List[Drawer]]]) -> Dict[str, int]:
    totals = {"SMALL": 0, "MED": 0, "DEEP": 0}
    for _, by_type in packed.items():
        for kind in totals:
            totals[kind] += len(by_type.get(kind, []))
    return totals

def optimize_units(drawers_needed: Dict[str, int]) -> Dict[str, int]:
    need_s = drawers_needed.get("SMALL", 0)
    need_m = drawers_needed.get("MED", 0)
    need_d = drawers_needed.get("DEEP", 0)

    price_s = RACKS["520"]["price_pln"]
    price_l = RACKS["5244"]["price_pln"]

    # 5244 must cover MED & DEEP
    min_xl = max(math.ceil(need_m / RACKS["5244"]["drawers"]["MED"]),
                 math.ceil(need_d / RACKS["5244"]["drawers"]["DEEP"]))
    best = {"520": 0, "5244": 0, "cost": float("inf")}
    # allow some slack for exploring coverage of SMALL via 5244
    max_xl = max(min_xl, math.ceil(need_s / 4) + 10)

    for xl in range(min_xl, max_xl + 1):
        covered_small = RACKS["5244"]["drawers"]["SMALL"] * xl
        rem_small = max(0, need_s - covered_small)
        xs = math.ceil(rem_small / RACKS["520"]["drawers"]["SMALL"]) if rem_small > 0 else 0
        cost = xl * price_l + xs * price_s
        if cost < best["cost"]:
            best = {"520": xs, "5244": xl, "cost": cost}

    return best

# ---------- Exports ----------
def export_purchase_order(solution: Dict[str, int], totals: Dict[str, int], path="purchase-order.md"):
    xs = solution.get("520", 0)
    xl = solution.get("5244", 0)
    cost = solution.get("cost", 0.0)

    with open(path, "w", encoding="utf-8") as f:
        f.write("# LEGO Storage Purchase Order\n\n")
        f.write("## Drawer usage\n")
        f.write(f"- SMALL drawers used: **{totals['SMALL']}**\n")
        f.write(f"- MED drawers used: **{totals['MED']}**\n")
        f.write(f"- DEEP drawers used: **{totals['DEEP']}**\n\n")

        f.write("## Units to purchase\n")
        f.write(f"- 520 (20× SMALL): **{xs}**\n")
        f.write(f"- 5244 (4× SMALL, 4× MED, 2× DEEP): **{xl}**\n\n")

        f.write("## Costs (PLN)\n")
        f.write(f"- 520: {xs} × {RACKS['520']['price_pln']:.2f} PLN\n")
        f.write(f"- 5244: {xl} × {RACKS['5244']['price_pln']:.2f} PLN\n")
        f.write(f"- **Total: {cost:.2f} PLN**\n")

def export_plan_md(packed: Dict[str, Dict[str, List[Drawer]]], path="container_plan.md"):
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Container Plan (Color-sorted)\n\n")
        for color, by_type in packed.items():
            f.write(f"## {color}\n\n")
            for kind_label in ("SMALL", "MED", "DEEP"):
                drawers = by_type.get(kind_label, [])
                if not drawers:
                    continue
                f.write(f"### {kind_label} drawers ({len(drawers)})\n")
                for i, dr in enumerate(drawers, start=1):
                    f.write(f"#### Drawer {i}\n")
                    for item in dr.items:
                        f.write(f"- {item['Part ID']} | {item['Part Name']} | Qty: {item['Qty']}\n")
                    f.write("\n")

def export_plan_pdf(packed: Dict[str, Dict[str, List[Drawer]]], path="container_plan.pdf"):
    c = canvas.Canvas(path, pagesize=A4)
    page_w, page_h = A4
    margin = 36
    y = page_h - margin

    def new_page():
        nonlocal y
        c.showPage()
        y = page_h - margin

    def need_space(lines: int) -> bool:
        return y < margin + lines * 14 + 40

    def h1(txt: str):
        nonlocal y
        if need_space(2): new_page()
        c.setFont("Helvetica-Bold", 16); c.drawString(margin, y, txt); y -= 20

    def h2(txt: str):
        nonlocal y
        if need_space(2): new_page()
        c.setFont("Helvetica-Bold", 13); c.drawString(margin, y, txt); y -= 16

    def h3(txt: str):
        nonlocal y
        if need_space(2): new_page()
        c.setFont("Helvetica-Bold", 11); c.drawString(margin, y, txt); y -= 13

    def item_line(item):
        nonlocal y
        if need_space(3): new_page()
        img = item.get("Image File") or ""
        if img and Path(img).exists():
            try:
                c.drawImage(ImageReader(img), margin, y-22, width=20, height=20, preserveAspectRatio=True, mask="auto")
            except Exception:
                pass
            c.setFont("Helvetica", 10)
            c.drawString(margin + 24, y - 6, f"{item['Part Name']} x{item['Qty']}")
            c.setFont("Helvetica-Oblique", 8)
            c.drawString(margin + 24, y - 18, f"{item['Part ID']}")
            y -= 26
        else:
            c.setFont("Helvetica", 10)
            c.drawString(margin, y, f"- {item['Part Name']} x{item['Qty']} ({item['Part ID']})")
            y -= 12

    h1("LEGO Container Plan (Color-sorted)")

    for color, by_type in packed.items():
        h2(color)
        for kind_label in ("SMALL", "MED", "DEEP"):
            drawers = by_type.get(kind_label, [])
            if not drawers:
                continue
            h3(f"{kind_label} drawers ({len(drawers)})")
            for i, dr in enumerate(drawers, start=1):
                h3(f"Drawer {i}")
                for item in dr.items:
                    item_line(item)

    c.save()

# ---------- JSON I/O ----------
@dataclass
class _RawPart:
    part_id: str
    part_name: str
    color: str
    color_id: int
    quantity: int
    length_mm: Optional[float]
    width_mm: Optional[float]
    height_mm: Optional[float]
    volume_each_mm3: Optional[float]
    image_file: str = ""

def load_parts(path: str) -> List[Part]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out: List[Part] = []
    for r in raw.get("parts", []):
        rp = _RawPart(
            part_id=str(r["part_id"]),
            part_name=str(r["part_name"]),
            color=str(r["color"]),
            color_id=int(r["color_id"]),
            quantity=int(r["quantity"]),
            length_mm=r.get("length_mm"),
            width_mm=r.get("width_mm"),
            height_mm=r.get("height_mm"),
            volume_each_mm3=r.get("volume_each_mm3"),
            image_file=r.get("image_file", "")
        )

        # Start with provided dims
        L, W, H = rp.length_mm, rp.width_mm, rp.height_mm

        # If any missing, try to infer from name
        if L is None or W is None or H is None:
            Li, Wi, Hi = infer_dims_from_name(rp.part_name)
            if L is None: L = Li
            if W is None: W = Wi
            if H is None: H = Hi

        # If we have L/W but H still missing → assume 5 mm
        if (L is not None or W is not None) and H is None:
            H = DEFAULT_H_IF_MISSING

        # Compute volume
        vol_each = rp.volume_each_mm3
        if vol_each is None:
            if all(v is not None for v in (L, W, H)):
                vol_each = L * W * H
            else:
                vol_each = FALLBACK_VOL_EACH

        out.append(Part(
            part_id=rp.part_id,
            name=rp.part_name,
            color=rp.color,
            color_id=rp.color_id,
            qty=rp.quantity,
            l=L, w=W, h=H,
            vol_each=vol_each,
            image_file=rp.image_file
        ))
    return out

# ---------- Main ----------
def main():
    ap = argparse.ArgumentParser(description="LEGO sorter (color-sorted, cost-optimized).")
    ap.add_argument("--json", default="aggregated_inventory.json", help="Path to aggregated inventory JSON")
    args = ap.parse_args()

    if not Path(args.json).exists():
        raise FileNotFoundError(f"{args.json} not found. Run lego_inventory.py first.")

    parts = load_parts(args.json)
    packed = pack_all(parts)

    totals = count_drawers(packed)
    solution = optimize_units(totals)

    export_purchase_order(solution, totals)
    export_plan_md(packed)
    export_plan_pdf(packed)

    print(f"✅ Packed by color. Drawers used → SMALL: {totals['SMALL']}, MED: {totals['MED']}, DEEP: {totals['DEEP']}")
    print(f"🧮 Units → 520: {solution['520']}, 5244: {solution['5244']}  (Total {solution['cost']:.2f} PLN)")

if __name__ == "__main__":
    main()
