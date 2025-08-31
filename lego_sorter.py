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
import csv
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import sys
import time
try:
    import yaml  # type: ignore
except Exception:
    yaml = None


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

# Accurate 1310 unit drawer dimensions (mm) from product page
S1310_DIMS = (160.0, 86.0, 39.0)     # 16.0 x 8.6 x 3.9 cm, 10 pcs
L1310_DIMS = (223.0, 160.0, 39.0)    # 22.3 x 16.0 x 3.9 cm, 3 pcs
L1310_DEEP_DIMS = (223.0, 160.0, 85.0)  # 22.3 x 16.0 x 8.5 cm, 1 pc

DRAWER_TYPES = {
    "SMALL": {"dims": SMALL_DIMS},
    "MED": {"dims": MED_DIMS},
    "DEEP": {"dims": DEEP_DIMS},
    # 1310 variants are enabled optionally in main()
}


def capacity_mm3(dims: Tuple[float, float, float]) -> float:
    L, W, H = dims
    return L * W * H * UTIL


CAPACITY = {k: capacity_mm3(v["dims"]) for k, v in DRAWER_TYPES.items()}

# ---------- Racks (units) ----------
RACKS = {
    "520": {"drawers": {"SMALL": 20, "MED": 0, "DEEP": 0}, "price_pln": 101.00},
    "5244": {"drawers": {"SMALL": 4, "MED": 4, "DEEP": 2}, "price_pln": 95.00},
    # "1310" is added dynamically when enabled with a price
}


def enable_1310(price_pln: Optional[float]) -> None:
    """Enable 1310 drawer types and rack option if a price is provided."""
    global DRAWER_TYPES, CAPACITY, RACKS
    DRAWER_TYPES.update({
        "S1310": {"dims": S1310_DIMS},
        "L1310": {"dims": L1310_DIMS},
        "L1310_DEEP": {"dims": L1310_DEEP_DIMS},
    })
    CAPACITY = {k: capacity_mm3(v["dims"]) for k, v in DRAWER_TYPES.items()}
    if price_pln is not None:
        RACKS["1310"] = {
            "drawers": {"S1310": 10, "L1310": 3, "L1310_DEEP": 1},
            "price_pln": float(price_pln),
        }


def apply_storage_config(path: str) -> None:
    """Apply storage configuration from YAML, overriding defaults.

    Expected structure:
      storage:
        util: 0.8
        drawer_types:
          KIND: { dims_mm: [L, W, H] }
        racks:
          CODE: { drawers: {KIND: count, ...}, price_pln: 123.0, link: "..." }
    """
    global UTIL, DRAWER_TYPES, CAPACITY, RACKS
    p = Path(path)
    if not p.exists():
        return
    if yaml is None:
        print(f"⚠️ PyYAML not installed; cannot read {path}. Using built-in defaults.")
        return
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        print(f"⚠️ Failed to parse {path}; using built-in defaults.")
        return
    storage = data.get("storage", {}) if isinstance(data, dict) else {}
    util = storage.get("util")
    if isinstance(util, (int, float)) and 0 < util <= 1:
        UTIL = float(util)
    dt = storage.get("drawer_types", {})
    if isinstance(dt, dict):
        new_types: Dict[str, Dict[str, Tuple[float, float, float]]] = {}
        for k, v in dt.items():
            dims = v.get("dims_mm") if isinstance(v, dict) else None
            if (
                isinstance(dims, list)
                and len(dims) == 3
                and all(isinstance(x, (int, float)) for x in dims)
            ):
                new_types[str(k)] = {"dims": (float(dims[0]), float(dims[1]), float(dims[2]))}
        if new_types:
            DRAWER_TYPES = new_types
            CAPACITY = {k: capacity_mm3(v["dims"]) for k, v in DRAWER_TYPES.items()}
    racks = storage.get("racks", {})
    if isinstance(racks, dict):
        new_racks: Dict[str, Dict] = {}
        for code, v in racks.items():
            drawers = v.get("drawers", {}) if isinstance(v, dict) else {}
            price = v.get("price_pln") if isinstance(v, dict) else None
            link = v.get("link") if isinstance(v, dict) else None
            if isinstance(drawers, dict) and isinstance(price, (int, float)):
                entry = {"drawers": {str(k): int(drawers[k]) for k in drawers}, "price_pln": float(price)}
                if isinstance(link, str):
                    entry["link"] = link
                new_racks[str(code)] = entry
        if new_racks:
            RACKS = new_racks


