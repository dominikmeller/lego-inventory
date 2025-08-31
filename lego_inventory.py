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
"""

import os
import sys
import json
import time
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import pandas as pd
import requests
from dotenv import load_dotenv

# ---------------- Config ----------------
BASE_URL = "https://rebrickable.com/api/v3/lego"
IMAGE_DIR = Path("images")
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

def parse_sets_from_args_or_file() -> List[str]:
    if len(sys.argv) > 1:
        return [s.strip() for s in sys.argv[1:] if s.strip()]
    if Path(SETS_FILE).exists():
        with open(SETS_FILE, "r", encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip()]
    print("No set numbers provided. Supply as CLI args or create sets.txt.")
    sys.exit(1)

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

def ensure_image(part_id: str, color_id: int, url: Optional[str]) -> str:
    IMAGE_DIR.mkdir(exist_ok=True)
    path = IMAGE_DIR / f"{part_id}_{color_id}.jpg"
    if url and not path.exists():
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                path.write_bytes(resp.content)
        except Exception:
            pass
    return str(path)

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
def get_set_details(setnum: str, headers: Dict[str, str]) -> Optional[Dict[str, Any]]:
    for candidate in [setnum, f"{setnum}-1"]:
        data = api_get(f"{BASE_URL}/sets/{candidate}/", headers)
        if data:
            return data
    return None

def get_set_parts(setnum: str, headers: Dict[str, str]) -> List[Dict[str, Any]]:
    results = []
    for candidate in [setnum, f"{setnum}-1"]:
        url = f"{BASE_URL}/sets/{candidate}/parts/?page_size=1000"
        while url:
            data = api_get(url, headers)
            if not data:
                break
            results.extend(data.get("results", []))
            url = data.get("next")
        if results:
            break
    return results

# ------------- Main ---------------------
def main():
    api_key = load_api_key()
    headers = {"Authorization": f"key {api_key}"}
    set_list = parse_sets_from_args_or_file()

    inventory_rows: List[Dict[str, Any]] = []

    for raw_set in set_list:
        setnum = raw_set.strip()

        details = get_set_details(setnum, headers)
        if not details:
            print(f"⚠️ Set not found: {setnum}")
            continue

        set_id = details.get("set_num", f"{setnum}-1")
        set_name = details.get("name", "Unknown Set")

        parts = get_set_parts(set_id, headers)
        time.sleep(0.35)  # polite pause per page

        for it in parts:
            part = it["part"]
            color = it["color"]
            qty = int(it.get("quantity", 1))

            # Parse then apply default-filling strategy
            L, W, H = infer_dims_from_name(part.get("name", ""))
            L, W, H = fill_dims_with_defaults_or_studs(L, W, H)

            img_path = ensure_image(part["part_num"], color["id"], part.get("part_img_url"))

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

    Path(OUT_JSON).write_text(json.dumps({"parts": agg_records}, indent=2), encoding="utf-8")

    print(f"✅ Saved: {OUT_XLSX}, {OUT_MD}, {OUT_JSON}")

    # Optional: run sorter now
    ans = input("Run sorter now? (y/n): ").strip().lower()
    if ans == "y":
        import subprocess, sys as _sys
        _sys.stdout.flush()
        subprocess.run([_sys.executable, "lego_sorter.py", "--json", OUT_JSON])

if __name__ == "__main__":
    main()
