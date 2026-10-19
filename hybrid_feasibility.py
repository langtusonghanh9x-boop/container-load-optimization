"""Incumbent-preserving hybrid search feasibility benchmark.

Run with:
    python hybrid_feasibility.py

Outputs:
    hybrid_feasibility.json
    hybrid_feasibility.txt
"""
import sys
import types
import time
import json
import math
from dataclasses import replace
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
pkg = types.ModuleType("container_optimizer")
pkg.__path__ = [str(APP_DIR)]
sys.modules["container_optimizer"] = pkg

from container_optimizer.cargo import product_rows_to_cargo_items
from container_optimizer.models import ContainerSpec, LoadingConfig
from container_optimizer.packing import (
    pack_container, repack_container, beam_search_pack,
    _rebuild_completion_state, _layout_score, sort_items_for_loading,
    _auto_derive_config,
)

import test_adversarial as ta

CONTAINER = ContainerSpec("40' High-Cube (40HQ)", 12032, 2352, 2698, 28000)
MINI = ContainerSpec("Mini-2.5x1.5x1.5", 2500, 1500, 1500, 10000)


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


def _util(packed, container):
    if not packed:
        return 0.0
    got = sum(i.size[0] * i.size[1] * i.size[2] for i in packed)
    return got / max(container.length_mm * container.width_mm * container.height_mm, 1.0) * 100.0


def make_large(case):
    if case == "mixed_160":
        products = [
            {"name": "Big", "l": 1200, "w": 800, "h": 1000, "wt": 10, "qty": 60},
            {"name": "Small", "l": 600, "w": 400, "h": 400, "wt": 2, "qty": 100},
        ]
    elif case == "mixed_500":
        products = [
            {"name": "A", "l": 1200, "w": 800, "h": 1000, "wt": 8, "qty": 100},
            {"name": "B", "l": 900, "w": 700, "h": 700, "wt": 6, "qty": 100},
            {"name": "C", "l": 700, "w": 500, "h": 500, "wt": 4, "qty": 100},
            {"name": "D", "l": 500, "w": 400, "h": 300, "wt": 2, "qty": 100},
            {"name": "E", "l": 300, "w": 200, "h": 200, "wt": 1, "qty": 100},
        ]
    elif case == "mixed_2000":
        products = [
            {"name": "A", "l": 1200, "w": 800, "h": 1000, "wt": 8, "qty": 400},
            {"name": "B", "l": 900, "w": 700, "h": 700, "wt": 6, "qty": 400},
            {"name": "C", "l": 700, "w": 500, "h": 500, "wt": 4, "qty": 400},
            {"name": "D", "l": 500, "w": 400, "h": 300, "wt": 2, "qty": 400},
            {"name": "E", "l": 300, "w": 200, "h": 200, "wt": 1, "qty": 400},
        ]
    else:
        raise ValueError(case)
    return product_rows_to_cargo_items(products)