def _pack_with_types(parts: List["Part"], strategy: str, allowed_kinds: List[str]) -> Tuple[Dict[str, Dict[str, List["Drawer"]]], Dict[str, Dict[str, Tuple[float, float, float]]]]:
    """Temporarily restrict drawer types to allowed_kinds, pack, and restore.

    Returns (packed, types_used) where types_used is a mapping of kind->dims used.
    """
    global DRAWER_TYPES, CAPACITY
    old_types, old_cap = DRAWER_TYPES, CAPACITY
    try:
        filtered: Dict[str, Dict[str, Tuple[float, float, float]]] = {k: v for k, v in old_types.items() if k in allowed_kinds}
        DRAWER_TYPES = filtered
        CAPACITY = {k: capacity_mm3(v["dims"]) for k, v in DRAWER_TYPES.items()}
        packed = pack_all(parts, strategy=strategy)
        return packed, filtered
    finally:
        DRAWER_TYPES = old_types
        CAPACITY = old_cap


def _globally_unfit(parts: List["Part"]) -> List[bool]:
    """Return list indicating which parts don't fit any currently defined kind."""
    res: List[bool] = []
    all_kinds = list(DRAWER_TYPES.keys())
    for p in parts:
        fits_any = any(p.fits_conservative(DRAWER_TYPES[k]["dims"]) for k in all_kinds)
        res.append(not fits_any)
    return res


def _subset_feasible(parts: List["Part"], kinds: List[str], globally_unfit: Optional[List[bool]] = None) -> bool:
    # A subset is feasible if every globally-fittable part fits at least one kind in the subset
    for idx, p in enumerate(parts):
        if globally_unfit and globally_unfit[idx]:
            continue
        fits_any = any(p.fits_conservative(DRAWER_TYPES[k]["dims"]) for k in kinds)
        if not fits_any:
            return False
    return True


def _solve_units_generic(need: Dict[str, int], racks_codes: List[str]) -> Dict[str, float]:
    """Exact enumeration with pruning for small rack sets.

    Returns mapping {code: count, cost: total_cost}. Infeasible kinds are allowed
    only if their need is zero or no rack covers them.
    """
    racks_list = [ (code, RACKS[code]) for code in racks_codes if code in RACKS ]
    # Filter out racks that cover nothing needed
    racks_list = [ (code, info) for code, info in racks_list if any(need.get(k,0) > 0 and info["drawers"].get(k,0) > 0 for k in need.keys()) ] or racks_list

    codes = [code for code, _ in racks_list]
    drawers_list = [info["drawers"] for _, info in racks_list]
    prices = [info.get("price_pln", 0.0) for _, info in racks_list]

    # Upper bounds per rack
    ubs: List[int] = []
    for dmap in drawers_list:
        ub = 0
        for k, n in need.items():
            if n <= 0:
                continue
            per = dmap.get(k, 0)
            if per > 0:
                ub = max(ub, math.ceil(n / per))
        ubs.append(ub if ub > 0 else max(1, 0))

    # Cheap lower-bound table: cost per drawer per kind (cheapest rack covering that kind)
    cheapest_per_kind: Dict[str, float] = {}
    for k, n in need.items():
        best_cpd = float("inf")
        for (code, dmap), price in zip(racks_list, prices):
            per = dmap.get(k, 0)
            if per > 0:
                best_cpd = min(best_cpd, price / per)
        cheapest_per_kind[k] = best_cpd

    best_cost = float("inf")
    best_counts: List[int] = [0]*len(codes)

    def dfs(i: int, coverage: Dict[str, int], counts: List[int], cost_so_far: float):
        nonlocal best_cost, best_counts
        # Simple cost prune only
        if cost_so_far >= best_cost:
            return
        if i == len(codes):
            # Check feasibility
            if all(coverage.get(k, 0) >= need.get(k, 0) for k in need.keys()):
                best_cost = cost_so_far
                best_counts = counts.copy()
            return
        # Try 0..ub for this rack
        code = codes[i]
        dmap = drawers_list[i]
        price = prices[i]
        for x in range(0, ubs[i] + 1):
            new_cov = coverage.copy()
            if x > 0:
                for k, per in dmap.items():
                    if per > 0:
                        new_cov[k] = new_cov.get(k, 0) + per * x
            counts[i] = x
            dfs(i+1, new_cov, counts, cost_so_far + x * price)
        counts[i] = 0

    dfs(0, {}, [0]*len(codes), 0.0)
    sol: Dict[str, float] = {"cost": best_cost}
    for code, x in zip(codes, best_counts):
        sol[code] = x
    return sol

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

    # Kind order: prefer smaller capacities when tie-breaking
    kind_order = sorted(DRAWER_TYPES.keys(), key=lambda k: CAPACITY[k])

    for p in parts_sorted:
        qty_left = p.qty
        fits_type = {
            k: p.fits_conservative(DRAWER_TYPES[k]["dims"]) for k in DRAWER_TYPES
        }

        while qty_left > 0:
            # prefer smallest feasible drawer that minimizes new drawers
            candidates = []
            for kind in kind_order:
                if not fits_type[kind]:
                    continue
                per_draw = max(pieces_per_new_drawer(kind, p.vol_each), 1)
                need = math.ceil(qty_left / per_draw)
                tie = kind_order.index(kind)  # prefer smaller drawer on ties
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
            kinds_scan = tuple(kind_order) if strategy == "balanced" else (best_kind,)
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
    totals = {k: 0 for k in DRAWER_TYPES.keys()}
    for _, by_type in packed.items():
        for kind in totals.keys():
            totals[kind] += len(by_type.get(kind, []))
    return totals


