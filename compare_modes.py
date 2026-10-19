"""Controlled comparison of cross-container rebalancing and root diversification.

Run with:
    python compare_modes.py

Outputs:
    mode_comparison.json
    mode_comparison.txt
"""
import sys
import types
import time
import json
import statistics
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
pkg = types.ModuleType("container_optimizer")
pkg.__path__ = [str(APP_DIR)]
sys.modules["container_optimizer"] = pkg

from container_optimizer.cargo import product_rows_to_cargo_items
from container_optimizer.models import ContainerSpec, LoadingConfig
from container_optimizer.optimization import optimize_loading

CONTAINER = ContainerSpec("40' High-Cube (40HQ)", 12032, 2352, 2698, 28000)

CASES = {
    "mixed_160": [
        {"name": "Big", "l": 1200, "w": 800, "h": 1000, "wt": 10, "qty": 60},
        {"name": "Small", "l": 600, "w": 400, "h": 400, "wt": 2, "qty": 100},
    ],
    "mixed_hard": [
        {"name": "A", "l": 1200, "w": 800, "h": 1000, "wt": 8, "qty": 40},
        {"name": "B", "l": 900, "w": 700, "h": 700, "wt": 6, "qty": 40},
        {"name": "C", "l": 700, "w": 500, "h": 500, "wt": 4, "qty": 40},
        {"name": "D", "l": 500, "w": 400, "h": 300, "wt": 2, "qty": 40},
        {"name": "E", "l": 300, "w": 200, "h": 200, "wt": 1, "qty": 40},
    ],
    "mixed_500": [
        {"name": "A", "l": 1200, "w": 800, "h": 1000, "wt": 8, "qty": 100},
        {"name": "B", "l": 900, "w": 700, "h": 700, "wt": 6, "qty": 100},
        {"name": "C", "l": 700, "w": 500, "h": 500, "wt": 4, "qty": 100},
        {"name": "D", "l": 500, "w": 400, "h": 300, "wt": 2, "qty": 100},
        {"name": "E", "l": 300, "w": 200, "h": 200, "wt": 1, "qty": 100},
    ],
    "mixed_2000": [
        {"name": "A", "l": 1200, "w": 800, "h": 1000, "wt": 8, "qty": 400},
        {"name": "B", "l": 900, "w": 700, "h": 700, "wt": 6, "qty": 400},
        {"name": "C", "l": 700, "w": 500, "h": 500, "wt": 4, "qty": 400},
        {"name": "D", "l": 500, "w": 400, "h": 300, "wt": 2, "qty": 400},
        {"name": "E", "l": 300, "w": 200, "h": 200, "wt": 1, "qty": 400},
    ],
}


def base_config():
    return LoadingConfig(
        mode="auto",
        time_budget_seconds=10.0,
        auto_time_per_carton=0.05,
        fast_pack_budget=1.0,
        enable_repacking=True,
        enable_cross_rebalancing=False,
        enable_root_diversification=False,
        enable_searates_scoring=True,
    )


def mode_configs():
    return {
        "BASE": base_config(),
        "REBAL": replace(base_config(), enable_cross_rebalancing=True, cross_rebalance_budget=5.0),
        "ROOT": replace(base_config(), enable_root_diversification=True, root_branch_width=3),
        "BOTH": replace(
            base_config(),
            enable_cross_rebalancing=True,
            cross_rebalance_budget=5.0,
            enable_root_diversification=True,
            root_branch_width=3,
        ),
    }


def run_one(case, config, label):
    items = product_rows_to_cargo_items(CASES[case])
    t0 = time.perf_counter()
    c0 = time.process_time()
    plan = optimize_loading(items, CONTAINER, selected_quantity=1, allow_auto_add=True, config=config)
    wall = time.perf_counter() - t0
    cpu = time.process_time() - c0

    total_container_vol = sum(c.spec.volume_m3 for c in plan.containers)
    total_cargo_vol = plan.total_volume_m3
    util = (total_cargo_vol / total_container_vol * 100.0) if total_container_vol else 0.0
    stabs = [c.diagnostics.get("stability_score", 0.0) for c in plan.containers]
    sups = [c.diagnostics.get("support_ratio", 0.0) for c in plan.containers]
    scores = [c.diagnostics.get("packing_score", 0.0) for c in plan.containers]
    return {
        "case": case,
        "mode": label,
        "wall_s": round(wall, 3),
        "cpu_s": round(cpu, 3),
        "containers": len(plan.containers),
        "packed": plan.loaded_count,
        "leftover": plan.leftover_count,
        "util_pct": round(util, 2),
        "stability": round(statistics.mean(stabs), 4) if stabs else 0.0,
        "support": round(statistics.mean(sups), 4) if sups else 0.0,
        "packing_score": round(statistics.mean(scores), 4) if scores else 0.0,
        "valid": plan.loaded_count + plan.leftover_count == len(items),
    }


def main():
    from dataclasses import replace
    global replace
    rows = []
    for case in CASES:
        for label, config in mode_configs().items():
            print(f"Running {case} / {label} ...", flush=True)
            rows.append(run_one(case, config, label))

    with open("mode_comparison.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)

    with open("mode_comparison.txt", "w", encoding="utf-8") as f:
        f.write(f"{'Case':<12} {'Mode':<6} {'Wall':>6} {'CPU':>6} {'Cont':>4} {'Pack':>5} {'Left':>5} {'Util%':>6} {'Supp':>5} {'Stab':>5} {'Valid':>5}\n")
        f.write("-" * 80 + "\n")
        for r in rows:
            f.write(
                f"{r['case']:<12} {r['mode']:<6} {r['wall_s']:>6.2f} {r['cpu_s']:>6.2f} "
                f"{r['containers']:>4} {r['packed']:>5} {r['leftover']:>5} {r['util_pct']:>6.2f} "
                f"{r['support']:>5.3f} {r['stability']:>5.3f} {str(r['valid']):>5}\n"
            )

    print("\n" + "=" * 80)
    with open("mode_comparison.txt", encoding="utf-8") as f:
        print(f.read())
    print("Saved: mode_comparison.json, mode_comparison.txt")


if __name__ == "__main__":
    main()