def hybrid_pack(container, items, config, tail_sizes=(1, 2, 3, 5, 8), enabled=False):
    """BASE incumbent + selective, quality-gated beam search.

    - Default ``enabled=False`` keeps baseline behavior unchanged.
    - Trigger only when there are leftovers, the baseline is exhausted (not
      deadline), the remaining search space is small, and time remains.
    - Quality Gate: only accept a candidate if it is valid, does not reduce
      packed count, and does not degrade support/stability/util when packed
      count is equal.
    """
    from time import monotonic
    budget = float(getattr(config, "time_budget_seconds", 60.0))
    deadline = monotonic() + budget
    total = len(items)

    # 1. Incumbent from the standard engine (repack_container returns the best
    # layout already found by vehicle completion + tail repacking).
    # Derive the same auto-config that pack_container uses so the baseline is
    # identical.
    derived_cfg = _auto_derive_config(config or LoadingConfig(), items)
    base_layout, base_leftover = repack_container(container, items, derived_cfg)
    base_count = len(base_layout.items)
    base_score = _layout_score(base_layout, container, total, derived_cfg)
    base_sup, base_stab = _stability(base_layout.items, container)
    base_util = _util(base_layout.items, container)

    # Initialize safe default diagnostics on the base layout.
    base_layout.triggered = False
    base_layout.skip_reason = ""
    base_layout.improved = False
    base_layout.improved_primary = False
    base_layout.hybrid_trials = 0
    base_layout.candidates_generated = 0
    base_layout.candidates_valid = 0
    base_layout.candidates_accepted = 0
    base_layout.candidates_improved_primary = 0
    base_layout.candidates_rejected_quality = 0

    # 2. Opt-in isolation: when disabled, return the baseline unchanged.
    if not enabled:
        base_layout.skip_reason = "disabled"
        return base_layout, base_leftover

    # 3. Selective trigger evaluation.
    beam_limit = max(0, int(getattr(derived_cfg, "beam_carton_limit", 20)))
    first_tail = min(tail_sizes[0] if tail_sizes else 1, len(base_layout.items))
    first_tail_items = [it.cargo for it in base_layout.items[-first_tail:]]
    first_remaining = list(sort_items_for_loading(first_tail_items + list(base_leftover), derived_cfg))

    if not base_leftover:
        base_layout.skip_reason = "no_leftover"
        return base_layout, base_leftover
    if getattr(base_layout, "termination", "") != "exhausted":
        base_layout.skip_reason = f"termination_{getattr(base_layout, 'termination', 'unknown')}"
        return base_layout, base_leftover
    if len(first_remaining) > beam_limit:
        base_layout.skip_reason = "remaining_too_large"
        return base_layout, base_leftover
    if deadline - monotonic() < 1.0:
        base_layout.skip_reason = "insufficient_time"
        return base_layout, base_leftover

    # 4. Tail beam search if triggered.
    base_layout.triggered = True
    best_layout, best_leftover = base_layout, base_leftover
    best_count = base_count
    best_score = base_score
    best_sup, best_stab, best_util = base_sup, base_stab, base_util
    improved = False
    improved_primary = False
    trials = 0
    candidates_generated = 0
    candidates_valid = 0
    candidates_accepted = 0
    candidates_improved_primary = 0
    candidates_rejected_quality = 0

    for idx, tail in enumerate(tail_sizes):
        if monotonic() >= deadline or not base_layout.items:
            break
        effective_tail = min(tail, len(base_layout.items))
        trials += 1

        checkpoint = base_layout.history[:-effective_tail]
        state = _rebuild_completion_state(container, checkpoint)
        if state is None:
            continue

        tail_items = [it.cargo for it in base_layout.items[-effective_tail:]]
        remaining = list(sort_items_for_loading(tail_items + list(base_leftover), derived_cfg))
        if not remaining:
            continue

        time_left = max(0.0, deadline - monotonic())
        if time_left <= 0.1:
            break
        # Share the remaining time across the remaining tails, but never spend
        # more than 2 seconds on a single tail beam.
        per_trial_budget = min(2.0, time_left / max(1, len(tail_sizes) - idx))

        beam_cfg = replace(
            derived_cfg,
            beam_width=16,
            beam_carton_limit=min(20, len(remaining)),
            time_budget_seconds=per_trial_budget,
            enable_root_diversification=False,
            enable_repacking=False,
        )
        new_layout, new_leftover = beam_search_pack(container, remaining, beam_cfg, initial_state=state)
        candidates_generated += 1

        if not _valid(new_layout.items, container):
            continue
        candidates_valid += 1

        new_count = len(new_layout.items)
        new_score = _layout_score(new_layout, container, total, derived_cfg)
        new_sup, new_stab = _stability(new_layout.items, container)
        new_util = _util(new_layout.items, container)

        accepted = False
        primary = False
        # Primary objective: packed count. Never accept fewer items.
        if new_count > best_count:
            accepted = True
            primary = True
        elif new_count == best_count:
            # Quality Gate: mandatory quality metrics must not degrade; only
            # then can utilization improve.
            if (
                new_sup + 1e-6 < best_sup
                or new_stab + 1e-6 < best_stab
                or new_util + 1e-6 < best_util
            ):
                candidates_rejected_quality += 1
            elif new_util > best_util + 1e-6:
                accepted = True
                primary = True

        if accepted:
            best_layout, best_leftover = new_layout, new_leftover
            best_count = new_count
            best_score = new_score
            best_sup, best_stab, best_util = new_sup, new_stab, new_util
            candidates_accepted += 1
            improved = True
            if primary:
                candidates_improved_primary += 1
                improved_primary = True
            if best_count == total:
                break

    best_layout.improved = improved
    best_layout.improved_primary = improved_primary
    best_layout.hybrid_trials = trials
    best_layout.candidates_generated = candidates_generated
    best_layout.candidates_valid = candidates_valid
    best_layout.candidates_accepted = candidates_accepted
    best_layout.candidates_improved_primary = candidates_improved_primary
    best_layout.candidates_rejected_quality = candidates_rejected_quality
    best_layout.triggered = True
    best_layout.skip_reason = ""
    if not improved:
        best_layout.score = base_score
    return best_layout, best_leftover