def optimize_units(drawers_needed: Dict[str, int]) -> Dict[str, float]:
    """Compute cheapest combination of units covering demand.

    Covers base kinds (SMALL, MED, DEEP) with 520/5244. If 1310 is enabled, it
    covers S1310/L1310/L1310_DEEP independently by taking the max needed.
    """
    result: Dict[str, float] = {"520": 0, "5244": 0, "cost": 0.0}

    # 1310-specific kinds
    if "1310" in RACKS:
        need_s13 = drawers_needed.get("S1310", 0)
        need_l13 = drawers_needed.get("L1310", 0)
        need_ld13 = drawers_needed.get("L1310_DEEP", 0)
        if any([need_s13, need_l13, need_ld13]):
            s_per = max(RACKS["1310"]["drawers"].get("S1310", 1), 1)
            l_per = max(RACKS["1310"]["drawers"].get("L1310", 1), 1)
            d_per = max(RACKS["1310"]["drawers"].get("L1310_DEEP", 1), 1)
            xs13 = math.ceil(need_s13 / s_per)
            xl13 = math.ceil(need_l13 / l_per)
            xd13 = math.ceil(need_ld13 / d_per)
            x13 = max(xs13, xl13, xd13)
            result["1310"] = x13
            result["cost"] += x13 * RACKS["1310"]["price_pln"]

    # Base kinds (unchanged logic)
    need_s = drawers_needed.get("SMALL", 0)
    need_m = drawers_needed.get("MED", 0)
    need_d = drawers_needed.get("DEEP", 0)

    price_s = RACKS["520"]["price_pln"]
    price_l = RACKS["5244"]["price_pln"]

    min_xl = max(
        math.ceil(need_m / max(RACKS["5244"]["drawers"].get("MED", 1), 1)),
        math.ceil(need_d / max(RACKS["5244"]["drawers"].get("DEEP", 1), 1)),
    )
    best = {"520": 0, "5244": 0, "cost": float("inf")}
    max_xl = max(min_xl, math.ceil(need_s / max(RACKS["5244"]["drawers"].get("SMALL", 1), 1)))

    for xl in range(min_xl, max_xl + 1):
        covered_small = RACKS["5244"]["drawers"].get("SMALL", 0) * xl
        rem_small = max(0, need_s - covered_small)
        xs = math.ceil(rem_small / max(RACKS["520"]["drawers"].get("SMALL", 1), 1)) if rem_small > 0 else 0
        cost = xl * price_l + xs * price_s
        if cost < best["cost"]:
            best = {"520": xs, "5244": xl, "cost": cost}

    result["520"] = best["520"]
    result["5244"] = best["5244"]
    result["cost"] += best["cost"]
    return result


