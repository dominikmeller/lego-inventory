#!/usr/bin/env python3
"""
LEGO Sorter (Color-sorted, cost-optimized, single-column PDF)

- Uses aggregated_inventory.json from lego_inventory.py
- Robust parser (same as inventory) + dimension defaults:
    * If ALL dims unknown → fallback = 2×4×1 studs → 16×32×9.6 mm.
    * If SOME dims known → fill missing independently: L=30, W=10, H=10 mm.
- Color-pure drawers; promotion heuristic minimizes new drawers.

Drawer types (internal, mm), 80% usable volume:
    SMALL : 133 x 62 x 37
    MED   : 133 x 133 x 37
    DEEP  : 133 x 133 x 80

Outputs:
- purchase-order.md
- container_plan.md
- container_plan.pdf (single column with thumbnails)

Progress Reporting (script-level)
---------------------------------
- Flags: `--quiet`, `--verbose`, `--progress-json <path>`.
- Phases: load JSON → pack → optimize → export; emits final summary.
"""

import argparse
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import sys
import time


# ---------- Progress utils (lightweight) ----------
@dataclass
class Step:
    name: str
    status: str = "pending"
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    items_total: Optional[int] = None
    items_done: int = 0


class ProgressReporter:
    def __init__(self, script: str, quiet: bool = False, verbose: bool = False, json_path: Optional[str] = None):
        self.script = script
        self.quiet = quiet
        self.verbose = verbose
        self.json_path = json_path
        self.t0 = time.time()
        self.steps: List[Step] = []
        self._isatty = sys.stdout.isatty()

    def start(self, name: str, total: Optional[int] = None) -> Step:
        st = Step(name=name, status="in_progress", started_at=time.time(), items_total=total)
        self.steps.append(st)
        if self.verbose and not self.quiet:
            print(f"→ {name}…")
        return st

    def update(self, st: Step, done: Optional[int] = None, total: Optional[int] = None) -> None:
        if done is not None:
            st.items_done = done
        if total is not None:
            st.items_total = total
        if not self.quiet:
            frac = ""
            if st.items_total:
                pct = int(100 * (st.items_done / max(1, st.items_total)))
                frac = f" {st.items_done}/{st.items_total} ({pct}%)"
            line = f"{st.name}:{frac}  elapsed {int(time.time()-st.started_at)}s"
            end = "\r" if self._isatty and not self.verbose else "\n"
            print(line, end=end, flush=True)

    def end(self, st: Step, status: str = "completed") -> None:
        st.status = status
        st.ended_at = time.time()
        if not self.quiet:
            elapsed = int((st.ended_at - (st.started_at or st.ended_at)))
            print(f"✓ {st.name} in {elapsed}s")

    def finalize(self, totals: Dict[str, int]) -> None:
        elapsed = int(time.time() - self.t0)
        if not self.quiet:
            print(
                f"[sorter] Summary: unique_items={totals.get('unique_items', 0)}, "
                f"pieces_total={totals.get('pieces_total', 0)}, colors={totals.get('colors', 0)}, "
                f"files={totals.get('outputs', 0)} | elapsed={elapsed}s"
            )
        if self.json_path:
            payload = {
                "script": self.script,
                "started_at": self.t0,
                "ended_at": time.time(),
                "elapsed_s": elapsed,
                "steps": [
                    {
                        "name": s.name,
                        "status": s.status,
                        "started_at": s.started_at,
                        "ended_at": s.ended_at,
                        "items_total": s.items_total,
                        "items_done": s.items_done,
                    }
                    for s in self.steps
                ],
                "totals": totals,
            }
            Path(self.json_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")

# ---------- Drawer definitions (mm) ----------
UTIL = 0.80  # 80% usable fill

SMALL_DIMS = (133.0, 62.0, 37.0)
MED_DIMS = (133.0, 133.0, 37.0)
DEEP_DIMS = (133.0, 133.0, 80.0)

DRAWER_TYPES = {
    "SMALL": {"dims": SMALL_DIMS},
    "MED": {"dims": MED_DIMS},
    "DEEP": {"dims": DEEP_DIMS},
}


def capacity_mm3(dims: Tuple[float, float, float]) -> float:
    L, W, H = dims
    return L * W * H * UTIL


CAPACITY = {k: capacity_mm3(v["dims"]) for k, v in DRAWER_TYPES.items()}

# ---------- Racks (units) ----------
RACKS = {
    "520": {"drawers": {"SMALL": 20, "MED": 0, "DEEP": 0}, "price_pln": 101.00},
    "5244": {"drawers": {"SMALL": 4, "MED": 4, "DEEP": 2}, "price_pln": 95.00},
}

# ---------- Defaults & Parsers ----------
STUD_MM = 8.0
PLATE_H_MM = 3.2
BRICK_H_MM = 9.6

DEFAULT_L_IF_MISSING = 30.0  # "depth"
DEFAULT_W_IF_MISSING = 10.0
DEFAULT_H_IF_MISSING = 10.0

FALLBACK_STUDS_DIMS = (2 * STUD_MM, 4 * STUD_MM, 1 * BRICK_H_MM)  # (16, 32, 9.6) mm
FALLBACK_VOL_EACH = (
    FALLBACK_STUDS_DIMS[0] * FALLBACK_STUDS_DIMS[1] * FALLBACK_STUDS_DIMS[2]
)  # 4915.2 mm³

_STUD_RE = re.compile(
    r"(\d+(?:/\d+)?)\s*[x×]\s*(\d+(?:/\d+)?)\s*(?:[x×]\s*(\d+(?:/\d+)?))?"
)
_TYRE_WHEEL_MM_RE = re.compile(
    r"(tyre|tire|wheel)[^0-9]*?(\d+(?:\.\d+)?)\s*(?:mm)?\s*[dx×]\s*(\d+(?:\.\d+)?)\s*(?:mm)?",
    re.IGNORECASE,
)


def _stud_to_mm(token: str) -> float:
    if "/" in token:
        num, den = token.split("/")
        return (float(num) / float(den)) * STUD_MM
    return float(token) * STUD_MM


def infer_dims_from_name(
    name: str,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    n = name.lower().strip()

    mm_match = _TYRE_WHEEL_MM_RE.search(n)
    if mm_match:
        diameter = float(mm_match.group(2))
        width = float(mm_match.group(3))
        return diameter, diameter, width

    stud_match = _STUD_RE.search(n)
    if stud_match:
        a, b, c = stud_match.group(1), stud_match.group(2), stud_match.group(3)
        L = _stud_to_mm(a)
        W = _stud_to_mm(b)
        H = None
        if "tile" in n or "plate" in n:
            H = PLATE_H_MM
        elif "brick" in n and not c:
            H = BRICK_H_MM
        elif c:
            if "/" in c:
                num, den = c.split("/")
                H = (float(num) / float(den)) * BRICK_H_MM
            else:
                H = float(c) * BRICK_H_MM
        return L, W, H

    return (None, None, None)


def fill_dims_with_defaults_or_studs(L, W, H):
    """
    - If ALL are None -> use studs fallback (2×4×1 studs) = 16×32×9.6 mm.
    - Else, fill missing ones independently with defaults: L=30, W=10, H=10 mm.
    """
    if L is None and W is None and H is None:
        return FALLBACK_STUDS_DIMS
    if L is None:
        L = DEFAULT_L_IF_MISSING
    if W is None:
        W = DEFAULT_W_IF_MISSING
    if H is None:
        H = DEFAULT_H_IF_MISSING
    return L, W, H


# ---------- Models ----------
@dataclass
class Part:
    part_id: str
    name: str
    color: str
    color_id: int
    qty: int
    l: Optional[float]  # noqa: E741 - keep short names for dimensions
    w: Optional[float]
    h: Optional[float]
    vol_each: Optional[float]
    image_file: str = ""

    def fits_conservative(self, drawer_dims: Tuple[float, float, float]) -> bool:
        """Axis-aligned fit using sorted comparison for known dimensions.

        - If 1 dim known: must be <= max axis.
        - If 2 known: compare top-two of known vs top-two of box.
        - If 3 known: compare all three sorted dims elementwise.
        """
        known = [x for x in (self.l, self.w, self.h) if x is not None]
        if not known:
            return True
        box = sorted(drawer_dims)
        ks = sorted(known)
        if len(ks) == 1:
            return ks[-1] <= box[-1]
        if len(ks) == 2:
            return ks[-2] <= box[-2] and ks[-1] <= box[-1]
        return all(a <= b for a, b in zip(ks, box))


@dataclass
class Drawer:
    kind: str  # "SMALL" | "MED" | "DEEP"
    color: str  # color bucket
    capacity: float
    used: float = 0.0
    items: List[Dict] = field(default_factory=list)

    @property
    def remaining(self) -> float:
        return self.capacity - self.used

    def place(self, part: Part, pieces: int):
        vol = (part.vol_each or 0.0) * pieces
        self.items.append(
            {
                "Part ID": part.part_id,
                "Part Name": part.name,
                "Color": part.color,
                "Color ID": part.color_id,
                "Qty": pieces,
                "Image File": part.image_file,
            }
        )
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
def pack_color_bucket(parts: List[Part], color: str, strategy: str = "greedy") -> Dict[str, List[Drawer]]:
    drawers: Dict[str, List[Drawer]] = {k: [] for k in DRAWER_TYPES.keys()}

    # Ensure dims + defaults + volumes
    for p in parts:
        if any(v is None for v in (p.l, p.w, p.h)):
            Li, Wi, Hi = infer_dims_from_name(p.name)
            p.l = p.l if p.l is not None else Li
            p.w = p.w if p.w is not None else Wi
            p.h = p.h if p.h is not None else Hi
        p.l, p.w, p.h = fill_dims_with_defaults_or_studs(p.l, p.w, p.h)
        p.vol_each = p.vol_each if p.vol_each is not None else (p.l * p.w * p.h)
        try:
            if p.vol_each is None or p.vol_each <= 0 or math.isnan(p.vol_each):
                p.vol_each = FALLBACK_VOL_EACH
        except Exception:
            p.vol_each = FALLBACK_VOL_EACH

    # Largest first by per-piece volume
    parts_sorted = sorted(
        parts,
        key=lambda p: (-(p.vol_each or 0.0), str(p.part_id), str(p.name)),
    )

    for p in parts_sorted:
        qty_left = p.qty
        fits_type = {
            k: p.fits_conservative(DRAWER_TYPES[k]["dims"]) for k in DRAWER_TYPES
        }

        while qty_left > 0:
            # prefer smallest feasible drawer that minimizes new drawers
            candidates = []
            for kind in ("SMALL", "MED", "DEEP"):
                if not fits_type[kind]:
                    continue
                per_draw = max(pieces_per_new_drawer(kind, p.vol_each), 1)
                need = math.ceil(qty_left / per_draw)
                tie = {"SMALL": 0, "MED": 1, "DEEP": 2}[
                    kind
                ]  # prefer smaller drawer on ties
                fill_ratio = min(1.0, (per_draw * p.vol_each) / CAPACITY[kind])
                candidates.append((need, tie, kind, per_draw, fill_ratio))

            if not candidates:
                print(
                    f"⚠️ Part {p.part_id} ({p.name}) does not fit any drawer; "
                    f"skipping {qty_left} pcs."
                )
                qty_left = 0
                break

            if strategy == "balanced":
                need, _, best_kind, per_draw, _ = min(
                    candidates, key=lambda x: (x[0], -x[4], x[1])
                )
            else:
                need, _, best_kind, per_draw, _ = min(candidates, key=lambda x: (x[0], x[1]))

            # Try existing drawers
            kinds_scan = ("SMALL", "MED", "DEEP") if strategy == "balanced" else (best_kind,)
            placed = False
            for kind_try in kinds_scan:
                for dr in drawers[kind_try]:
                    if dr.color != color:
                        continue
                    cap = max_fit_by_volume(dr.remaining, p.vol_each)
                    if cap > 0:
                        k = min(qty_left, cap)
                        dr.place(p, k)
                        qty_left -= k
                        placed = True
                        if qty_left == 0:
                            break
                if qty_left == 0:
                    break
            if qty_left == 0:
                break
            if placed:
                continue

            # Open a new drawer of chosen type
            new_dr = Drawer(kind=best_kind, color=color, capacity=CAPACITY[best_kind])
            drawers[best_kind].append(new_dr)
            k = min(qty_left, per_draw)
            new_dr.place(p, k)
            qty_left -= k

    return drawers


def pack_all(parts: List[Part], strategy: str = "greedy") -> Dict[str, Dict[str, List[Drawer]]]:
    # Group by color
    by_color: Dict[str, List[Part]] = {}
    for p in parts:
        by_color.setdefault(p.color, []).append(p)

    # Pack colors in descending total volume
    color_order = sorted(
        by_color.keys(),
        key=lambda c: (
            sum(
                (pp.vol_each if pp.vol_each is not None else FALLBACK_VOL_EACH) * pp.qty
                for pp in by_color[c]
            ),
            c.lower(),
        ),
        reverse=True,
    )

    packed: Dict[str, Dict[str, List[Drawer]]] = {}
    for color in color_order:
        packed[color] = pack_color_bucket(by_color[color], color, strategy=strategy)

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
    min_xl = max(
        math.ceil(need_m / RACKS["5244"]["drawers"]["MED"]),
        math.ceil(need_d / RACKS["5244"]["drawers"]["DEEP"]),
    )
    best = {"520": 0, "5244": 0, "cost": float("inf")}
    max_xl = max(min_xl, math.ceil(need_s / 4))

    for xl in range(min_xl, max_xl + 1):
        covered_small = RACKS["5244"]["drawers"]["SMALL"] * xl
        rem_small = max(0, need_s - covered_small)
        xs = (
            math.ceil(rem_small / RACKS["520"]["drawers"]["SMALL"])
            if rem_small > 0
            else 0
        )
        cost = xl * price_l + xs * price_s
        if cost < best["cost"]:
            best = {"520": xs, "5244": xl, "cost": cost}

    return best


# ---------- Exports ----------
def export_purchase_order(
    solution: Dict[str, int], totals: Dict[str, int], path="purchase-order.md"
):
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

        f.write("### Purchase Links\n")
        f.write("- 520 product page: https://rito.pl/szufladki-system-z-szufladami-organizer/35572-infinity-hearts-system-szuflad-organizer-regal-z-szufladami-plastik-520-20-szuflad-378x154x189cm-5713410019740.html\n")
        f.write("- 5244 product page: https://rito.pl/szufladki-system-z-szufladami-organizer/35574-infinity-hearts-system-szuflad-organizer-regal-z-szufladami-plastik-5244-10-szuflad-378x154x189cm-5713410019764.html\n\n")

        f.write("## Costs (PLN)\n")
        f.write(f"- 520: {xs} × {RACKS['520']['price_pln']:.2f} PLN\n")
        f.write(f"- 5244: {xl} × {RACKS['5244']['price_pln']:.2f} PLN\n")
        f.write(f"- **Total: {cost:.2f} PLN**\n")

        f.write("\n## Links\n")
        f.write("- Container Plan (Markdown): [container_plan.md](container_plan.md)\n")
        f.write("- Container Plan (PDF): [container_plan.pdf](container_plan.pdf)\n")
        f.write("- Inventory (Excel): [lego_inventory.xlsx](lego_inventory.xlsx)\n")
        f.write("- Inventory (Markdown): [lego_inventory.md](lego_inventory.md)\n")
        f.write("- Aggregated Inventory JSON: [aggregated_inventory.json](aggregated_inventory.json)\n")


def export_plan_md(
    packed: Dict[str, Dict[str, List[Drawer]]], path="container_plan.md"
):
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
                    for item in sorted(dr.items, key=lambda it: (str(it.get('Part ID','')), str(it.get('Part Name','')))):
                        f.write(
                            f"- {item['Part ID']} | {item['Part Name']} | Qty: {item['Qty']}\n"
                        )
                    f.write("\n")


def export_plan_pdf(
    packed: Dict[str, Dict[str, List[Drawer]]], path="container_plan.pdf"
):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import ImageReader
    except Exception:
        print("⚠️ ReportLab not available; skipping PDF export.")
        return

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
        if need_space(2):
            new_page()
        c.setFont("Helvetica-Bold", 16)
        c.drawString(margin, y, txt)
        y -= 20

    def h2(txt: str):
        nonlocal y
        if need_space(2):
            new_page()
        c.setFont("Helvetica-Bold", 13)
        c.drawString(margin, y, txt)
        y -= 16

    def h3(txt: str):
        nonlocal y
        if need_space(2):
            new_page()
        c.setFont("Helvetica-Bold", 11)
        c.drawString(margin, y, txt)
        y -= 13

    def item_line(item):
        nonlocal y
        if need_space(3):
            new_page()
        img = item.get("Image File") or ""
        if img and Path(img).exists():
            try:
                c.drawImage(
                    ImageReader(img),
                    margin,
                    y - 22,
                    width=20,
                    height=20,
                    preserveAspectRatio=True,
                    mask="auto",
                )
            except Exception:
                pass
            c.setFont("Helvetica", 10)
            c.drawString(margin + 24, y - 6, f"{item['Part Name']} x{item['Qty']}")
            c.setFont("Helvetica-Oblique", 8)
            c.drawString(margin + 24, y - 18, f"{item['Part ID']}")
            y -= 26
        else:
            c.setFont("Helvetica", 10)
            c.drawString(
                margin, y, f"- {item['Part Name']} x{item['Qty']} ({item['Part ID']})"
            )
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
                for item in sorted(dr.items, key=lambda it: (str(it.get('Part ID','')), str(it.get('Part Name','')))):
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
        L, W, H = r.get("length_mm"), r.get("width_mm"), r.get("height_mm")

        # If any missing, try to infer from name then apply defaults/studs
        if L is None or W is None or H is None:
            Li, Wi, Hi = infer_dims_from_name(r.get("part_name", ""))
            L = L if L is not None else Li
            W = W if W is not None else Wi
            H = H if H is not None else Hi
        L, W, H = fill_dims_with_defaults_or_studs(L, W, H)

        vol_each = r.get("volume_each_mm3")
        if vol_each is None:
            vol_each = L * W * H

        out.append(
            Part(
                part_id=str(r["part_id"]),
                name=str(r["part_name"]),
                color=str(r["color"]),
                color_id=int(r["color_id"]),
                qty=int(r["quantity"]),
                l=L,
                w=W,
                h=H,
                vol_each=vol_each,
                image_file=r.get("image_file", ""),
            )
        )
    return out


# ---------- Main ----------
def main():
    ap = argparse.ArgumentParser(description="LEGO sorter (color-sorted, cost-optimized) with progress reporting")
    ap.add_argument(
        "--json",
        default="aggregated_inventory.json",
        help="Path to aggregated inventory JSON",
    )
    ap.add_argument("--progress-json", default=None, help="Write progress JSON to this path")
    ap.add_argument("--quiet", action="store_true", help="Only print final summary")
    ap.add_argument("--verbose", action="store_true", help="Print step-by-step logs")
    ap.add_argument("--pack-strategy", choices=["greedy", "balanced"], default="greedy", help="Packing heuristic to use")
    ap.add_argument("--no-pdf", action="store_true", help="Skip PDF export")
    ap.add_argument("--no-md", action="store_true", help="Skip Markdown export")
    ap.add_argument("--purchase-only", action="store_true", help="Only output purchase-order.md")
    args = ap.parse_args()

    prog = ProgressReporter(script="sorter", quiet=args.quiet, verbose=args.verbose, json_path=args.progress_json)

    if not Path(args.json).exists():
        raise FileNotFoundError(f"{args.json} not found. Run lego_inventory.py first.")

    st_load = prog.start("Load JSON")
    parts = load_parts(args.json)
    prog.end(st_load)

    st_pack = prog.start("Pack parts", total=len(parts))
    packed = pack_all(parts, strategy=args.pack_strategy)
    prog.end(st_pack)

    st_opt = prog.start("Optimize units")
    totals = count_drawers(packed)
    solution = optimize_units(totals)
    prog.end(st_opt)

    st_export = prog.start("Export files")
    export_purchase_order(solution, totals)
    if not args.purchase_only and not args.no_md:
        export_plan_md(packed)
    if not args.purchase_only and not args.no_pdf:
        export_plan_pdf(packed)
    prog.end(st_export)

    if not args.quiet:
        print(
            f"✅ Packed by color. Drawers used → SMALL: {totals['SMALL']}, MED: {totals['MED']}, DEEP: {totals['DEEP']}"
        )
        print(
            f"🧮 Units → 520: {solution['520']}, 5244: {solution['5244']}  (Total {solution['cost']:.2f} PLN)"
        )

    prog.finalize(totals={
        # Unique aggregated items from JSON
        "unique_items": len(parts),
        # Total pieces in all items
        "pieces_total": sum(p.qty for p in parts),
        "colors": len({p.color for p in parts}),
        "outputs": 3,
    })


if __name__ == "__main__":
    main()