def run_base(container, items, label, time_budget):
    cfg = LoadingConfig(time_budget_seconds=time_budget)
    t0 = time.perf_counter()
    c0 = time.process_time()
    packed, leftover = pack_container(container, items, config=cfg)
    wall = time.perf_counter() - t0
    cpu = time.process_time() - c0
    got_vol = sum(i.size[0] * i.size[1] * i.size[2] for i in packed.items)
    capacity = container.length_mm * container.width_mm * container.height_mm
    sup, stab = _stability(packed.items, container)
    return {
        "label": label,
        "mode": "BASE",
        "wall_s": round(wall, 3),
        "cpu_s": round(cpu, 3),
        "packed": len(packed.items),
        "leftover": len(leftover),
        "util_pct": round(got_vol / max(capacity, 1.0) * 100, 2),
        "support": round(sup, 4),
        "stability": round(stab, 4),
        "termination": packed.diagnostics.get("search_termination", "unknown"),
        "valid": _valid(packed.items, container),
    }


def run_hybrid(container, items, label, time_budget, enabled=True, mode_name="HYBRID"):
    cfg = LoadingConfig(time_budget_seconds=time_budget)
    t0 = time.perf_counter()
    c0 = time.process_time()
    layout, leftover = hybrid_pack(container, items, cfg, enabled=enabled)
    wall = time.perf_counter() - t0
    cpu = time.process_time() - c0
    got_vol = sum(i.size[0] * i.size[1] * i.size[2] for i in layout.items)
    capacity = container.length_mm * container.width_mm * container.height_mm
    sup, stab = _stability(layout.items, container)
    # With the Quality Gate the only meaningful "improved" is primary objective.
    improved = getattr(layout, "improved_primary", False)
    return {
        "label": label,
        "mode": mode_name,
        "wall_s": round(wall, 3),
        "cpu_s": round(cpu, 3),
        "packed": len(layout.items),
        "leftover": len(leftover),
        "util_pct": round(got_vol / max(capacity, 1.0) * 100, 2),
        "support": round(sup, 4),
        "stability": round(stab, 4),
        "termination": layout.termination,
        "improved": improved,
        "triggered": getattr(layout, "triggered", False),
        "skip_reason": getattr(layout, "skip_reason", ""),
        "trials": getattr(layout, "hybrid_trials", 0),
        "candidates_generated": getattr(layout, "candidates_generated", 0),
        "candidates_valid": getattr(layout, "candidates_valid", 0),
        "candidates_accepted": getattr(layout, "candidates_accepted", 0),
        "candidates_improved_primary": getattr(layout, "candidates_improved_primary", 0),
        "candidates_rejected_quality": getattr(layout, "candidates_rejected_quality", 0),
        "valid": _valid(layout.items, container),
    }


