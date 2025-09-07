import math

import lego_inventory as inv
import lego_sorter as sorter


def test_infer_dims_from_name_studs():
    L, W, H = inv.infer_dims_from_name("Brick 2 x 4")
    assert math.isclose(L or 0, 16.0, rel_tol=1e-6)
    assert math.isclose(W or 0, 32.0, rel_tol=1e-6)
    assert math.isclose(H or 0, 9.6, rel_tol=1e-6)


def test_infer_dims_from_name_tyre_wheel():
    L, W, H = inv.infer_dims_from_name("Wheel 30 x 10 mm")
    assert (L, W, H) == (30.0, 30.0, 10.0)


def test_fill_defaults_when_partial():
    L, W, H = inv.fill_dims_with_defaults_or_studs(10.0, None, None)
    assert L == 10.0 and W == inv.DEFAULT_W_IF_MISSING and H == inv.DEFAULT_H_IF_MISSING


def test_max_fit_and_pieces_per_drawer():
    # Use SMALL drawer capacity from sorter with UTIL
    cap = sorter.CAPACITY["SMALL"]
    vol_each = 1000.0
    # max_fit_by_volume should be floor division
    assert sorter.max_fit_by_volume(cap, vol_each) == int(cap // vol_each)
    # pieces_per_new_drawer respects PACK_MAX_FILL (default 1.0 here)
    assert sorter.pieces_per_new_drawer("SMALL", vol_each) == int(cap // vol_each)


def test_transparent_mixing_and_rare_pooling():
    # Minimal synthetic parts
    parts = [
        sorter.Part(part_id="p1", name="Brick 2 x 4", color="Trans-Red", color_id=1, qty=5, l=None, w=None, h=None, vol_each=None),
        sorter.Part(part_id="p2", name="Brick 2 x 4", color="Bright Blue", color_id=2, qty=1, l=None, w=None, h=None, vol_each=None),
    ]
    sorter.apply_transparent_mixing(parts, bucket_label="TRANSPARENT")
    assert all(p.color in ("TRANSPARENT", "Bright Blue") for p in parts)
    # Rare pooling with a generous threshold should pool the blue item too
    sorter.apply_rare_mixing(parts, threshold_frac=1.0, bucket_label="RARE")
    assert any(p.color == "RARE" for p in parts)

