"""Regression and correctness tests for the container loading engine.

Run with:
    python -m unittest test_engine -v
"""
import sys
import types
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
pkg = types.ModuleType("container_optimizer")
pkg.__path__ = [str(APP_DIR)]
sys.modules["container_optimizer"] = pkg

from container_optimizer.cargo import product_rows_to_cargo_items
from container_optimizer.models import CargoItem, ContainerSpec, LoadingConfig
from container_optimizer.packing import (
    _cargo_orientations,
    _overlap_1d,
    _placement_signature,
    pack_container,
)
from container_optimizer.optimization import optimize_loading

CONTAINER = ContainerSpec("40' High-Cube (40HQ)", 12032, 2352, 2698, 28000)
SMALL_CONTAINER = ContainerSpec("20ft", 5898, 2352, 2698, 28000)

AUTO = LoadingConfig(mode="auto")
MANUAL = LoadingConfig(
    mode="manual",
    packing_sequence="largest_volume_first",
    placement_strategy="bottom_left_fill",
    packing_pattern="balanced",
    enable_searates_scoring=False,
    enable_repacking=False,
    adaptive_search=False,
    time_budget_seconds=10.0,
)


def make_items(specs):
    """specs: list of (l, w, h, wt, qty) or product dict rows."""
    products = []
    for i, spec in enumerate(specs):
        if isinstance(spec, dict):
            products.append(spec)
        else:
            l, w, h, wt, qty = spec
            products.append({"name": f"C{i}", "l": l, "w": w, "h": h, "wt": wt, "qty": qty})
    return product_rows_to_cargo_items(products)


def boxes_overlap(a, b):
    ax, ay, az = a.position
    bx, by, bz = b.position
    adx, ady, adz = a.size
    bdx, bdy, bdz = b.size
    return (
        _overlap_1d(ax, adx, bx, bdx) > 1e-6
        and _overlap_1d(ay, ady, by, bdy) > 1e-6
        and _overlap_1d(az, adz, bz, bdz) > 1e-6
    )


class TestValidity(unittest.TestCase):
    """Physical validity: bounds, collisions, weight, support, no duplicates."""

    def _check_valid(self, packed, container=CONTAINER):
        ids = set()
        total_weight = 0.0
        for item in packed.items:
            self.assertNotIn(item.cargo.id, ids, "duplicate carton placed")
            ids.add(item.cargo.id)
            x, y, z = item.position
            dx, dy, dz = item.size
            self.assertGreaterEqual(x, -1e-6)
            self.assertGreaterEqual(y, -1e-6)
            self.assertGreaterEqual(z, -1e-6)
            self.assertLessEqual(x + dx, container.length_mm + 1e-6)
            self.assertLessEqual(y + dy, container.width_mm + 1e-6)
            self.assertLessEqual(z + dz, container.height_mm + 1e-6)
            self.assertGreater(min(dx, dy, dz), 0)
            total_weight += item.cargo.weight_kg
            for other in packed.items:
                if other is item:
                    continue
                self.assertFalse(boxes_overlap(item, other),
                                 f"overlap: {item.cargo.id} vs {other.cargo.id}")
            if z > 1e-6 and not item.cargo.overhang_allowed:
                base = dx * dy
                supported = 0.0
                for other in packed.items:
                    if other is item:
                        continue
                    ox, oy, oz = other.position
                    odx, ody, odz = other.size
                    if abs(oz + odz - z) > 1e-6:
                        continue
                    supported += _overlap_1d(x, dx, ox, odx) * _overlap_1d(y, dy, oy, ody)
                self.assertGreaterEqual(
                    supported / max(base, 1.0), 0.70 - 1e-6,
                    f"support ratio violated for {item.cargo.id}")
        self.assertLessEqual(total_weight, container.max_weight_kg + 1e-6)
        return ids

    def test_mixed_valid(self):
        items = make_items([(1200, 800, 1000, 10, 60), (600, 400, 400, 2, 100)])
        packed, leftover = pack_container(CONTAINER, items, config=AUTO)
        ids = self._check_valid(packed)
        leftover_ids = {item.id for item in leftover}
        all_ids = {item.id for item in items}
        self.assertFalse(ids & leftover_ids, "carton both packed and leftover")
        self.assertEqual(ids | leftover_ids, all_ids, "cartons lost or duplicated")

    def test_uniform_valid(self):
        items = make_items([(1000, 1000, 1000, 5, 100)])
        packed, leftover = pack_container(CONTAINER, items, config=AUTO)
        self._check_valid(packed)
        self.assertEqual(len(packed.items) + len(leftover), 100)

    def test_small_valid(self):
        items = make_items([(500, 400, 300, 1, 200)])
        packed, leftover = pack_container(CONTAINER, items, config=AUTO)
        self._check_valid(packed)
        self.assertEqual(len(packed.items) + len(leftover), 200)

    def test_manual_valid(self):
        items = make_items([(1200, 800, 1000, 10, 60), (600, 400, 400, 2, 100)])
        packed, leftover = pack_container(CONTAINER, items, config=MANUAL)
        self._check_valid(packed)

    def test_weight_capacity_respected(self):
        # 40 items x 800kg = 32000kg > 28000kg max -> at most 35 can pack.
        items = make_items([(1000, 1000, 1000, 800, 40)])
        packed, leftover = pack_container(CONTAINER, items, config=AUTO)
        total = sum(i.cargo.weight_kg for i in packed.items)
        self.assertLessEqual(total, CONTAINER.max_weight_kg + 1e-6)
        self.assertGreater(len(leftover), 0)


