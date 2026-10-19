"""Bounded Beam Search prototype experiment vs BASE.

Run with:
    python beam_experiment.py

Outputs:
    beam_experiment.json
    beam_experiment.txt
"""
import sys
import types
import time
import json
import math
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
pkg = types.ModuleType("container_optimizer")
pkg.__path__ = [str(APP_DIR)]
sys.modules["container_optimizer"] = pkg

from container_optimizer.models import ContainerSpec, LoadingConfig, CargoItem
from container_optimizer.packing import pack_container, beam_search_pack, sort_items_for_loading

import test_adversarial as ta


def _valid(packed, container):
    seen = set()
    total_wt = 0.0
    for it in packed:
        if id(it.cargo) in seen:
            return False
        seen.add(id(it.cargo))
        x, y, z = it.position
        dx, dy, dz = it.size
        if x < -1e-6 or y < -1e-6 or z < -1e-6:
            return False
        if x + dx > container.length_mm + 1e-6 or y + dy > container.width_mm + 1e-6 or z + dz > container.height_mm + 1e-6:
            return False
        total_wt += it.cargo.weight_kg
    if total_wt > container.max_weight_kg + 1e-6:
        return False
    for it in packed:
        x, y, z = it.position
        dx, dy, dz = it.size
        for ot in packed:
            if ot is it:
                continue
            ox, oy, oz = ot.position
            odx, ody, odz = ot.size
            if (x < ox + odx and x + dx > ox and y < oy + ody and y + dy > oy and z < oz + odz and z + dz > oz):
                return False
        if z > 1e-6 and not it.cargo.overhang_allowed:
            base = dx * dy
            supported = 0.0
            for ot in packed:
                if ot is it:
                    continue
                ox, oy, oz = ot.position
                odx, ody, odz = ot.size
                if abs(oz + odz - z) > 1e-6:
                    continue
                supported += max(0.0, min(x + dx, ox + odx) - max(x, ox)) * max(0.0, min(y + dy, oy + ody) - max(y, oy))
            if supported / max(base, 1.0) < 0.70 - 1e-6:
                return False
    return True


def _stability(packed, container):
    if not packed:
        return 1.0, 1.0
    total_weight = max(sum(it.cargo.weight_kg for it in packed), 1e-9)
    cgz = sum((it.position[2] + it.size[2] / 2) * it.cargo.weight_kg for it in packed) / total_weight
    support_vals = []
    for it in packed:
        if it.position[2] <= 1e-6:
            support_vals.append(1.0)
            continue
        base = max(it.size[0] * it.size[1], 1.0)
        supported = 0.0
        for ot in packed:
            if ot is it:
                continue
            if abs(ot.position[2] + ot.size[2] - it.position[2]) > 1e-6:
                continue
            supported += max(0.0, min(it.position[0] + it.size[0], ot.position[0] + ot.size[0]) - max(it.position[0], ot.position[0])) * max(0.0, min(it.position[1] + it.size[1], ot.position[1] + ot.size[1]) - max(it.position[1], ot.position[1]))
        support_vals.append(min(1.0, supported / base))
    support = sum(support_vals) / len(support_vals)
    low_cog = max(0.0, 1.0 - cgz / max(container.height_mm, 1))
    return support, support * low_cog


def run_base(container, items, label, cfg):
    t0 = time.perf_counter()
    c0 = time.process_time()
    packed, leftover = pack_container(container, items, config=cfg)
    wall = time.perf_counter() - t0
    cpu = time.process_time() - c0
    exp_count, exp_vol = ta.exhaustive_best(container, items, node_budget=30_000_000)
    got_vol = sum(i.size[0] * i.size[1] * i.size[2] for i in packed.items)
    sup, stab = _stability(packed.items, container)
    return {
        "label": label,
        "mode": "BASE",
        "wall_s": round(wall, 4),
        "cpu_s": round(cpu, 4),
        "exhaustive_count": int(exp_count),
        "exhaustive_vol_mm3": float(exp_vol),
        "packed": len(packed.items),
        "leftover": len(leftover),
        "count_gap": int(exp_count) - len(packed.items),
        "packed_vol_mm3": float(got_vol),
        "vol_gap": float(exp_vol) - float(got_vol),
        "support": round(sup, 4),
        "stability": round(stab, 4),
        "termination": packed.diagnostics.get("search_termination", "unknown"),
        "valid": _valid(packed.items, container),
    }


