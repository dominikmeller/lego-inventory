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
import subprocess
import hashlib
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

# Packing headroom cap (fraction of capacity allowed to be used per drawer)
PACK_MAX_FILL: float = 1.0


# ---------- Run metadata helpers ----------
def _get_git_info() -> Optional[Dict[str, object]]:
    try:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL).decode("utf-8").strip()
        status = subprocess.check_output(["git", "status", "--porcelain"], stderr=subprocess.DEVNULL).decode("utf-8")
        dirty = bool(status.strip())
        try:
            branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], stderr=subprocess.DEVNULL).decode("utf-8").strip()
        except Exception:
            branch = None
        return {"commit": sha, "dirty": dirty, "branch": branch}
    except Exception:
        return None

def _sha256_file(path: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None

# ---------- Racks (units) ----------
RACKS = {
    "520": {"drawers": {"SMALL": 20, "MED": 0, "DEEP": 0}, "price_pln": 101.00, "external_cm": [37.8, 15.4, 18.9]},
    "5244": {"drawers": {"SMALL": 4, "MED": 4, "DEEP": 2}, "price_pln": 95.00, "external_cm": [37.8, 15.4, 18.9]},
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
            "external_cm": [44.9, 18.0, 24.7],
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
        new_types: Dict[str, Dict] = {}
        for k, v in dt.items():
            dims = v.get("dims_mm") if isinstance(v, dict) else None
            if (
                isinstance(dims, list)
                and len(dims) == 3
                and all(isinstance(x, (int, float)) for x in dims)
            ):
                entry: Dict[str, object] = {"dims": (float(dims[0]), float(dims[1]), float(dims[2]))}
                price = v.get("price_pln") if isinstance(v, dict) else None
                if isinstance(price, (int, float)):
                    entry["price_pln"] = float(price)
                title = v.get("title") if isinstance(v, dict) else None
                if isinstance(title, str):
                    entry["title"] = title
                new_types[str(k)] = entry
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
            ext = v.get("external_cm") if isinstance(v, dict) else None
            frame_only = v.get("frame_only") if isinstance(v, dict) else None
            if isinstance(drawers, dict) and isinstance(price, (int, float)):
                entry = {"drawers": {str(k): int(drawers[k]) for k in drawers}, "price_pln": float(price)}
                if isinstance(link, str):
                    entry["link"] = link
                if isinstance(ext, list) and len(ext) == 3 and all(isinstance(x, (int, float)) for x in ext):
                    entry["external_cm"] = [float(ext[0]), float(ext[1]), float(ext[2])]
                if isinstance(frame_only, bool):
                    entry["frame_only"] = frame_only
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
                "VolEach_mm3": float(part.vol_each or 0.0),
                "VolTotal_mm3": float(vol),
            }
        )
        self.used += vol


def _merge_item_into_drawer(dr: Drawer, item: Dict, add_qty: int, vol_each: float) -> None:
    # Try to find existing entry to merge quantities
    pid = str(item.get("Part ID", ""))
    for it in dr.items:
        if str(it.get("Part ID", "")) == pid:
            it["Qty"] = int(it.get("Qty", 0)) + int(add_qty)
            # Maintain or set volume fields
            try:
                it["VolEach_mm3"] = float(it.get("VolEach_mm3", vol_each))
            except Exception:
                it["VolEach_mm3"] = float(vol_each)
            try:
                prev = float(it.get("VolTotal_mm3", 0.0))
            except Exception:
                prev = 0.0
            it["VolTotal_mm3"] = prev + float(vol_each) * int(add_qty)
            dr.used += vol_each * add_qty
            return
    # Otherwise append new
    dr.items.append({
        "Part ID": pid,
        "Part Name": item.get("Part Name", ""),
        "Color": item.get("Color", ""),
        "Color ID": item.get("Color ID", 0),
        "Qty": int(add_qty),
        "Image File": item.get("Image File", ""),
        "VolEach_mm3": float(vol_each),
        "VolTotal_mm3": float(vol_each) * int(add_qty),
    })
    dr.used += vol_each * add_qty