def main():
    rows = []

    # Small seeds
    for seed in (2111, 2138):
        items = ta.TestSyntheticRealistic()._gen(seed, n_skus=8, qty_lo=1, qty_hi=1)
        exp_count, _ = ta.exhaustive_best(MINI, items, node_budget=30_000_000)
        for r in (run_base(MINI, items, f"seed{seed}", 3.0),
                  run_hybrid(MINI, items, f"seed{seed}", 3.0, enabled=True, mode_name="HYBRID")):
            r["exhaustive_count"] = int(exp_count)
            rows.append(r)

    # Tiny adversarial cases
    tiny_cases = [
        ("tiny_uniform", ta.SMALLER, ta.cargo(1000, 1000, 1000, 10, qty=5)),
        ("tiny_mixed", ta.SMALLER, ta.cargo(2000, 1000, 1000, 10, 2) + ta.cargo(1000, 500, 500, 2, 4)),
        ("tiny_constrained", ta.SMALLER, ta.cargo(1500, 1000, 800, 10, 2, disable_stacking=True) + ta.cargo(500, 500, 500, 2, 3)),
        ("tiny_tilt", ta.SMALLER, ta.cargo(1900, 1200, 600, 5, 3, tilt_to_length=True)),
    ]
    for name, container, items in tiny_cases:
        exp_count, _ = ta.exhaustive_best(container, items, node_budget=30_000_000)
        for r in (run_base(container, items, name, 3.0),
                  run_hybrid(container, items, name, 3.0, enabled=True, mode_name="HYBRID")):
            r["exhaustive_count"] = int(exp_count)
            rows.append(r)

    # Large mixed on 40HQ
    for case in ("mixed_160", "mixed_500", "mixed_2000"):
        items = make_large(case)
        for r in (run_base(CONTAINER, items, case, 15.0),
                  run_hybrid(CONTAINER, items, case, 15.0, enabled=True, mode_name="HYBRID")):
            r["exhaustive_count"] = None
            rows.append(r)

    with open("hybrid_feasibility.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)

    with open("hybrid_feasibility.txt", "w", encoding="utf-8") as f:
        f.write(
            f"{'Label':<18} {'Mode':<10} {'Wall':>6} {'CPU':>6} {'Pack':>6} {'Left':>6} "
            f"{'Util%':>7} {'Supp':>5} {'Stab':>5} {'Term':<10} {'Valid':>5} {'Impr':>6} "
            f"{'Trig':>6} {'Reason':<18} {'Trials':>6} {'Gen':>5} {'Acc':>5} {'Rej':>5} {'ImpP':>5}\n"
        )
        f.write("-" * 170 + "\n")
        for r in rows:
            f.write(
                f"{r['label']:<18} {r['mode']:<10} {r['wall_s']:>6.2f} {r['cpu_s']:>6.2f} "
                f"{r['packed']:>6} {r['leftover']:>6} {r['util_pct']:>7.2f} "
                f"{r['support']:>5.3f} {r['stability']:>5.3f} {r['termination']:<10} {str(r['valid']):>5} "
                f"{str(r.get('improved', False)):>6} {str(r.get('triggered', False)):>6} "
                f"{r.get('skip_reason', ''):<18} {r.get('trials', 0):>6} "
                f"{r.get('candidates_generated', 0):>5} {r.get('candidates_accepted', 0):>5} "
                f"{r.get('candidates_rejected_quality', 0):>5} {r.get('candidates_improved_primary', 0):>5}\n"
            )

    print("\n" + "=" * 170)
    with open("hybrid_feasibility.txt", encoding="utf-8") as f:
        print(f.read())
    print("Saved: hybrid_feasibility.json, hybrid_feasibility.txt")


if __name__ == "__main__":
    main()