# ---------- Exports ----------
def export_purchase_order(
    solution: Dict[str, int], totals: Dict[str, int], path="purchase-order.md"
):
    cost = solution.get("cost", 0.0)

    with open(path, "w", encoding="utf-8") as f:
        f.write("# LEGO Storage Purchase Order\n\n")
        f.write("## Drawer usage\n")
        f.write(f"- SMALL drawers used: **{totals.get('SMALL', 0)}**\n")
        f.write(f"- MED drawers used: **{totals.get('MED', 0)}**\n")
        f.write(f"- DEEP drawers used: **{totals.get('DEEP', 0)}**\n")
        if 'S1310' in totals or 'L1310' in totals or 'L1310_DEEP' in totals:
            f.write(f"- S1310 drawers used: **{totals.get('S1310', 0)}**\n")
            f.write(f"- L1310 drawers used: **{totals.get('L1310', 0)}**\n")
            f.write(f"- L1310_DEEP drawers used: **{totals.get('L1310_DEEP', 0)}**\n")
        f.write("\n")

        f.write("## Units to purchase\n")
        def fmt_composition(drawers: Dict[str, int]) -> str:
            kinds = sorted(drawers.keys(), key=lambda k: CAPACITY.get(k, 0.0))
            parts = [f"{drawers[k]}× {k}" for k in kinds if drawers[k] > 0]
            return ", ".join(parts)
        # Only print units actually present in solution, excluding the 'cost' key
        for code, val in solution.items():
            if code == "cost":
                continue
            count = int(val)
            if count <= 0:
                continue
            comp = fmt_composition(RACKS.get(code, {}).get("drawers", {}))
            f.write(f"- {code} ({comp}): **{count}**\n")
        f.write("\n")

        # Dynamic shop links rendered later from RACKS

        f.write("## Costs (PLN)\n")
        for code, val in solution.items():
            if code == "cost":
                continue
            count = int(val)
            if count <= 0:
                continue
            price = RACKS.get(code, {}).get("price_pln")
            if isinstance(price, (int, float)):
                f.write(f"- {code}: {count} × {float(price):.2f} PLN\n")
        f.write(f"- **Total: {float(cost):.2f} PLN**\n")

        f.write("\n## Links\n")
        f.write("- Container Plan (Markdown): [container_plan.md](container_plan.md)\n")
        f.write("- Container Plan (PDF): [container_plan.pdf](container_plan.pdf)\n")
        f.write("- Inventory (Excel): [lego_inventory.xlsx](lego_inventory.xlsx)\n")
        f.write("- Inventory (Markdown): [lego_inventory.md](lego_inventory.md)\n")
        f.write("- Aggregated Inventory JSON: [aggregated_inventory.json](aggregated_inventory.json)\n")

        f.write("\n## Shop Links\n")
        for code, info in RACKS.items():
            link = info.get("link")
            if isinstance(link, str) and link:
                f.write(f"- {code}: {link}\n")