class TestOrientations(unittest.TestCase):
    def test_all_orientations_box(self):
        cargo = CargoItem("a", "a", 100, 200, 300, 1)
        self.assertEqual(len(_cargo_orientations(cargo)), 6)

    def test_symmetric_dedup(self):
        cargo = CargoItem("a", "a", 100, 100, 100, 1)
        self.assertEqual(len(_cargo_orientations(cargo)), 1)
        cargo = CargoItem("b", "b", 100, 100, 300, 1)
        self.assertEqual(len(_cargo_orientations(cargo)), 3)

    def test_tilt_restricts(self):
        cargo = CargoItem("a", "a", 100, 200, 300, 1,
                          tilt_to_length=False, tilt_to_width=False)
        # no tilt flags => all rotations allowed
        self.assertEqual(len(_cargo_orientations(cargo)), 6)
        # tilting allowed only to length axis
        cargo2 = CargoItem("b", "b", 100, 200, 300, 1,
                           tilt_to_length=True, tilt_to_width=False)
        self.assertEqual(len(_cargo_orientations(cargo2)), 2)

    def test_oversized_not_packed(self):
        items = make_items([(13000, 2000, 2000, 10, 2), (500, 400, 300, 1, 3)])
        packed, leftover = pack_container(CONTAINER, items, config=AUTO)
        placed_dims = {tuple(sorted(i.size)) for i in packed.items}
        self.assertNotIn((2000.0, 2000.0, 13000.0), placed_dims)
        self.assertEqual(len(leftover), 2)


class TestStacking(unittest.TestCase):
    def test_disable_stacking_stays_on_floor(self):
        items = [
            CargoItem(f"ns{i}", "ns", 500, 400, 300, 1, disable_stacking=True)
            for i in range(200)
        ]
        packed, _ = pack_container(CONTAINER, items, config=AUTO)
        for item in packed.items:
            self.assertLessEqual(item.position[2], 1e-6,
                                 "disable_stacking item above floor")

    def test_max_layers(self):
        items = [
            CargoItem(f"l{i}", "l", 500, 400, 300, 1, max_layers=2)
            for i in range(300)
        ]
        packed, _ = pack_container(CONTAINER, items, config=AUTO)
        for item in packed.items:
            self.assertLess(item.position[2], 2 * 300 - 1e-6,
                            "max_layers exceeded")


class TestDeterminism(unittest.TestCase):
    def test_same_input_same_output(self):
        items = make_items([(1200, 800, 1000, 10, 30), (600, 400, 400, 2, 50)])
        a, la = pack_container(CONTAINER, list(items), config=AUTO)
        b, lb = pack_container(CONTAINER, list(items), config=AUTO)
        sig_a = sorted((i.cargo.id, i.position, i.size) for i in a.items)
        sig_b = sorted((i.cargo.id, i.position, i.size) for i in b.items)
        self.assertEqual(sig_a, sig_b)
        self.assertEqual([i.id for i in la], [i.id for i in lb])


class TestSignatures(unittest.TestCase):
    def test_identical_items_share_signature(self):
        a = CargoItem("a", "x", 100, 200, 300, 5)
        b = CargoItem("b", "y", 100, 200, 300, 5)
        self.assertEqual(_placement_signature(a), _placement_signature(b))

    def test_constraints_distinguish(self):
        a = CargoItem("a", "x", 100, 200, 300, 5)
        b = CargoItem("b", "y", 100, 200, 300, 5, disable_stacking=True)
        self.assertNotEqual(_placement_signature(a), _placement_signature(b))


