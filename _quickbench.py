"""Quick timing benchmark — mirrors benchmark.py cases without report generation."""
import sys
import types
import time
import tracemalloc
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
pkg = types.ModuleType("container_optimizer")
pkg.__path__ = [str(APP_DIR)]
sys.modules["container_optimizer"] = pkg

from container_optimizer.cargo import product_rows_to_cargo_items
from container_optimizer.models import ContainerSpec, LoadingConfig
from container_optimizer.packing import pack_container

CONTAINER = ContainerSpec("40' High-Cube (40HQ)", 12032, 2352, 2698, 28000)

BEFORE = LoadingConfig(
    mode="manual",
    packing_sequence="largest_volume_first",
    placement_strategy="bottom_left_fill",
    packing_pattern="balanced",
    enable_searates_scoring=False,
    enable_repacking=False,
    adaptive_search=False,
    time_budget_seconds=5.0,
)

AFTER = LoadingConfig(mode="auto")


def make_items(case):
    products = {
        "mixed_160": [
            {"name": "Big", "l": 1200, "w": 800, "h": 1000, "wt": 10, "qty": 60},
            {"name": "Small", "l": 600, "w": 400, "h": 400, "wt": 2, "qty": 100},
        ],
        "uniform_100": [
            {"name": "Box", "l": 1000, "w": 1000, "h": 1000, "wt": 5, "qty": 100},
        ],
        "small_200": [
            {"name": "Mini", "l": 500, "w": 400, "h": 300, "wt": 1, "qty": 200},
        ],
        "uniform_500": [
            {"name": "Box", "l": 1000, "w": 1000, "h": 1000, "wt": 5, "qty": 500},
        ],
        "small_1000": [
            {"name": "Mini", "l": 500, "w": 400, "h": 300, "wt": 1, "qty": 1000},
        ],
    }[case]
    return product_rows_to_cargo_items(products)


def run_once(case, config, label):
    items = make_items(case)
    tracemalloc.reset_peak()
    tracemalloc.start()
    t0 = time.perf_counter()
    c0 = time.process_time()
    packed, leftover = pack_container(CONTAINER, items, config=config)
    cpu = time.process_time() - c0
    wall = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    d = packed.diagnostics
    print(
        f"  {label:6s} | wall {wall:6.3f}s | cpu {cpu:6.3f}s | "
        f"packed {len(packed.items):4d} | leftover {len(leftover):4d} | "
        f"util {d.get('utilization_pct', 0):5.2f}% | "
        f"support {d.get('support_ratio', 0):.3f} | "
        f"cand {d.get('candidate_count', 0)} | full {d.get('full_scores', 0)} | "
        f"peak {peak/1e6:.1f}MB"
    )
    return dict(case=case, mode=label, wall=wall, cpu=cpu,
                packed=len(packed.items), leftover=len(leftover),
                util=d.get("utilization_pct", 0), diag=d)


CASES = ["mixed_160", "uniform_100", "small_200"]

if __name__ == "__main__":
    args = sys.argv[1:]
    cases = [a for a in args if a in CASES + ["uniform_500", "small_1000"]] or CASES
    mode = "BEFORE" if "before" in args else "AFTER" if "after" in args else "BOTH"
    for case in cases:
        print(f"=== {case} ===")
        if mode in ("BEFORE", "BOTH"):
            run_once(case, BEFORE, "BEFORE")
        if mode in ("AFTER", "BOTH"):
            run_once(case, AFTER, "AFTER")