def export_plan_md(
    packed: Dict[str, Dict[str, List[Drawer]]], path="container_plan.md"
):
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Container Plan (Color-sorted)\n\n")
        for color, by_type in packed.items():
            f.write(f"## {color}\n\n")
            kind_order = sorted(
                [k for k, v in by_type.items() if v], key=lambda k: CAPACITY.get(k, 0.0)
            )
            for kind_label in kind_order:
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
        kind_order = sorted(
            [k for k, v in by_type.items() if v], key=lambda k: CAPACITY.get(k, 0.0)
        )
        for kind_label in kind_order:
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
    # 1310 toggle flags (enabled by default); provide price override
    group_1310 = ap.add_mutually_exclusive_group()
    group_1310.add_argument("--enable-1310", dest="use_1310", action="store_true", help="Enable 1310 drawer sizes and allow purchasing 1310 units")
    group_1310.add_argument("--disable-1310", dest="use_1310", action="store_false", help="Disable 1310 drawers and units")
    ap.set_defaults(use_1310=True)
    ap.add_argument("--price-1310", type=float, default=138.0, help="Price (PLN) for 1310 unit when enabled (default: 138.0)")
    ap.add_argument("--storage", default="storage_system.yaml", help="Path to storage system YAML to override defaults")
    ap.add_argument("--cost-optimisation", action="store_true", help="Evaluate all rack combinations and pick the cheapest; save CSV comparison")
    ap.add_argument("--compare-out", default="racks_compare.csv", help="CSV path for rack mix comparison when cost optimisation is enabled")
    args = ap.parse_args()

    prog = ProgressReporter(script="sorter", quiet=args.quiet, verbose=args.verbose, json_path=args.progress_json)

    if not Path(args.json).exists():
        raise FileNotFoundError(f"{args.json} not found. Run lego_inventory.py first.")

    # Load YAML overrides first
    apply_storage_config(args.storage)
    # Then apply 1310 toggle
    if args.use_1310:
        # If 1310 not present, add built-in sizes and rack with given price
        if "S1310" not in DRAWER_TYPES or "1310" not in RACKS:
            enable_1310(args.price_1310)
    else:
        # Remove 1310 kinds and racks if present
        for k in ("S1310", "L1310", "L1310_DEEP"):
            if k in DRAWER_TYPES:
                DRAWER_TYPES.pop(k, None)
        RACKS.pop("1310", None)
        CAPACITY = {k: capacity_mm3(v["dims"]) for k, v in DRAWER_TYPES.items()}

    st_load = prog.start("Load JSON")
    parts = load_parts(args.json)
    prog.end(st_load)

    st_pack = prog.start("Pack parts", total=len(parts))
    packed = pack_all(parts, strategy=args.pack_strategy)
    prog.end(st_pack)

    # Cost optimisation across rack subsets (optional)
    if args.cost_optimisation:
        st_mix = prog.start("Evaluate rack mixes")
        # Build subsets (all non-empty combinations)
        rack_codes = [code for code in RACKS.keys()]
        subsets: List[List[str]] = []
        for mask in range(1, 1 << len(rack_codes)):
            subset = [rack_codes[i] for i in range(len(rack_codes)) if (mask >> i) & 1]
            subsets.append(subset)

        rows: List[List[str]] = []
        best_tuple = (float("inf"), None, None, None)  # (cost, subset, packed_best, solution)

        glob_unfit = _globally_unfit(parts)

        for subset in subsets:
            kinds = sorted({k for code in subset for k in RACKS[code]["drawers"].keys()})
            # Feasibility check
            feasible = _subset_feasible(parts, kinds, globally_unfit=glob_unfit)
            if not feasible:
                rows.append(["+".join(subset), "False", "inf", "{}", ",".join(kinds)])
                continue
            packed_s, _ = _pack_with_types(parts, args.pack_strategy, kinds)
            need_s = count_drawers(packed_s)
            # Remove kinds not in subset
            need_s = {k: v for k, v in need_s.items() if k in kinds}
            sol_s = _solve_units_generic(need_s, subset)
            cost_s = sol_s.get("cost", float("inf"))
            units_s = {k: int(v) for k, v in sol_s.items() if k != "cost" and int(v) > 0}
            rows.append(["+".join(subset), "True", f"{cost_s:.2f}", json.dumps(units_s, ensure_ascii=False), ",".join(kinds)])
            if cost_s < best_tuple[0]:
                best_tuple = (cost_s, subset, packed_s, sol_s)

        # Write CSV
        try:
            with open(args.compare_out, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["subset", "feasible", "total_cost_pln", "units", "kinds_used"])
                w.writerows(rows)
        except Exception:
            pass

        prog.end(st_mix)

        # Adopt best subset plan
        if best_tuple[1] is not None:
            packed = best_tuple[2]  # type: ignore
            totals = count_drawers(packed)
            solution = best_tuple[3]  # type: ignore
        else:
            totals = count_drawers(packed)
            solution = optimize_units(totals)
    else:
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
        used_parts = ", ".join(
            f"{k}: {totals[k]}" for k in sorted(totals.keys(), key=lambda t: CAPACITY.get(t, 0.0)) if totals[k] > 0
        )
        print(f"✅ Packed by color. Drawers used → {used_parts}")
        # Dynamic units summary (exclude 'cost' and zero-count units)
        units_list = []
        for code, cnt in solution.items():
            if code == "cost":
                continue
            n = int(cnt)
            if n > 0:
                units_list.append(f"{code}: {n}")
        units_str = ", ".join(units_list)
        print(f"🧮 Units → {units_str}  (Total {solution['cost']:.2f} PLN)")

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