class TestTimeoutAndRecovery(unittest.TestCase):
    def test_timeout_keeps_valid_partial_layout(self):
        items = make_items([(1000, 1000, 1000, 5, 100)])
        cfg = LoadingConfig(mode="manual", time_budget_seconds=0.001,
                            packing_sequence="largest_volume_first",
                            placement_strategy="bottom_left_fill")
        packed, leftover = pack_container(CONTAINER, items, config=cfg)
        ids = {i.cargo.id for i in packed.items}
        self.assertEqual(len(ids), len(packed.items))
        self.assertEqual(len(packed.items) + len(leftover), 100)
        for item in packed.items:
            x, y, z = item.position
            dx, dy, dz = item.size
            self.assertLessEqual(x + dx, CONTAINER.length_mm + 1e-6)

    def test_repack_never_loses_packed_items(self):
        items = make_items([(1200, 800, 1000, 10, 60), (600, 400, 400, 2, 100)])
        no_repack = LoadingConfig(
            mode="manual", packing_sequence="largest_volume_first",
            placement_strategy="bottom_left_fill", packing_pattern="balanced",
            enable_searates_scoring=False, enable_repacking=False,
            adaptive_search=False, time_budget_seconds=10.0)
        with_repack = LoadingConfig(
            mode="manual", packing_sequence="largest_volume_first",
            placement_strategy="bottom_left_fill", packing_pattern="balanced",
            enable_searates_scoring=True, enable_repacking=True,
            adaptive_search=True, time_budget_seconds=15.0)
        p1, _ = pack_container(CONTAINER, make_items([(1200, 800, 1000, 10, 60), (600, 400, 400, 2, 100)]), config=no_repack)
        p2, _ = pack_container(CONTAINER, make_items([(1200, 800, 1000, 10, 60), (600, 400, 400, 2, 100)]), config=with_repack)
        self.assertGreaterEqual(len(p2.items), len(p1.items) - 2)


class TestMultiContainer(unittest.TestCase):
    def test_completion_verified_flag(self):
        items = make_items([(1000, 1000, 1000, 5, 10)])
        packed, leftover = pack_container(CONTAINER, items, config=AUTO)
        self.assertTrue(getattr(packed, "completion_verified", False))
        self.assertEqual(len(leftover), 0)

    def test_full_plan_no_duplicates(self):
        items = make_items([(1000, 1000, 1000, 5, 100)])
        plan = optimize_loading(items, CONTAINER, selected_quantity=1,
                                allow_auto_add=True, config=AUTO)
        placed = [i.cargo.id for c in plan.containers for i in c.items]
        self.assertEqual(len(placed), len(set(placed)), "duplicate cartons in plan")
        leftover_ids = {i.id for i in plan.leftover_items}
        self.assertEqual(len(placed) + len(leftover_ids), 100)
        self.assertGreaterEqual(len(plan.containers), 1)


class TestDiagnostics(unittest.TestCase):
    def test_diagnostics_present(self):
        items = make_items([(500, 400, 300, 1, 20)])
        packed, _ = pack_container(CONTAINER, items, config=AUTO)
        d = packed.diagnostics
        for key in ("utilization_pct", "weight_utilization_pct", "empty_space_m3",
                    "number_of_layers", "support_ratio", "center_of_gravity",
                    "stability_score", "packing_score", "runtime_s",
                    "candidate_count", "full_scores"):
            self.assertIn(key, d)
        self.assertGreater(d["utilization_pct"], 0)


class TestCargoItemInit(unittest.TestCase):
    def test_allow_overhang_alias(self):
        # Regression: CargoItem should accept the `allow_overhang` keyword
        # used by some UI/data sources and map it to `overhang_allowed`.
        c = CargoItem("a", "a", 100, 200, 300, 1, allow_overhang=True)
        self.assertTrue(c.overhang_allowed)

    def test_overhang_allowed_preserved_when_no_alias(self):
        c = CargoItem("a", "a", 100, 200, 300, 1, overhang_allowed=True)
        self.assertTrue(c.overhang_allowed)

    def test_product_rows_accept_allow_overhang_key(self):
        items = product_rows_to_cargo_items([{
            "name": "x", "l": 100, "w": 100, "h": 100, "wt": 1, "qty": 1,
            "allow_overhang": True,
        }])
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].overhang_allowed)


if __name__ == "__main__":
    unittest.main()