def _enforce_min_fill(
    packed: Dict[str, Dict[str, List[Drawer]]],
    parts: List[Part],
    min_fill: float,
    buckets: set,
) -> None:
    # Build part_id -> vol_each map
    vol_map: Dict[str, float] = {}
    for p in parts:
        pid = str(p.part_id)
        if pid not in vol_map:
            v = p.vol_each
            if v is None or v <= 0:
                _ensure_part_dims_and_volume(p)
                v = p.vol_each
            vol_map[pid] = float(v or 0.0)

    for color in list(packed.keys()):
        if color not in buckets:
            continue
        by_type = packed[color]
        for kind, drawers in list(by_type.items()):
            if not drawers:
                continue
            full_cap = CAPACITY.get(kind, 0.0)
            if full_cap <= 0:
                continue
            cap = full_cap * PACK_MAX_FILL
            # Flatten all items across this color+kind into a single pool
            pool: List[Dict] = []  # each: {item, vol_each, qty}
            total_vol = 0.0
            for d in drawers:
                for it in d.items:
                    pid = str(it.get("Part ID", ""))
                    q = int(it.get("Qty", 0))
                    if q <= 0:
                        continue
                    ve = vol_map.get(pid, 0.0)
                    if ve <= 0:
                        continue
                    pool.append({"item": it, "vol_each": ve, "qty": q})
                    total_vol += ve * q
            # Clear drawers; we'll rebuild
            drawers[:] = []
            if total_vol <= 0:
                continue
            # If pool volume cannot reach min_fill in any drawer, place all into one drawer
            target_base = min_fill * full_cap
            if total_vol < target_base:
                nd = Drawer(kind=kind, color=color, capacity=cap)
                for e in pool:
                    _merge_item_into_drawer(nd, e["item"], int(e["qty"]), float(e["vol_each"]))
                drawers.append(nd)
                continue
            # Otherwise, pack greedily to minimize drawers, then balance last to meet min_fill if possible
            pool.sort(key=lambda e: -e["vol_each"])  # largest items first
            # Initial greedy pass filling towards capacity
            new_drawers: List[Drawer] = []
            cur: Optional[Drawer] = None
            while pool:
                if cur is None:
                    cur = Drawer(kind=kind, color=color, capacity=full_cap)
                progressed = False
                for idx in range(len(pool)):
                    e = pool[idx]
                    ve = float(e["vol_each"]) or 0.0
                    if ve <= 0:
                        continue
                    rem = max(0.0, cap - cur.used)
                    if rem <= 0:
                        break
                    fit = int(rem // ve)
                    if fit <= 0:
                        continue
                    take = min(int(e["qty"]) or 0, fit)
                    if take > 0:
                        _merge_item_into_drawer(cur, e["item"], take, ve)
                        e["qty"] = int(e["qty"]) - take
                        progressed = True
                # Drop exhausted items
                pool = [e for e in pool if int(e["qty"]) > 0]
                if not progressed or not pool:
                    new_drawers.append(cur)
                    cur = None
            if cur is not None and cur.items:
                new_drawers.append(cur)

            # Balancing pass: raise any underfilled drawer to target_base by borrowing from earlier drawers with surplus
            target = target_base
            for i in range(len(new_drawers) - 1, -1, -1):
                d = new_drawers[i]
                if d.used >= target:
                    continue
                needed = target - d.used
                # Borrow from earlier drawers j where (used - target) > 0
                for j in range(0, i):
                    dj = new_drawers[j]
                    surplus = dj.used - target
                    if surplus <= 0:
                        continue
                    # Move items from dj to d
                    moved_any = False
                    for it in list(dj.items):
                        pid = str(it.get("Part ID", ""))
                        ve = vol_map.get(pid, 0.0)
                        if ve <= 0:
                            continue
                        qj = int(it.get("Qty", 0))
                        if qj <= 0:
                            continue
                        max_move_qty = int(min(surplus, needed) // ve)
                        if max_move_qty <= 0:
                            continue
                        # Move quantity
                        take = min(qj, max_move_qty)
                        _merge_item_into_drawer(d, it, take, ve)
                        it["Qty"] = qj - take
                        try:
                            it["VolTotal_mm3"] = float(ve) * int(it["Qty"])  # keep per-entry totals consistent
                        except Exception:
                            it["VolTotal_mm3"] = float(ve) * int(it.get("Qty", 0))
                        dj.used -= ve * take
                        moved_any = True
                        needed -= ve * take
                        if it["Qty"] <= 0:
                            dj.items.remove(it)
                        if needed <= 0:
                            break
                    if moved_any and needed <= 0:
                        break
                # If still under target, cannot satisfy; leave as is
            # Remove any empty drawers (shouldn't occur, but safe)
            new_drawers = [d for d in new_drawers if d.items]
            drawers[:] = new_drawers


# ---------- Packing helpers ----------
def max_fit_by_volume(rem: float, vol_each: float) -> int:
    if not vol_each or vol_each <= 0:
        return 0
    return int(rem // vol_each)


def pieces_per_new_drawer(kind: str, vol_each: float) -> int:
    if not vol_each or vol_each <= 0:
        return 0
    eff_cap = CAPACITY[kind] * PACK_MAX_FILL
    return int(eff_cap // vol_each)


# ---------- Color grouping helpers ----------
def is_transparent_color(color_name: str) -> bool:
    """Heuristic: treat any color that contains 'Trans' (e.g., 'Trans-Red', 'Opal Trans-Light Blue',
    'Glitter Trans-Light Blue', 'Trans-Clear') as transparent.

    Case-insensitive match on the substring 'trans'.
    """
    try:
        return "trans" in color_name.lower()
    except Exception:
        return False


def apply_transparent_mixing(parts: List["Part"], bucket_label: str = "TRANSPARENT") -> None:
    """Rewrite transparent colors to a single bucket label in-place.

    This reduces nearly-empty drawers by coalescing all transparent shades.
    """
    for p in parts:
        if is_transparent_color(p.color):
            p.color = bucket_label


def _ensure_part_dims_and_volume(p: "Part") -> None:
    # Mirror the pre-processing in pack_color_bucket to ensure we can measure volumes during mixing
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


def apply_rare_mixing(parts: List["Part"], threshold_frac: float = 0.25, bucket_label: str = "RARE") -> None:
    """Mark colors with total volume below threshold as RARE.

    threshold is a fraction of the smallest drawer capacity across kinds.
    """
    if not parts or not DRAWER_TYPES:
        return
    # Determine absolute threshold in mm^3
    min_cap = min(CAPACITY.values()) if CAPACITY else 0.0
    thr = max(0.0, float(threshold_frac)) * min_cap
    # Aggregate volume by color
    vol_by_color: Dict[str, float] = {}
    for p in parts:
        _ensure_part_dims_and_volume(p)
        v = (p.vol_each or 0.0) * int(p.qty)
        vol_by_color[p.color] = vol_by_color.get(p.color, 0.0) + v
    # Colors to mark as rare (exclude already-transparent bucket label)
    rare_colors = {c for c, v in vol_by_color.items() if v < thr and c not in (bucket_label, "TRANSPARENT")}
    if not rare_colors:
        return
    for p in parts:
        if p.color in rare_colors:
            p.color = bucket_label


def maybe_merge_transparent_into_rare(parts: List["Part"], min_fill: float) -> None:
    """If total transparent volume is below min_fill threshold for the smallest drawer,
    merge all TRANSPARENT items into RARE to avoid producing sub-50% drawers.
    """
    if min_fill <= 0 or not parts or not CAPACITY:
        return
    min_cap = min(CAPACITY.values())
    thr = min_fill * min_cap
    total_trans = 0.0
    for p in parts:
        if p.color == "TRANSPARENT":
            _ensure_part_dims_and_volume(p)
            total_trans += (p.vol_each or 0.0) * int(p.qty)
    if total_trans > 0.0 and total_trans < thr:
        for p in parts:
            if p.color == "TRANSPARENT":
                p.color = "RARE"


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
                    eff_cap = CAPACITY[kind_try] * PACK_MAX_FILL
                    rem_eff = max(0.0, eff_cap - dr.used)
                    cap = max_fit_by_volume(rem_eff, p.vol_each)
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

    Default logic covers base kinds via bundled racks (520/5244) and optional 1310.
    If storage defines 'frame_only' racks and drawer types have prices, we decouple
    frame cost (by capacity slots) from drawer purchase (per-kind price), buying
    only as many drawers as needed and the minimum number of frames to host them.
    """
    # Detect frame-only racks (e.g., TROFAST frames) and priced drawer types
    frame_racks = {code: info for code, info in RACKS.items() if info.get("frame_only")}
    any_priced_drawers = any(isinstance(DRAWER_TYPES.get(k, {}).get("price_pln"), (int, float)) for k in drawers_needed)
    if frame_racks and any_priced_drawers:
        # For simplicity pick the cheapest frame across those that provide slots for the needed kinds
        best: Optional[Tuple[str, int, float]] = None  # (code, frames_needed, frames_cost)
        needed_kinds = {k for k, n in drawers_needed.items() if n > 0}
        for code, info in frame_racks.items():
            slots: Dict[str, int] = info.get("drawers", {})
            if not any(slots.get(k, 0) > 0 for k in needed_kinds):
                continue
            frames_needed = 0
            # Frames required is the max over kinds ceil(need_k / slots_k) for kinds this frame can host
            for k in needed_kinds:
                per = slots.get(k, 0)
                if per > 0:
                    frames_needed = max(frames_needed, math.ceil(drawers_needed.get(k, 0) / per))
            frames_needed = max(frames_needed, 1) if sum(drawers_needed.get(k, 0) for k in needed_kinds) > 0 else 0
            frames_cost = frames_needed * float(info.get("price_pln", 0.0))
            if best is None or frames_cost < best[2]:
                best = (code, frames_needed, frames_cost)
        result: Dict[str, float] = {"cost": 0.0}
        # Drawer cost: per-kind price times needed count
        drawer_cost = 0.0
        for k, n in drawers_needed.items():
            if n <= 0:
                continue
            price = float(DRAWER_TYPES.get(k, {}).get("price_pln", 0.0) or 0.0)
            if price > 0:
                result[k] = int(n)
                drawer_cost += price * int(n)
        frames_cost = 0.0
        if best is not None and best[1] > 0:
            result[best[0]] = int(best[1])
            frames_cost = best[2]
        result["cost"] = drawer_cost + frames_cost
        return result
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
    solution: Dict[str, int],
    totals: Dict[str, int],
    path: str = "purchase-order.md",
    *,
    plan_md: Optional[str] = None,
    plan_pdf: Optional[str] = None,
    used_volume_mm3: Optional[float] = None,
    purchased_capacity_mm3: Optional[float] = None,
    meta: Optional[Dict[str, object]] = None,
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
        def fmt_external_cm(code: str) -> Optional[str]:
            dims = RACKS.get(code, {}).get("external_cm")
            if isinstance(dims, (list, tuple)) and len(dims) == 3:
                try:
                    L, W, H = float(dims[0]), float(dims[1]), float(dims[2])
                    return f"{L:.1f}×{W:.1f}×{H:.1f} cm"
                except Exception:
                    return None
            return None
        # Only print units actually present in solution, excluding the 'cost' key
        for code, val in solution.items():
            if code == "cost":
                continue
            count = int(val)
            if count <= 0:
                continue
            if code in RACKS:
                comp = fmt_composition(RACKS.get(code, {}).get("drawers", {}))
                size_txt = fmt_external_cm(code)
                if size_txt:
                    f.write(f"- {code} ({comp}): **{count}** — {size_txt}\n")
                else:
                    f.write(f"- {code} ({comp}): **{count}**\n")
            else:
                # Drawer kind purchase line
                if code in DRAWER_TYPES:
                    f.write(f"- {code}: **{count}**\n")
        f.write("\n")

        # Dynamic shop links rendered later from RACKS

        f.write("## Costs (PLN)\n")
        for code, val in solution.items():
            if code == "cost":
                continue
            count = int(val)
            if count <= 0:
                continue
            price = RACKS.get(code, {}).get("price_pln") if code in RACKS else DRAWER_TYPES.get(code, {}).get("price_pln")
            if isinstance(price, (int, float)):
                f.write(f"- {code}: {count} × {float(price):.2f} PLN\n")
        f.write(f"- **Total: {float(cost):.2f} PLN**\n")

        f.write("\n## Links\n")
        md_link = plan_md if isinstance(plan_md, str) else "container_plan.md"
        pdf_link = plan_pdf if isinstance(plan_pdf, str) else "container_plan.pdf"
        f.write(f"- Container Plan (Markdown): [{md_link}]({md_link})\n")
        f.write(f"- Container Plan (PDF): [{pdf_link}]({pdf_link})\n")
        f.write("- Inventory (Excel): [lego_inventory.xlsx](lego_inventory.xlsx)\n")
        f.write("- Inventory (Markdown): [lego_inventory.md](lego_inventory.md)\n")
        f.write("- Aggregated Inventory JSON: [aggregated_inventory.json](aggregated_inventory.json)\n")

        # Volume summary (optional)
        if isinstance(used_volume_mm3, (int, float)) or isinstance(purchased_capacity_mm3, (int, float)):
            uv = float(used_volume_mm3 or 0.0)
            pc = float(purchased_capacity_mm3 or 0.0)
            pct = int(100 * uv / pc) if pc > 0 else 0
            f.write("\n## Volume Summary\n")
            f.write(f"- Packed volume: {uv:.0f} mm³ ({uv/1e6:.2f} L)\n")
            f.write(f"- Purchased capacity (at max-fill): {pc:.0f} mm³ ({pc/1e6:.2f} L)\n")
            f.write(f"- Utilization: {pct}%\n")

        f.write("\n## Shop Links\n")
        for code, info in RACKS.items():
            link = info.get("link")
            if isinstance(link, str) and link:
                f.write(f"- {code}: {link}\n")
        # Also list drawer type references if present
        for kind, info in DRAWER_TYPES.items():
            link = info.get("link") if isinstance(info, dict) else None
            if isinstance(link, str) and link:
                f.write(f"- {kind}: {link}\n")

        # Machine-readable YAML summary for tooling
        try:
            summary_units = {
                code: int(val)
                for code, val in solution.items()
                if code != "cost" and int(val) > 0
            }
            summary_racks = {}
            for code, cnt in summary_units.items():
                info = RACKS.get(code, {})
                entry = {
                    "price_pln": float(info.get("price_pln", 0.0)),
                    "drawers": info.get("drawers", {}),
                }
                if isinstance(info.get("external_cm"), list):
                    entry["external_cm"] = info["external_cm"]
                if isinstance(info.get("link"), str):
                    entry["link"] = info["link"]
                summary_racks[code] = entry

            yaml_payload = {
                "solution": {
                    "units": summary_units,
                    "total_cost_pln": float(cost),
                },
                "racks": summary_racks,
                "drawer_usage": {k: int(v) for k, v in totals.items()},
                "volume_mm3": {
                    "packed": float(used_volume_mm3 or 0.0),
                    "purchased_capacity": float(purchased_capacity_mm3 or 0.0),
                },
            }
            if meta is not None:
                # Keep meta compact: ensure JSON-serializable builtins only
                try:
                    yaml_payload["meta"] = meta
                except Exception:
                    pass

            f.write("\n## Machine-Readable Summary (YAML)\n")
            f.write("```yaml\n")
            if yaml is not None:
                f.write(yaml.safe_dump(yaml_payload, sort_keys=True))
            else:
                # Minimal YAML serialization fallback
                f.write("solution:\n")
                f.write("  units:\n")
                for code, cnt in summary_units.items():
                    f.write(f"    {code}: {cnt}\n")
                f.write(f"  total_cost_pln: {float(cost):.2f}\n")
                f.write("racks:\n")
                for code, info in summary_racks.items():
                    f.write(f"  {code}:\n")
                    f.write(f"    price_pln: {info['price_pln']:.2f}\n")
                    if "external_cm" in info:
                        L, W, H = info["external_cm"]
                        f.write(f"    external_cm: [{L}, {W}, {H}]\n")
                    if "link" in info:
                        f.write(f"    link: {info['link']}\n")
                    f.write("    drawers:\n")
                    for kind, n in info["drawers"].items():
                        f.write(f"      {kind}: {n}\n")
                f.write("drawer_usage:\n")
                for kind, n in totals.items():
                    f.write(f"  {kind}: {int(n)}\n")
            f.write("```\n")
        except Exception:
            # Non-fatal; continue without YAML block
            pass


def export_plan_md(
    packed: Dict[str, Dict[str, List[Drawer]]], path="container_plan.md", *, meta: Optional[Dict[str, object]] = None
):
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Container Plan (Color-sorted)\n\n")
        if meta:
            # Compact run settings for quick comparisons
            f.write("## Run Settings\n")
            ts = str(meta.get("timestamp", "")) if isinstance(meta, dict) else ""
            storage_label = str(meta.get("storage_label", "")) if isinstance(meta, dict) else ""
            f.write(f"- Timestamp: {ts}\n")
            if storage_label:
                f.write(f"- Storage label: {storage_label}\n")
            try:
                args_meta = meta.get("args", {}) if isinstance(meta, dict) else {}
            except Exception:
                args_meta = {}
            if isinstance(args_meta, dict):
                # Show the key tunables if present
                keys = [
                    "pack_strategy",
                    "min_fill",
                    "max_fill",
                    "rare_threshold",
                    "mix_transparents",
                    "mix_rare",
                    "merge_trans_into_rare",
                    "exclude_duplo",
                ]
                for k in keys:
                    if k in args_meta:
                        f.write(f"- {k}: {args_meta[k]}\n")
            f.write("\n")
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
                    # Show fill percentage for quick spotting of underfilled drawers
                    try:
                        pct = int(100 * (dr.used / max(1e-9, dr.capacity)))
                    except Exception:
                        pct = 0
                    f.write(f"#### Drawer {i} — Fill: {pct}%\n")
                    for item in sorted(dr.items, key=lambda it: (str(it.get('Part ID','')), str(it.get('Part Name','')))):
                        vol_e = item.get("VolEach_mm3")
                        vol_t = item.get("VolTotal_mm3")
                        if isinstance(vol_e, (int, float)) and isinstance(vol_t, (int, float)):
                            f.write(
                                f"- {item['Part ID']} | {item['Part Name']} | Qty: {item['Qty']} | v_each: {vol_e:.0f} mm³ | v_total: {vol_t:.0f} mm³\n"
                            )
                        else:
                            f.write(
                                f"- {item['Part ID']} | {item['Part Name']} | Qty: {item['Qty']}\n"
                            )
                    # Drawer totals and verification
                    eff_cap = CAPACITY.get(kind_label, 0.0) * PACK_MAX_FILL
                    try:
                        pct_eff = int(100 * (dr.used / max(1e-9, eff_cap))) if eff_cap > 0 else 0
                    except Exception:
                        pct_eff = pct
                    status = "OK" if dr.used <= eff_cap + 1e-6 else "OVER"
                    f.write(
                        f"- Total volume: {dr.used:.0f} mm³ / allowed {eff_cap:.0f} mm³ ({pct_eff}%) — {status}\n\n"
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
        vol_e = item.get("VolEach_mm3")
        vol_t = item.get("VolTotal_mm3")
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
            if isinstance(vol_e, (int, float)) and isinstance(vol_t, (int, float)):
                c.drawString(margin + 24, y - 18, f"{item['Part ID']} • v_each: {vol_e:.0f} mm3 • v_total: {vol_t:.0f} mm3")
            else:
                c.drawString(margin + 24, y - 18, f"{item['Part ID']}")
            y -= 26
        else:
            c.setFont("Helvetica", 10)
            if isinstance(vol_e, (int, float)) and isinstance(vol_t, (int, float)):
                c.drawString(
                    margin, y, f"- {item['Part Name']} x{item['Qty']} ({item['Part ID']}) • v_each: {vol_e:.0f} mm3 • v_total: {vol_t:.0f} mm3"
                )
            else:
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
                # Drawer totals and verification line
                eff_cap = CAPACITY.get(kind_label, 0.0) * PACK_MAX_FILL
                c.setFont("Helvetica-Oblique", 8)
                pct_eff = int(100 * (dr.used / eff_cap)) if eff_cap > 0 else 0
                status = "OK" if dr.used <= eff_cap + 1e-6 else "OVER"
                if need_space(2):
                    new_page()
                c.drawString(margin, y, f"Total volume: {dr.used:.0f} mm3 / allowed {eff_cap:.0f} mm3 ({pct_eff}%) — {status}")
                y -= 12

    c.save()


def export_duplo_report(excluded: List[Part], path: str) -> None:
    by_color: Dict[str, List[Part]] = {}
    for p in excluded:
        by_color.setdefault(p.color, []).append(p)
    lines: List[str] = []
    lines.append("# DUPLO Items (Excluded from Sorter)\n")
    total_items = sum(p.qty for p in excluded)
    lines.append(f"Total unique parts: {len(excluded)}  |  Total pieces: {total_items}\n")
    for color in sorted(by_color.keys()):
        lines.append(f"\n## {color}\n")
        items = sorted(by_color[color], key=lambda pp: (str(pp.part_id), str(pp.name)))
        for p in items:
            lines.append(f"- {p.part_id} | {p.name} | Qty: {p.qty}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    ap.add_argument("--progress-json", default=None, help="Write progress JSON to this path (will be prefixed + placed in output dir)")
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
    ap.add_argument("--compare-out", default="racks_compare.csv", help="CSV path for rack mix comparison when cost optimisation is enabled (will be prefixed + placed in output dir)")
    ap.add_argument("--run-billy", action="store_true", help="After exports, run billy-fitting.py to produce BILLY layout from purchase-order.md")
    ap.add_argument("--run-trofast", action="store_true", help="After exports, run trofast-fitting.py to produce TROFAST frame layout from purchase-order.md")
    # Presets / aliases
    ap.add_argument(
        "--preset-trofast-rare-split",
        action="store_true",
        help=(
            "Alias: apply best-known TROFAST 'Good Rare Split (C)' parameters: "
            "--storage storage_trofast.yaml --disable-1310 --exclude-duplo --mix-transparents "
            "--mix-rare --rare-threshold 0.15 --min-fill 0.5 --max-fill 0.85 --merge-trans-into-rare "
            "--pack-strategy balanced, and run trofast-fitting (PDF enabled)"
        ),
    )
    ap.add_argument(
        "--preset-billy-structured",
        action="store_true",
        help=(
            "Alias: apply best-known BILLY 'Structured Coding' parameters (2-cabinet limit run): "
            "--storage storage_system.yaml --disable-1310 --exclude-duplo --mix-transparents "
            "--mix-rare --rare-threshold 0.45 --min-fill 0.5 --max-fill 1.0 --pack-strategy greedy, "
            "and run billy-fitting (PDF enabled)"
        ),
    )
    ap.add_argument("--output-dir", default="output", help="Directory to write outputs into (default: output)")
    ap.add_argument(
        "--exclude-duplo",
        action="store_true",
        help="Exclude DUPLO parts (detected by 'Duplo' in part name) from packing; they are considered outside the system",
    )
    ap.add_argument(
        "--duplo-report",
        default="duplo_excluded.md",
        help="Filename for the excluded DUPLO summary written to the output directory",
    )
    ap.add_argument(
        "--max-fill",
        type=float,
        default=1.0,
        help="Maximum fraction of a drawer's capacity that can be used (0.0–1.0). Example: 0.85",
    )
    ap.add_argument(
        "--mix-transparents",
        action="store_true",
        help="Mix all transparent colors into a single 'TRANSPARENT' bucket to reduce nearly-empty drawers",
    )
    ap.add_argument(
        "--mix-rare",
        action="store_true",
        help="Pool rare colors (by total volume) into a shared 'RARE' bucket before packing",
    )
    ap.add_argument(
        "--rare-threshold",
        type=float,
        default=0.25,
        help="Threshold as a fraction of the smallest drawer capacity to mark a color as rare (default: 0.25)",
    )
    ap.add_argument(
        "--min-fill",
        type=float,
        default=0.0,
        help="Minimum per-drawer fill ratio to enforce for shared buckets (0.0–1.0). Example: 0.5",
    )
    ap.add_argument(
        "--merge-trans-into-rare",
        action="store_true",
        help="If transparent pool cannot reach --min-fill, merge it into RARE before packing",
    )
    args = ap.parse_args()

    # Apply preset aliases (mutates args to enforce discovered best settings)
    if getattr(args, "preset_trofast_rare_split", False):
        args.storage = "storage_trofast.yaml"
        args.use_1310 = False
        args.exclude_duplo = True
        args.mix_transparents = True
        args.mix_rare = True
        args.rare_threshold = 0.15
        args.min_fill = 0.5
        args.max_fill = 0.85
        args.merge_trans_into_rare = True
        args.pack_strategy = "balanced"
        args.run_trofast = True
        # Ensure PDFs are generated
        args.no_pdf = False

    if getattr(args, "preset_billy_structured", False):
        args.storage = "storage_system.yaml"
        args.use_1310 = False
        args.exclude_duplo = True
        args.mix_transparents = True
        args.mix_rare = True
        args.rare_threshold = 0.45
        args.min_fill = 0.5
        args.max_fill = 1.0
        args.merge_trans_into_rare = False
        args.pack_strategy = "greedy"
        args.run_billy = True
        # Ensure PDFs are generated
        args.no_pdf = False

    # Prepare output directory + timestamp prefix + storage suffix
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    # Derive storage label from the storage file basename
    try:
        storage_label = re.sub(r"[^A-Za-z0-9]+", "-", Path(args.storage).stem).strip("-") or "storage"
    except Exception:
        storage_label = "storage"

    # Normalize compare and progress paths into output dir with timestamp prefix
    if args.compare_out:
        args.compare_out = str(out_dir / f"{ts}-{storage_label}-{Path(args.compare_out).name}")
    if args.progress_json:
        args.progress_json = str(out_dir / f"{ts}-{storage_label}-{Path(args.progress_json).name}")

    # Apply max fill globally
    global PACK_MAX_FILL, CAPACITY
    try:
        PACK_MAX_FILL = max(0.1, min(1.0, float(args.max_fill)))
    except Exception:
        PACK_MAX_FILL = 1.0

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

    # Optionally exclude DUPLO parts (stored outside of the system)
    excluded_duplo: List[Part] = []
    if args.exclude_duplo:
        def _is_duplo(p: Part) -> bool:
            try:
                return "duplo" in (p.name or "").lower()
            except Exception:
                return False
        kept: List[Part] = []
        for p in parts:
            if _is_duplo(p):
                excluded_duplo.append(p)
            else:
                kept.append(p)
        parts = kept

    # Optional: mix all transparent colors into one bucket to reduce drawer count for rare colors
    if args.mix_transparents:
        apply_transparent_mixing(parts, bucket_label="TRANSPARENT")
    # Optional: pool rare colors into a shared bucket
    if args.mix_rare and args.rare_threshold > 0.0:
        apply_rare_mixing(parts, threshold_frac=args.rare_threshold, bucket_label="RARE")
    # Optional: merge transparent into rare if its pool cannot reach the --min-fill threshold
    if args.merge_trans_into_rare and args.min_fill > 0.0:
        maybe_merge_transparent_into_rare(parts, min_fill=args.min_fill)

    st_pack = prog.start("Pack parts", total=len(parts))
    packed = pack_all(parts, strategy=args.pack_strategy)
    prog.end(st_pack)

    # Enforce minimum fill on shared buckets (TRANSPARENT/RARE) if requested
    if args.min_fill and args.min_fill > 0.0:
        buckets_to_fix = set()
        if args.mix_transparents:
            buckets_to_fix.add("TRANSPARENT")
        if args.mix_rare:
            buckets_to_fix.add("RARE")
        if buckets_to_fix:
            _enforce_min_fill(packed, parts, min_fill=max(0.0, min(1.0, args.min_fill)), buckets=buckets_to_fix)

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
    # Build timestamped, outdir-scoped file paths
    po_path = out_dir / f"{ts}-purchase-order-{storage_label}.md"
    md_path = out_dir / f"{ts}-container_plan-{storage_label}.md"
    pdf_path = out_dir / f"{ts}-container_plan-{storage_label}.pdf"
    # Append storage label before extension for duplo report
    _duplo_name = Path(args.duplo_report).name
    _duplo_stem, _duplo_ext = Path(_duplo_name).stem, Path(_duplo_name).suffix or ".md"
    duplo_path = out_dir / f"{ts}-{_duplo_stem}-{storage_label}{_duplo_ext}"

    # Build run metadata and sidecar
    inv_path = Path(args.json)
    run_meta: Dict[str, object] = {
        "timestamp": ts,
        "command": " ".join(sys.argv),
        "args": {k: getattr(args, k) for k in vars(args).keys()},
        "storage_label": storage_label,
        "storage_config": str(args.storage),
        "output_dir": str(out_dir),
        "inventory_file": str(inv_path),
        "inventory_hash": _sha256_file(inv_path),
        "git": _get_git_info(),
        "pack_max_fill": PACK_MAX_FILL,
        "drawer_types": {k: {"dims_mm": list(DRAWER_TYPES[k]["dims"]), **({"price_pln": float(DRAWER_TYPES[k]["price_pln"]) } if isinstance(DRAWER_TYPES.get(k, {}).get("price_pln"), (int, float)) else {})} for k in DRAWER_TYPES},
        "racks": RACKS,
    }
    # Sidecar JSON
    sidecar_path = out_dir / f"{ts}-{storage_label}-run_meta.json"
    try:
        sidecar_path.write_text(json.dumps(run_meta, indent=2), encoding="utf-8")
    except Exception:
        pass

    # Compute volume metrics
    total_used_mm3 = 0.0
    for _, by_type in packed.items():
        for _, drawers in by_type.items():
            for dr in drawers:
                total_used_mm3 += float(dr.used or 0.0)
    purchased_capacity_mm3 = 0.0
    for code, val in solution.items():
        if code == "cost":
            continue
        cnt = int(val)
        if cnt <= 0:
            continue
        if code in DRAWER_TYPES:
            purchased_capacity_mm3 += cnt * CAPACITY.get(code, 0.0) * PACK_MAX_FILL
        elif code in RACKS:
            dmap = RACKS.get(code, {}).get("drawers", {}) or {}
            for kind, per in dmap.items():
                purchased_capacity_mm3 += cnt * int(per) * CAPACITY.get(kind, 0.0) * PACK_MAX_FILL

    export_purchase_order(
        solution,
        totals,
        path=str(po_path),
        plan_md=md_path.name,
        plan_pdf=pdf_path.name,
        used_volume_mm3=total_used_mm3,
        purchased_capacity_mm3=purchased_capacity_mm3,
        meta=run_meta,
    )
    if not args.purchase_only and not args.no_md:
        export_plan_md(packed, path=str(md_path), meta=run_meta)
    if not args.purchase_only and not args.no_pdf:
        export_plan_pdf(packed, path=str(pdf_path))
    if args.exclude_duplo and excluded_duplo:
        export_duplo_report(excluded_duplo, path=str(duplo_path))
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

    # Optional: execute BILLY fitting planner
    def _run_billy(po_src: str):
        try:
            import subprocess, sys as _sys
            print("→ Running billy-fitting.py …")
            _ = subprocess.run([_sys.executable, "billy-fitting.py", "--source", po_src])  # noqa: S603
        except Exception as e:
            print(f"⚠️ Could not run billy-fitting.py: {e}")

    def _run_trofast(po_src: str):
        try:
            import subprocess, sys as _sys
            print("→ Running trofast-fitting.py …")
            _ = subprocess.run([_sys.executable, "trofast-fitting.py", "--source", po_src, "--output-dir", str(out_dir)])  # noqa: S603
        except Exception as e:
            print(f"⚠️ Could not run trofast-fitting.py: {e}")

    if args.run_billy:
        _run_billy(str(po_path))
    elif not args.quiet:
        try:
            ans = input("Run fitting planner now? (y/n): ").strip().lower()
            if ans == "y":
                _run_billy(str(po_path))
        except (EOFError, KeyboardInterrupt):
            pass

    if args.run_trofast:
        _run_trofast(str(po_path))
    elif not args.quiet:
        try:
            ans = input("Run TROFAST fitting planner now? (y/n): ").strip().lower()
            if ans == "y":
                _run_trofast(str(po_path))
        except (EOFError, KeyboardInterrupt):
            pass


if __name__ == "__main__":
    main()
