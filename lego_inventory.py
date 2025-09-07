#!/usr/bin/env python3
"""
LEGO Inventory Pipeline
-----------------------
- Reads set list from CLI or sets.txt (one set per line).
- Loads REBRICKABLE_API_KEY from .env; if missing, prompts for input.
- Fetches set inventories with retry + backoff; falls back to "-1" suffix.
- Parses dimensions from part name:
    * Tyre/Wheel numbers are interpreted as millimetres.
    * Brick/Plate/Tile 'A x B x C' are studs → mm (8 mm per stud; plate=3.2 mm; brick=9.6 mm).
- Dimension defaults:
    * If ALL dims are unknown → fallback box = 2×4×1 studs = 16×32×9.6 mm (4915.2 mm³).
    * If SOME dims are known → fill missing independently: L=30 mm (depth), W=10 mm, H=10 mm.
- Downloads part images locally (images/{part_id}_{color_id}.jpg).
- Exports:
  - lego_inventory.xlsx  (Inventory + Aggregated)
  - lego_inventory.md    (markdown list)
  - aggregated_inventory.json  (canonical handoff for lego_sorter.py)

Progress Reporting (script-level)
---------------------------------
- Flags: `--quiet`, `--verbose`, `--progress-json <path>`, `--no-prompt`.
- Emits phased progress and a final summary; optional JSON with timings and counters.

Image Caching
-------------
- Images are cached under `images/{part_id}_{color_id}.jpg`.
- Use `--no-images` to skip downloads or `--refresh-images` to force re-download.
"""

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
from dotenv import load_dotenv


# ---------------- Progress utils (lightweight) ----------------
@dataclass
class Step:
    name: str
    status: str = "pending"  # pending | in_progress | completed | failed
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    items_total: Optional[int] = None
    items_done: int = 0
    notes: List[str] = field(default_factory=list)