def run_beam(container, items, label, beam_width, beam_carton_limit):
    cfg = LoadingConfig(beam_width=beam_width, beam_carton_limit=beam_carton_limit)
    cartons = list(sort_items_for_loading(items, cfg))
    t0 = time.perf_counter()
    c0 = time.process_time()
    layout = beam_search_pack(container, cartons, cfg)
    wall = time.perf_counter() - t0
    cpu = time.process_time() - c0
    packed_ids = {id(it.cargo) for it in layout.items}
    leftover = [c for c in items if id(c) not in packed_ids]
    exp_count, exp_vol = ta.exhaustive_best(container, items, node_budget=30_000_000)
    got_vol = sum(i.size[0] * i.size[1] * i.size[2] for i in layout.items)
    sup, stab = _stability(layout.items, container)
    return {
        "label": label,
        "mode": f"BEAM(bw={beam_width},bl={beam_carton_limit})",
        "wall_s": round(wall, 4),
        "cpu_s": round(cpu, 4),
        "exhaustive_count": int(exp_count),
        "exhaustive_vol_mm3": float(exp_vol),
        "packed": len(layout.items),
        "leftover": len(leftover),
        "count_gap": int(exp_count) - len(layout.items),
        "packed_vol_mm3": float(got_vol),
        "vol_gap": float(exp_vol) - float(got_vol),
        "support": round(sup, 4),
        "stability": round(stab, 4),
        "termination": "beam",
        "valid": _valid(layout.items, container),
    }


def main():
    rows = []

    # Seed cases on a tight container
    MINI = ContainerSpec("Mini-2.5x1.5x1.5", 2500, 1500, 1500, 10000)
    for seed in (2111, 2138):
        items = ta.TestSyntheticRealistic()._gen(seed, n_skus=8, qty_lo=1, qty_hi=1)
        for bw, bl in [(12, 8), (16, 8), (24, 8)]:
            rows.append(run_base(MINI, items, f"seed{seed}_base", LoadingConfig()))
            rows.append(run_beam(MINI, items, f"seed{seed}_beam{bw}", bw, bl))

    # Tiny adversarial cases against exhaustive reference
    tiny_cases = [
        ("tiny_uniform", ta.SMALLER, ta.cargo(1000, 1000, 1000, 10, qty=5)),
        ("tiny_mixed", ta.SMALLER, ta.cargo(2000, 1000, 1000, 10, 2) + ta.cargo(1000, 500, 500, 2, 4)),
        ("tiny_constrained", ta.SMALLER, ta.cargo(1500, 1000, 800, 10, 2, disable_stacking=True) + ta.cargo(500, 500, 500, 2, 3)),
        ("tiny_tilt", ta.SMALLER, ta.cargo(1900, 1200, 600, 5, 3, tilt_to_length=True)),
    ]
    for name, container, items in tiny_cases:
        rows.append(run_base(container, items, name, LoadingConfig()))
        rows.append(run_beam(container, items, name, 12, min(20, len(items))))

    with open("beam_experiment.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)

    with open("beam_experiment.txt", "w", encoding="utf-8") as f:
        f.write(f"{'Label':<22} {'Mode':<22} {'Wall':>6} {'CPU':>6} {'ExCnt':>5} {'Pack':>5} {'Left':>5} "
                f"{'Gap':>4} {'Supp':>5} {'Stab':>5} {'Term':<10} {'Valid':>5}\n")
        f.write("-" * 110 + "\n")
        for r in rows:
            f.write(
                f"{r['label']:<22} {r['mode']:<22} {r['wall_s']:>6.4f} {r['cpu_s']:>6.4f} "
                f"{r['exhaustive_count']:>5} {r['packed']:>5} {r['leftover']:>5} {r['count_gap']:>4} "
                f"{r['support']:>5.3f} {r['stability']:>5.3f} {r['termination']:<10} {str(r['valid']):>5}\n"
            )

    print("\n" + "=" * 110)
    with open("beam_experiment.txt", encoding="utf-8") as f:
        print(f.read())
    print("Saved: beam_experiment.json, beam_experiment.txt")


if __name__ == "__main__":
    main()