class ProgressReporter:
    def __init__(
        self,
        script: str,
        quiet: bool = False,
        verbose: bool = False,
        json_path: Optional[str] = None,
    ) -> None:
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

    def update(self, st: Step, done: Optional[int] = None, total: Optional[int] = None, msg: Optional[str] = None) -> None:
        if done is not None:
            st.items_done = done
        if total is not None:
            st.items_total = total
        if msg:
            st.notes.append(msg)
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

    def finalize(self, totals: Dict[str, Any], errors: List[str]) -> None:
        elapsed = int(time.time() - self.t0)
        if not self.quiet:
            print(
                f"[inventory] Summary: sets={totals.get('sets', 0)}, unique_items={totals.get('unique_items', 0)}, "
                f"pieces_total={totals.get('pieces_total', 0)}, rows_raw={totals.get('rows_raw', 0)}, "
                f"images_available={totals.get('images', 0)} | files={totals.get('outputs', 0)} | elapsed={elapsed}s"
            )
            if errors:
                print(f"Warnings: {len(errors)}")
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
                        "notes": s.notes,
                    }
                    for s in self.steps
                ],
                "totals": totals,
                "errors": errors,
            }
            Path(self.json_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")

# ---------------- Config ----------------
BASE_URL = "https://rebrickable.com/api/v3/lego"
IMAGE_DIR = Path("images")
CACHE_DIR = Path("cache")
CACHE_SETS_DIR = CACHE_DIR / "sets"
CACHE_PARTS_DIR = CACHE_DIR / "parts"
OUT_XLSX = "lego_inventory.xlsx"
OUT_MD = "lego_inventory.md"
OUT_JSON = "aggregated_inventory.json"
SETS_FILE = "sets.txt"

# Stud & brick metrics (mm)
STUD_MM = 8.0
PLATE_H_MM = 3.2
BRICK_H_MM = 9.6

# Independent defaults when SOME dims are known
DEFAULT_L_IF_MISSING = 30.0  # "depth"
DEFAULT_W_IF_MISSING = 10.0
DEFAULT_H_IF_MISSING = 10.0

# Fallback when ALL dims are unknown: 2×4×1 studs
FALLBACK_STUDS_DIMS = (2 * STUD_MM, 4 * STUD_MM, 1 * BRICK_H_MM)  # (16, 32, 9.6) mm
FALLBACK_VOL_EACH = FALLBACK_STUDS_DIMS[0] * FALLBACK_STUDS_DIMS[1] * FALLBACK_STUDS_DIMS[2]  # 4915.2 mm³

# ------------- Helpers ------------------
def load_api_key() -> str:
    load_dotenv()
    key = os.getenv("REBRICKABLE_API_KEY")
    if not key:
        key = input("Enter your Rebrickable API key: ").strip()
    return key

def parse_sets_from_file(path: str) -> List[str]:
    p = Path(path)
    if p.exists():
        with p.open("r", encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip()]
    return []

def backoff_sleep(attempt: int) -> None:
    time.sleep(min(2 ** attempt, 10))

def api_get(url: str, headers: Dict[str, str]) -> Optional[Dict[str, Any]]:
    for attempt in range(5):
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        if r.status_code in (429, 500, 502, 503, 504):
            backoff_sleep(attempt)
            continue
        r.raise_for_status()
    return None


def _read_cache(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def _write_cache(path: Path, data: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass

def ensure_image(
    part_id: str,
    color_id: int,
    url: Optional[str],
    *,
    refresh: bool = False,
    enable: bool = True,
) -> Tuple[str, bool]:
    IMAGE_DIR.mkdir(exist_ok=True)
    path = IMAGE_DIR / f"{part_id}_{color_id}.jpg"
    # If downloads disabled, just report whether it exists
    if not enable:
        return str(path), path.exists()

    # If file exists and not refreshing, use cache
    if path.exists() and not refresh:
        return str(path), True

    if url:
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                path.write_bytes(resp.content)
                return str(path), True
            # Non-200; return whether a file exists already
            return str(path), path.exists()
        except Exception:
            return str(path), path.exists()
    return str(path), path.exists()

# ------------- Dimension Parser -----------------
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

    # Tyre/Wheel: treat two numbers as mm (Diameter x Width)
    mm_match = _TYRE_WHEEL_MM_RE.search(n)
    if mm_match:
        diameter = float(mm_match.group(2))
        width = float(mm_match.group(3))
        return diameter, diameter, width  # L, W, H as bounding box

    # Stud-based: A x B x C (studs)
    stud_match = _STUD_RE.search(n)
    if stud_match:
        a, b, c = stud_match.group(1), stud_match.group(2), stud_match.group(3)
        L = _stud_to_mm(a)
        W = _stud_to_mm(b)
        H = None
        if 'tile' in n or 'plate' in n:
            H = PLATE_H_MM
        elif 'brick' in n and not c:
            H = BRICK_H_MM
        elif c:
            if '/' in c:
                num, den = c.split('/')
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
    if L is None: L = DEFAULT_L_IF_MISSING
    if W is None: W = DEFAULT_W_IF_MISSING
    if H is None: H = DEFAULT_H_IF_MISSING
    return L, W, H

# ------------- Fetchers -----------------
def get_set_details(
    setnum: str,
    headers: Dict[str, str],
    *,
    use_cache: bool = True,
    refresh_cache: bool = False,
) -> Optional[Dict[str, Any]]:
    for candidate in [setnum, f"{setnum}-1"]:
        cache_path = CACHE_SETS_DIR / f"{candidate}.json"
        if use_cache and not refresh_cache:
            cached = _read_cache(cache_path)
            if cached:
                return cached
        data = api_get(f"{BASE_URL}/sets/{candidate}/", headers)
        if data:
            _write_cache(cache_path, data)
            return data
    return None

def get_set_parts(
    set_id: str,
    headers: Dict[str, str],
    *,
    use_cache: bool = True,
    refresh_cache: bool = False,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Return (parts, from_cache). set_id should be the resolved set like '1234-1'."""
    cache_path = CACHE_PARTS_DIR / f"{set_id}.json"
    if use_cache and not refresh_cache:
        cached = _read_cache(cache_path)
        if cached and isinstance(cached.get("results"), list):
            return list(cached.get("results", [])), True

    results: List[Dict[str, Any]] = []
    url = f"{BASE_URL}/sets/{set_id}/parts/?page_size=1000"
    while url:
        data = api_get(url, headers)
        if not data:
            break
        results.extend(data.get("results", []))
        url = data.get("next")
    if results:
        _write_cache(cache_path, {"results": results})
    return results, False

# ------------- Main ---------------------
def main():
    ap = argparse.ArgumentParser(description="LEGO inventory pipeline with progress reporting")
    ap.add_argument("sets", nargs="*", help="Set numbers (e.g. 31129 76989). If empty, uses sets.txt")
    ap.add_argument("--sets-file", default=SETS_FILE, help="Path to sets list file")
    ap.add_argument("--progress-json", default=None, help="Write progress JSON to this path")
    ap.add_argument("--quiet", action="store_true", help="Only print final summary")
    ap.add_argument("--verbose", action="store_true", help="Print step-by-step logs")
    ap.add_argument("--no-prompt", action="store_true", help="Do not prompt to run sorter")
    ap.add_argument("--no-images", action="store_true", help="Skip downloading part images (use existing cache if present)")
    ap.add_argument("--refresh-images", action="store_true", help="Force re-download of all part images")
    ap.add_argument("--refresh-cache", action="store_true", help="Force refresh of API cache for set details and parts")
    ap.add_argument("--output-dir", default="output", help="Directory to write timestamped copies of exports (default: output)")
    args = ap.parse_args()

    api_key = load_api_key()
    headers = {"Authorization": f"key {api_key}"}

    set_list: List[str] = [s.strip() for s in args.sets if s.strip()]
    if not set_list:
        set_list = parse_sets_from_file(args.sets_file)
    if not set_list:
        print("No set numbers provided. Supply as CLI args or create sets.txt.")
        sys.exit(1)

    prog = ProgressReporter(script="inventory", quiet=args.quiet, verbose=args.verbose, json_path=args.progress_json)

    inventory_rows: List[Dict[str, Any]] = []
    errors: List[str] = []
    st_fetch = prog.start("Fetch sets", total=len(set_list))

    for raw_set in set_list:
        setnum = raw_set.strip()

        details = get_set_details(setnum, headers, use_cache=not args.refresh_cache, refresh_cache=args.refresh_cache)
        if not details:
            print(f"⚠️ Set not found: {setnum}")
            errors.append(f"set_not_found:{setnum}")
            st_fetch.items_done += 1
            prog.update(st_fetch)
            continue

        set_id = details.get("set_num", f"{setnum}-1")
        set_name = details.get("name", "Unknown Set")

        parts, from_cache = get_set_parts(set_id, headers, use_cache=not args.refresh_cache, refresh_cache=args.refresh_cache)
        st_fetch.items_done += 1
        prog.update(st_fetch)
        # Delay only if we actually pulled from the API (not from cache)
        if not from_cache:
            time.sleep(0.35)  # polite pause between API calls

        st_images = prog.start(f"Images for {set_id}")
        for it in parts:
            part = it["part"]
            color = it["color"]
            qty = int(it.get("quantity", 1))

            # Parse then apply default-filling strategy
            L, W, H = infer_dims_from_name(part.get("name", ""))
            L, W, H = fill_dims_with_defaults_or_studs(L, W, H)

            img_path, available = ensure_image(
                part["part_num"],
                color["id"],
                part.get("part_img_url"),
                refresh=args.refresh_images,
                enable=not args.no_images,
            )
            if available:
                st_images.items_done += 1
                prog.update(st_images)

            inventory_rows.append({
                "Set Number": set_id,
                "Set Name": set_name,
                "Part ID": part["part_num"],
                "Part Name": part["name"],
                "Color": color["name"],
                "Color ID": color["id"],
                "Quantity": qty,
                "Length (mm)": L,
                "Width (mm)": W,
                "Height (mm)": H,
                "Image File": img_path
            })
        prog.end(st_images)

    prog.end(st_fetch)

    st_agg = prog.start("Aggregate inventory")
    df = pd.DataFrame(inventory_rows)

    # Aggregated (unique Part ID + Color)
    def first_nonnull(s):
        s = s.dropna()
        return s.iloc[0] if not s.empty else None

    agg = (df.groupby(["Part ID", "Part Name", "Color", "Color ID"], as_index=False)
             .agg(Quantity=("Quantity", "sum"),
                  **{"Length (mm)": ("Length (mm)", first_nonnull),
                     "Width (mm)":  ("Width (mm)",  first_nonnull),
                     "Height (mm)": ("Height (mm)", first_nonnull)},
                  **{"Image File": ("Image File", first_nonnull)}))

    # Exports
    with pd.ExcelWriter(OUT_XLSX) as w:
        df.to_excel(w, sheet_name="Inventory", index=False)
        agg.to_excel(w, sheet_name="Aggregated", index=False)

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("# LEGO Inventory\n\n")
        for _, row in agg.iterrows():
            f.write(f"- {row['Part Name']} | {row['Color']} | Qty: {row['Quantity']} | ID: {row['Part ID']}\n")

    # Canonical JSON handoff for lego_sorter.py
    agg_records = []
    for _, r in agg.iterrows():
        L = float(r["Length (mm)"]) if pd.notna(r["Length (mm)"]) else None
        W = float(r["Width (mm)"])  if pd.notna(r["Width (mm)"])  else None
        H = float(r["Height (mm)"]) if pd.notna(r["Height (mm)"]) else None
        L, W, H = fill_dims_with_defaults_or_studs(L, W, H)
        rec = {
            "part_id": str(r["Part ID"]),
            "part_name": str(r["Part Name"]),
            "color": str(r["Color"]),
            "color_id": int(r["Color ID"]),
            "quantity": int(r["Quantity"]),
            "length_mm": L,
            "width_mm":  W,
            "height_mm": H,
            "volume_each_mm3": L * W * H,
            "image_file": str(r["Image File"]) if isinstance(r["Image File"], str) else ""
        }
        agg_records.append(rec)

    json_payload = json.dumps({"parts": agg_records}, indent=2)
    Path(OUT_JSON).write_text(json_payload, encoding="utf-8")

    prog.end(st_agg)

    # Write timestamped copies into output directory
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    xlsx_ts = out_dir / f"{ts}-lego_inventory.xlsx"
    md_ts = out_dir / f"{ts}-lego_inventory.md"
    json_ts = out_dir / f"{ts}-aggregated_inventory.json"
    # Save copies
    try:
        with pd.ExcelWriter(xlsx_ts) as w:
            df.to_excel(w, sheet_name="Inventory", index=False)
            agg.to_excel(w, sheet_name="Aggregated", index=False)
    except Exception:
        pass
    try:
        md_ts.write_text(Path(OUT_MD).read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass
    try:
        json_ts.write_text(json_payload, encoding="utf-8")
    except Exception:
        pass

    st_export = prog.start("Export files")
    outputs = [OUT_XLSX, OUT_MD, OUT_JSON, str(xlsx_ts), str(md_ts), str(json_ts)]
    st_export.items_done = len(outputs)
    st_export.items_total = len(outputs)
    prog.end(st_export)

    if not args.quiet:
        print(f"✅ Saved: {OUT_XLSX}, {OUT_MD}, {OUT_JSON}")
        print(f"✅ Timestamped copies: {xlsx_ts.name}, {md_ts.name}, {json_ts.name} → {out_dir}")

    # Optional: run sorter now
    # Finalize progress before optional sorter run
    totals = {
        "sets": len(set_list),
        # Raw rows = occurrences across sets
        "rows_raw": int(df.shape[0]),
        # Unique aggregated items (Part ID + Color)
        "unique_items": int(agg.shape[0]),
        # Total pieces across all items
        "pieces_total": int(agg["Quantity"].sum()),
        "images": int((df["Image File"].astype(str) != "").sum()),
        "outputs": 3,
    }
    prog.finalize(totals=totals, errors=errors)

    # Optional: run sorter now
    if not args.no_prompt:
        ans = input("Run sorter now? (y/n): ").strip().lower()
        if ans == "y":
            import subprocess, sys as _sys
            _sys.stdout.flush()
            subprocess.run([_sys.executable, "lego_sorter.py", "--json", OUT_JSON])

if __name__ == "__main__":
    main()
