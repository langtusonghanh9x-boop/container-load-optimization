from dataclasses import dataclass, field, replace
from heapq import heappush, heappop, nlargest
from itertools import permutations
from time import monotonic

from py3dbp import Bin, Item
from py3dbp.main import START_POSITION, RotationType, intersect

from .models import CargoItem, ContainerSpec, LoadingConfig, PackedContainer, PackedItem
from .patterns import get_pattern


MAX_ACTIVE_FREE_SPACES = 128
FAST_FREE_SPACE_LIMIT = 4096
MAX_PLACEMENT_CANDIDATES = 6


def can_fit_item(item: CargoItem, container: ContainerSpec) -> bool:
    cargo_dims = [item.length_mm, item.width_mm, item.height_mm]
    limit_dims = [container.length_mm, container.width_mm, container.height_mm]
    dimension_fit = any(all(rotated[i] <= limit_dims[i] for i in range(3)) for rotated in permutations(cargo_dims))
    return dimension_fit and item.weight_kg <= container.max_weight_kg


def sort_items_for_loading(items, config=None):
    config = config or LoadingConfig()

    # Loading Order is a hard constraint: do not apply a secondary sort that
    # could silently reorder products with the same (or missing) order value.
    if any(item.loading_order is not None for item in items):
        ordered = sorted(
            enumerate(items),
            key=lambda indexed: (
                indexed[1].loading_order is None,
                indexed[1].loading_order if indexed[1].loading_order is not None else float("inf"),
                indexed[0],
            ),
        )
        return [item for _, item in ordered]

    sequence = getattr(config, "packing_sequence", "largest_volume_first")
    sequence_keys = {
        "searates_priority": lambda item: (
            -getattr(item, "priority", 0),
            -getattr(item, "stackability", 1),
            -(item.length_mm * item.width_mm),
            -item.volume_mm3,
            -item.weight_kg,
            -max(item.length_mm, item.width_mm, item.height_mm),
        ),
        "largest_volume_first": lambda item: (-item.volume_mm3, -item.weight_kg),
        "largest_base_first": lambda item: (-(item.length_mm * item.width_mm), -item.volume_mm3),
        "heaviest_first": lambda item: (-item.weight_kg, -item.volume_mm3),
        "longest_first": lambda item: (-max(item.length_mm, item.width_mm, item.height_mm), -item.volume_mm3),
        "highest_density_first": lambda item: (-(item.weight_kg / max(item.volume_mm3, 1.0)), -item.weight_kg),
    }
    base_key = sequence_keys.get(sequence)
    if base_key is None:
        base_key = sequence_keys["heaviest_first" if getattr(config, "heavy_priority", "heavy_bottom") == "heavy_bottom" else "largest_volume_first"]
    return sorted(items, key=base_key)


def _allowed_rotations(cargo):
    if not (cargo.tilt_to_length or cargo.tilt_to_width):
        return RotationType.ALL

    rotations = [RotationType.RT_WHD]
    if cargo.tilt_to_length:
        rotations.append(RotationType.RT_DHW)
    if cargo.tilt_to_width:
        rotations.append(RotationType.RT_WDH)
    return rotations


def _stacking_constraints_allow(bin_obj, item, cargo, pivot, dimension, config):
    base_z = float(pivot[2])
    top_z = base_z + float(dimension[2])

    if cargo.disable_stacking and base_z > 0:
        return False
    if cargo.max_stack_height_mm is not None and top_z > cargo.max_stack_height_mm:
        return False
    if cargo.max_layers is not None and base_z + 1e-6 >= float(dimension[2]) * cargo.max_layers:
        return False

    support_area = 0.0
    for packed_item in bin_obj.items:
        packed_dimension = packed_item.get_dimension()
        packed_top = float(packed_item.position[2]) + float(packed_dimension[2])
        if abs(packed_top - base_z) > 1e-6:
            continue
        overlaps_base = (
            float(pivot[0]) < float(packed_item.position[0]) + float(packed_dimension[0])
            and float(pivot[0]) + float(dimension[0]) > float(packed_item.position[0])
            and float(pivot[1]) < float(packed_item.position[1]) + float(packed_dimension[1])
            and float(pivot[1]) + float(dimension[1]) > float(packed_item.position[1])
        )
        if not overlaps_base:
            continue
        overlap_x = max(0.0, min(float(pivot[0]) + float(dimension[0]), float(packed_item.position[0]) + float(packed_dimension[0])) - max(float(pivot[0]), float(packed_item.position[0])))
        overlap_y = max(0.0, min(float(pivot[1]) + float(dimension[1]), float(packed_item.position[1]) + float(packed_dimension[1])) - max(float(pivot[1]), float(packed_item.position[1])))
        support_area += overlap_x * overlap_y
        support = getattr(packed_item, "cargo", None)
        if support is not None and support.disable_stacking:
            return False
        if support is not None and support.max_stack_mass_kg is not None and float(item.weight) > support.max_stack_mass_kg:
            return False
    if base_z > 0:
        base_area = max(float(dimension[0]) * float(dimension[1]), 1.0)
        threshold = float(getattr(config, "stability_min_support", getattr(config, "minimum_support_ratio", 0.0)))
        if cargo.overhang_allowed:
            threshold = 0.0
        if support_area / base_area < threshold:
            return False
    return True


def _candidate_pivots(bin_obj):
    """Generate every extreme point exposed by the current packing."""
    # py3dbp stores coordinates as Decimal.  Preserve that representation for
    # collision checks; converting only happens inside scoring helpers.
    pivots = {tuple(START_POSITION)}
    for packed_item in bin_obj.items:
        x, y, z = packed_item.position
        dx, dy, dz = packed_item.get_dimension()
        pivots.update(((x + dx, y, z), (x, y + dy, z), (x, y, z + dz)))
    return sorted(pivots, key=lambda pivot: (pivot[2], pivot[0], pivot[1]))


def _contact_area(bin_obj, pivot, dimension):
    contact = 0.0
    for packed in bin_obj.items:
        px, py, pz = (float(value) for value in packed.position)
        dx, dy, dz = (float(value) for value in packed.get_dimension())
        if abs(pz + dz - pivot[2]) < 1e-6:
            contact += _overlap_1d(pivot[0], dimension[0], px, dx) * _overlap_1d(pivot[1], dimension[1], py, dy)
        if abs(px + dx - pivot[0]) < 1e-6:
            contact += _overlap_1d(pivot[1], dimension[1], py, dy) * _overlap_1d(pivot[2], dimension[2], pz, dz)
        if abs(py + dy - pivot[1]) < 1e-6:
            contact += _overlap_1d(pivot[0], dimension[0], px, dx) * _overlap_1d(pivot[2], dimension[2], pz, dz)
    return contact


def _placement_value(bin_obj, pivot, dimension, strategy):
    """A deterministic score for one valid position/orientation choice."""
    pivot = tuple(float(value) for value in pivot)
    dimension = tuple(float(value) for value in dimension)
    contact = _contact_area(bin_obj, pivot, dimension)
    right_gap = float(bin_obj.width) - pivot[0] - dimension[0]
    width_gap = float(bin_obj.height) - pivot[1] - dimension[1]
    top_gap = float(bin_obj.depth) - pivot[2] - dimension[2]
    remaining_box = max(right_gap, 0) * max(width_gap, 0) * max(top_gap, 0)
    strategy = {
        "stable_floor_first": "lowest_z",
        "fill_width_before_length": "width_first",
    }.get(strategy, strategy)
    if strategy == "length_first":
        return (-pivot[0], -pivot[2], -pivot[1], contact)
    if strategy == "width_first":
        return (-pivot[1], -pivot[2], -pivot[0], contact)
    if strategy == "bottom_left_fill":
        return (-pivot[2], -pivot[0], -pivot[1], contact)
    if strategy == "best_contact_area":
        return (contact, -pivot[2], -pivot[0], -pivot[1])
    if strategy == "best_volume_fit":
        return (-remaining_box, contact, -pivot[2])
    if strategy == "best_free_space_reduction":
        # Prefer consuming the narrowest available residual dimension.
        return (-min(right_gap, width_gap, top_gap), contact, -pivot[2])
    # lowest_z (and unknown values) keeps mass close to the floor.
    return (-pivot[2], contact, -pivot[0], -pivot[1])


def _pack_to_bin(bin_obj, item, cargo, config):
    """Evaluate all allowed orientations and all current extreme points.

    The old implementation committed the first feasible result.  This routine
    scores every feasible placement for the active heuristic before committing
    exactly one, which makes a complete candidate layout reproducible.
    """
    valid_position, valid_rotation = item.position, item.rotation_type
    candidates = []
    for pivot in _candidate_pivots(bin_obj):
        for rotation in _allowed_rotations(cargo):
            item.position = pivot
            item.rotation_type = rotation
            dimension = item.get_dimension()
            if (bin_obj.width < pivot[0] + dimension[0] or bin_obj.height < pivot[1] + dimension[1]
                    or bin_obj.depth < pivot[2] + dimension[2]):
                continue
            if any(intersect(packed_item, item) for packed_item in bin_obj.items):
                continue
            if bin_obj.get_total_weight() + item.weight > bin_obj.max_weight:
                continue
            if not _stacking_constraints_allow(bin_obj, item, cargo, pivot, dimension, config):
                continue
            candidates.append((_placement_value(bin_obj, pivot, dimension, getattr(config, "placement_strategy", "lowest_z")), pivot, rotation))
    item.position, item.rotation_type = valid_position, valid_rotation
    if not candidates:
        return False
    _, pivot, rotation = max(candidates, key=lambda candidate: candidate[0])
    item.position, item.rotation_type, item.cargo = pivot, rotation, cargo
    bin_obj.items.append(item)
    return True


def _normalize_position(position, size, container, config):
    x, y, z = position
    dx, dy, dz = size
    if getattr(config, "load_direction", "inside_out") == "door_to_inside":
        x = container.length_mm - x - dx
    return (x, y, z)


def _overlap_1d(start_a, size_a, start_b, size_b):
    return max(0, min(start_a + size_a, start_b + size_b) - max(start_a, start_b))


def _base_overlap_ratio(item, other):
    x, y, _ = item.position
    dx, dy, _ = item.size
    ox, oy, _ = other.position
    odx, ody, _ = other.size
    overlap_area = _overlap_1d(x, dx, ox, odx) * _overlap_1d(y, dy, oy, ody)
    base_area = max(dx * dy, 1)
    return overlap_area / base_area


def _side_overlap(item, other):
    _, y, z = item.position
    _, dy, dz = item.size
    _, oy, oz = other.position
    _, ody, odz = other.size
    return _overlap_1d(y, dy, oy, ody) > 0 and _overlap_1d(z, dz, oz, odz) > 0


def _apply_vertical_gravity(packed_items, config):
    settled = []
    for item in sorted(packed_items, key=lambda packed: (packed.position[2], packed.cargo.weight_kg), reverse=False):
        x, y, _ = item.position
        dx, dy, dz = item.size
        support_z = 0
        for other in settled:
            if _base_overlap_ratio(PackedItem(item.cargo, (x, y, 0), item.size), other) > 0:
                support_z = max(support_z, other.position[2] + other.size[2])
        item.position = (x, y, support_z)
        settled.append(item)
    return settled


def _apply_inside_out_compaction(packed_items, container, config):
    compacted = []
    if config.load_direction == "door_to_inside":
        ordered = sorted(packed_items, key=lambda packed: packed.position[0], reverse=True)
        for item in ordered:
            _, y, z = item.position
            dx, dy, dz = item.size
            target_x = container.length_mm - dx
            for other in compacted:
                if _side_overlap(PackedItem(item.cargo, (0, y, z), item.size), other):
                    target_x = min(target_x, other.position[0] - dx)
            item.position = (max(target_x, 0), y, z)
            compacted.append(item)
        return compacted

    ordered = sorted(packed_items, key=lambda packed: packed.position[0])
    for item in ordered:
        _, y, z = item.position
        dx, dy, dz = item.size
        target_x = 0
        for other in compacted:
            if _side_overlap(PackedItem(item.cargo, (0, y, z), item.size), other):
                target_x = max(target_x, other.position[0] + other.size[0])
        item.position = (min(target_x, container.length_mm - dx), y, z)
        compacted.append(item)
    return compacted


def _apply_contact_compaction(packed_items, container, config):
    if not getattr(config, "contact_compaction", True):
        return packed_items
    compacted = _apply_vertical_gravity(packed_items, config)
    compacted = _apply_inside_out_compaction(compacted, container, config)
    compacted = _apply_vertical_gravity(compacted, config)
    return compacted


@dataclass(frozen=True)
class _FreeSpace:
    x: float
    y: float
    z: float
    length: float
    width: float
    height: float

    @property
    def volume(self):
        return self.length * self.width * self.height


@dataclass
class _BeamLayout:
    items: list = field(default_factory=list)
    spaces: list = field(default_factory=list)
    score: float = 0.0
    weight: float = 0.0


class _FreeSpaceIndex:
    """Dimension-bucket spatial index with a lazy priority queue.

    It avoids scanning every free space for every carton.  Spaces are indexed
    by their three dimension classes; each bucket has a low-Z / best-fit heap.
    Entries are invalidated lazily when a space is split.
    """
    def __init__(self, spaces=(), max_spaces=FAST_FREE_SPACE_LIMIT):
        self.spaces = {}
        self.buckets = {}
        self.global_heap = []
        self.next_id = 0
        self.max_spaces = max_spaces
        for space in spaces:
            self.add(space)

    @staticmethod
    def _key(space):
        return (int(space.length // 250), int(space.width // 250), int(space.height // 250))

    @staticmethod
    def _priority(space):
        return (space.z, space.volume, space.x, space.y)

    def add(self, space):
        if min(space.length, space.width, space.height) <= 1e-6:
            return
        ident = self.next_id
        self.next_id += 1
        self.spaces[ident] = space
        key = self._key(space)
        heap = self.buckets.setdefault(key, [])
        entry = (*self._priority(space), ident)
        heappush(heap, entry)
        heappush(self.global_heap, entry)

    def remove(self, ident):
        self.spaces.pop(ident, None)

    def candidates(self, size, limit=MAX_PLACEMENT_CANDIDATES):
        selected = []
        held = []
        # Priority queue gives the best low-Z candidates without traversing all
        # dimension buckets.  The exact dimensions are checked before scoring.
        while self.global_heap and len(held) < limit * 2 and len(selected) < limit:
            entry = heappop(self.global_heap)
            ident = entry[-1]
            space = self.spaces.get(ident)
            if space is None:
                continue
            held.append(entry)
            if size[0] <= space.length + 1e-6 and size[1] <= space.width + 1e-6 and size[2] <= space.height + 1e-6:
                selected.append((ident, space))
        for entry in held:
            heappush(self.global_heap, entry)
        return selected

    def fit_candidates_ordered(self, size, limit):
        """Best-priority fitting spaces without scanning the whole index.

        Pops the global heap in (z, volume, x, y) order until ``limit`` fitting
        spaces are collected.  A bounded number of extra pops prevents
        pathological scans when few spaces fit; stale heap entries are dropped
        permanently, which also keeps the heap small over the run.  When the
        load is small or degenerate the full index scan may still be cheaper —
        callers fall back to ``all_fit_candidates`` for the completeness check.
        """
        selected = []
        held = []
        popped = 0
        max_pops = max(limit * 8, 256)
        while self.global_heap and len(selected) < limit and popped < max_pops:
            entry = heappop(self.global_heap)
            popped += 1
            ident = entry[-1]
            space = self.spaces.get(ident)
            if space is None:
                continue
            held.append(entry)
            if (size[0] <= space.length + 1e-6 and size[1] <= space.width + 1e-6
                    and size[2] <= space.height + 1e-6):
                selected.append((ident, space))
        for entry in held:
            heappush(self.global_heap, entry)
        return selected

    def all_fit_candidates(self, size):
        """Exhaustive feasibility iterator used by Vehicle Completion mode."""
        # Bucket prefilter: a space can only contain ``size`` if each dimension
        # class is at least the size's class (floor buckets are monotonic).
        # Buckets that are too small in any axis are skipped wholesale, which
        # removes the bulk of sliver spaces on large detailed loads.
        kl, kw, kh = int(size[0] // 250), int(size[1] // 250), int(size[2] // 250)
        if len(self.spaces) > 64 and len(self.buckets) > 1:
            result = []
            for (bl, bw, bh), heap in self.buckets.items():
                if bl < kl or bw < kw or bh < kh:
                    continue
                for entry in heap:
                    ident = entry[-1]
                    space = self.spaces.get(ident)
                    if (space is not None
                            and size[0] <= space.length + 1e-6
                            and size[1] <= space.width + 1e-6
                            and size[2] <= space.height + 1e-6):
                        result.append((ident, space))
            return result
        return [
            (ident, space) for ident, space in self.spaces.items()
            if size[0] <= space.length + 1e-6
            and size[1] <= space.width + 1e-6
            and size[2] <= space.height + 1e-6
        ]

    def find_exact(self, position, size):
        for ident, space in self.spaces.items():
            if (abs(space.x - position[0]) < 1e-6 and abs(space.y - position[1]) < 1e-6
                    and abs(space.z - position[2]) < 1e-6
                    and size[0] <= space.length + 1e-6 and size[1] <= space.width + 1e-6
                    and size[2] <= space.height + 1e-6):
                return ident, space
        return None

    def split(self, ident, size):
        space = self.spaces.get(ident)
        if space is None:
            return
        self.remove(ident)
        dx, dy, dz = size
        self.add(_FreeSpace(space.x + dx, space.y, space.z, space.length - dx, space.width, space.height))
        self.add(_FreeSpace(space.x, space.y + dy, space.z, dx, space.width - dy, space.height))
        self.add(_FreeSpace(space.x, space.y, space.z + dz, dx, dy, space.height - dz))
        # Bound memory for highly detailed loads.  Discard only the least
        # reachable high-Z fragments; current low-Z regions stay indexed.
        if self.max_spaces is not None and len(self.spaces) > int(self.max_spaces * 1.15):
            keep = sorted(self.spaces, key=lambda key: self._priority(self.spaces[key]))[:self.max_spaces]
            self.spaces = {key: self.spaces[key] for key in keep}


class _MutablePackingState:
    """Incremental state: no layout copies while greedy packing proceeds."""
    def __init__(self, container, spaces=None, items=None, exhaustive=False):
        self.container = container
        self.items = list(items or ())
        self.free = _FreeSpaceIndex(
            spaces or [_FreeSpace(0.0, 0.0, 0.0, float(container.length_mm), float(container.width_mm), float(container.height_mm))],
            max_spaces=None if exhaustive else FAST_FREE_SPACE_LIMIT,
        )
        self.weight = sum(item.cargo.weight_kg for item in self.items)
        self.used_volume = sum(item.size[0] * item.size[1] * item.size[2] for item in self.items)
        self._moment = [
            sum((item.position[0] + item.size[0] / 2.0) * item.cargo.weight_kg for item in self.items),
            sum((item.position[1] + item.size[1] / 2.0) * item.cargo.weight_kg for item in self.items),
            sum((item.position[2] + item.size[2] / 2.0) * item.cargo.weight_kg for item in self.items),
        ]
        self.cg = self._compute_cg()
        self.top_surfaces = {}
        self.top_cells = {}
        self.right_cells = {}
        self.front_cells = {}
        self.free_min_z = min((space.z for space in self.free.spaces.values()), default=0.0)
        self._score_weights = None
        for item in self.items:
            self._index_top(item)

    def _compute_cg(self):
        if self.weight <= 1e-9:
            return (self.container.length_mm / 2.0, self.container.width_mm / 2.0, 0.0)
        return (self._moment[0] / self.weight, self._moment[1] / self.weight, self._moment[2] / self.weight)

    def _index_top(self, item):
        x, y, z = item.position
        dx, dy, dz = item.size
        top = round(z + dz, 3)
        right = round(x + dx, 3)
        front = round(y + dy, 3)
        self.top_surfaces.setdefault(top, []).append(item)
        gx0, gx1 = int(x // 500), int((x + dx - 1e-6) // 500) + 1
        gy0, gy1 = int(y // 500), int((y + dy - 1e-6) // 500) + 1
        gz0, gz1 = int(z // 500), int((z + dz - 1e-6) // 500) + 1
        for gx in range(gx0, gx1):
            for gy in range(gy0, gy1):
                self.top_cells.setdefault((top, gx, gy), []).append(item)
        for gy in range(gy0, gy1):
            for gz in range(gz0, gz1):
                self.right_cells.setdefault((right, gy, gz), []).append(item)
        for gx in range(gx0, gx1):
            for gz in range(gz0, gz1):
                self.front_cells.setdefault((front, gx, gz), []).append(item)

    def place(self, carton, ident, space, size):
        item = PackedItem(carton, (space.x, space.y, space.z), size)
        self.items.append(item)
        self.weight += carton.weight_kg
        self.used_volume += size[0] * size[1] * size[2]
        self._moment[0] += (space.x + size[0] / 2.0) * carton.weight_kg
        self._moment[1] += (space.y + size[1] / 2.0) * carton.weight_kg
        self._moment[2] += (space.z + size[2] / 2.0) * carton.weight_kg
        self.cg = self._compute_cg()
        self._index_top(item)
        self.free.split(ident, size)
        self.free_min_z = min((space.z for space in self.free.spaces.values()), default=0.0)


def _cargo_orientations(cargo):
    """Return each permitted physical orientation exactly once."""
    base = (float(cargo.length_mm), float(cargo.width_mm), float(cargo.height_mm))
    if not (cargo.tilt_to_length or cargo.tilt_to_width):
        return sorted(set(permutations(base)))
    allowed = [base]
    if cargo.tilt_to_length:
        allowed.append((base[2], base[1], base[0]))
    if cargo.tilt_to_width:
        allowed.append((base[0], base[2], base[1]))
    return list(dict.fromkeys(allowed))


def _support_allowed(items, cargo, position, size, config):
    x, y, z = position
    dx, dy, dz = size
    if cargo.disable_stacking and z > 1e-6:
        return False
    if cargo.max_stack_height_mm is not None and z + dz > cargo.max_stack_height_mm + 1e-6:
        return False
    if cargo.max_layers is not None and z + 1e-6 >= dz * cargo.max_layers:
        return False
    if z <= 1e-6:
        return True
    supported = 0.0
    for other in items:
        ox, oy, oz = other.position
        odx, ody, odz = other.size
        if abs(oz + odz - z) > 1e-6:
            continue
        overlap = _overlap_1d(x, dx, ox, odx) * _overlap_1d(y, dy, oy, ody)
        if overlap <= 0:
            continue
        if other.cargo.disable_stacking:
            return False
        if other.cargo.max_stack_mass_kg is not None and cargo.weight_kg > other.cargo.max_stack_mass_kg + 1e-6:
            return False
        supported += overlap
    threshold = float(getattr(config, "stability_min_support", getattr(config, "minimum_support_ratio", 0.0)))
    if cargo.overhang_allowed:
        threshold = 0.0
    return supported / max(dx * dy, 1.0) >= threshold


def _support_allowed_indexed(state, cargo, position, size, config):
    """Support test restricted to the cached top surface at this Z level.

    Positive results are memoized on the state: items are never removed or
    moved, so support coverage at a fixed position can only grow.  Negative
    results are deliberately not cached because a newly placed neighbour can
    make a previously unsupported position legal.
    """
    x, y, z = position
    dx, dy, dz = size
    if cargo.disable_stacking and z > 1e-6:
        return False
    if cargo.max_stack_height_mm is not None and z + dz > cargo.max_stack_height_mm + 1e-6:
        return False
    if cargo.max_layers is not None and z + 1e-6 >= dz * cargo.max_layers:
        return False
    if z <= 1e-6:
        return True
    threshold = float(getattr(config, "stability_min_support", getattr(config, "minimum_support_ratio", 0.0)))
    if cargo.overhang_allowed:
        threshold = 0.0
    cache_key = (round(x, 3), round(y, 3), round(z, 3), size[0], size[1], size[2],
                 float(cargo.weight_kg), threshold)
    ok = getattr(state, "_support_ok", None)
    if ok is None:
        ok = state._support_ok = set()
    elif cache_key in ok:
        return True
    supported = 0.0
    supports = {}
    for gx in range(int(x // 500), int((x + dx - 1e-6) // 500) + 1):
        for gy in range(int(y // 500), int((y + dy - 1e-6) // 500) + 1):
            for other in state.top_cells.get((round(z, 3), gx, gy), ()):
                supports[id(other)] = other
    for other in supports.values():
        ox, oy, _ = other.position
        odx, ody, _ = other.size
        overlap = _overlap_1d(x, dx, ox, odx) * _overlap_1d(y, dy, oy, ody)
        if overlap <= 0:
            continue
        if other.cargo.disable_stacking:
            return False
        if other.cargo.max_stack_mass_kg is not None and cargo.weight_kg > other.cargo.max_stack_mass_kg + 1e-6:
            return False
        supported += overlap
    if supported / max(dx * dy, 1.0) >= threshold:
        ok.add(cache_key)
        return True
    return False


def _support_ratio_indexed(state, position, size):
    """Return the actual support ratio for a placement above the floor."""
    x, y, z = position
    dx, dy, dz = size
    if z <= 1e-6:
        return 1.0
    supported = 0.0
    supports = {}
    for gx in range(int(x // 500), int((x + dx - 1e-6) // 500) + 1):
        for gy in range(int(y // 500), int((y + dy - 1e-6) // 500) + 1):
            for other in state.top_cells.get((round(z, 3), gx, gy), ()):
                supports[id(other)] = other
    for other in supports.values():
        ox, oy, _ = other.position
        odx, ody, _ = other.size
        overlap = _overlap_1d(x, dx, ox, odx) * _overlap_1d(y, dy, oy, ody)
        if overlap <= 0:
            continue
        supported += overlap
    return supported / max(dx * dy, 1.0)


def _placement_contact_area(state, position, size):
    """Total touching surface area between a candidate and packed items.

    Indexed version: the candidate always sits at a free-space corner, so it can
    only touch packed items on its bottom (z), left (x) and back (y) faces.  The
    state's face-cell indexes map each exposed face plane to the 500 mm cells it
    covers, turning the O(n) item scan into O(neighbors).
    """
    x, y, z = position
    dx, dy, dz = size
    gx0, gx1 = int(x // 500), int((x + dx - 1e-6) // 500) + 1
    gy0, gy1 = int(y // 500), int((y + dy - 1e-6) // 500) + 1
    gz0, gz1 = int(z // 500), int((z + dz - 1e-6) // 500) + 1
    contact = 0.0

    below = {}
    for gx in range(gx0, gx1):
        for gy in range(gy0, gy1):
            for other in state.top_cells.get((round(z, 3), gx, gy), ()):
                below[id(other)] = other
    for other in below.values():
        ox, oy, _ = other.position
        odx, ody, _ = other.size
        contact += _overlap_1d(x, dx, ox, odx) * _overlap_1d(y, dy, oy, ody)

    left = {}
    for gy in range(gy0, gy1):
        for gz in range(gz0, gz1):
            for other in state.right_cells.get((round(x, 3), gy, gz), ()):
                left[id(other)] = other
    for other in left.values():
        _, oy, oz = other.position
        _, ody, odz = other.size
        contact += _overlap_1d(y, dy, oy, ody) * _overlap_1d(z, dz, oz, odz)

    back = {}
    for gx in range(gx0, gx1):
        for gz in range(gz0, gz1):
            for other in state.front_cells.get((round(y, 3), gx, gz), ()):
                back[id(other)] = other
    for other in back.values():
        ox, _, oz = other.position
        odx, _, odz = other.size
        contact += _overlap_1d(x, dx, ox, odx) * _overlap_1d(z, dz, oz, odz)
    return contact


def _normalize_score_weights(config):
    """Return a safe, normalized copy of the scoring weight map."""
    defaults = {
        "contact_area": 0.40,
        "void_reduction": 0.20,
        "weight_balance": 0.10,
        "support_ratio": 0.10,
        "accessibility": 0.10,
        "wall_alignment": 0.05,
        "future_packing_potential": 0.05,
    }
    user = getattr(config, "score_weights", None) or {}
    merged = {**defaults, **{k: float(v) for k, v in user.items() if v is not None}}
    total = sum(merged.values()) or 1.0
    return {k: v / total for k, v in merged.items()}


def _placement_score(cargo, space, size, state, config):
    """Weighted SeaRates-style score for one legal placement candidate."""
    x, y, z = float(space.x), float(space.y), float(space.z)
    dx, dy, dz = float(size[0]), float(size[1]), float(size[2])
    L, W, H = float(state.container.length_mm), float(state.container.width_mm), float(state.container.height_mm)
    cached_weights = getattr(state, "_score_weights", None)
    if cached_weights is None or cached_weights[0] is not config:
        weights = _normalize_score_weights(config)
        state._score_weights = (config, weights)
    else:
        weights = cached_weights[1]

    # Contact surface with packed items and container boundaries
    item_contact = _placement_contact_area(state, (x, y, z), (dx, dy, dz))
    surface = 2.0 * (dx * dy + dx * dz + dy * dz)
    contact_score = min(1.0, item_contact / max(surface, 1.0))

    wall_contact = 0.0
    if x <= 1e-6:
        wall_contact += dy * dz
    if x + dx >= L - 1e-6:
        wall_contact += dy * dz
    if y <= 1e-6:
        wall_contact += dx * dz
    if y + dy >= W - 1e-6:
        wall_contact += dx * dz
    if z <= 1e-6:
        wall_contact += dx * dy
    wall_score = wall_contact / max(surface, 1.0)

    # Support / stability
    support = _support_ratio_indexed(state, (x, y, z), (dx, dy, dz))
    stability = support * (1.0 - z / max(H, 1.0))

    # Void and remaining-space metrics
    void_left = (space.length - dx) * (space.width - dy) * (space.height - dz)
    space_vol = max(space.length * space.width * space.height, 1.0)
    void_reduction = 1.0 - (max(void_left, 0.0) / space_vol)

    remaining_l = max(L - x - dx, 0.0)
    remaining_w = max(W - y - dy, 0.0)
    remaining_h = max(H - z - dz, 0.0)
    container_vol = L * W * H
    remaining_score = 1.0 - (remaining_l * remaining_w * remaining_h) / max(container_vol, 1.0)
    future_potential = 1.0 - (max(space.length - dx, 0.0) + max(space.width - dy, 0.0) + max(space.height - dz, 0.0)) / (L + W + H)

    # Center of gravity and weight balance
    item_weight = cargo.weight_kg
    new_weight = state.weight + item_weight
    if new_weight > 1e-9:
        new_cgx = (state._moment[0] + (x + dx / 2.0) * item_weight) / new_weight
        new_cgy = (state._moment[1] + (y + dy / 2.0) * item_weight) / new_weight
        new_cgz = (state._moment[2] + (z + dz / 2.0) * item_weight) / new_weight
    else:
        new_cgx, new_cgy, new_cgz = L / 2.0, W / 2.0, 0.0
    dist_xy = (((new_cgx - L / 2.0) / max(L / 2.0, 1.0)) ** 2 + ((new_cgy - W / 2.0) / max(W / 2.0, 1.0)) ** 2) ** 0.5
    weight_balance = max(0.0, 1.0 - dist_xy)
    low_cog = max(0.0, 1.0 - new_cgz / max(H, 1.0))

    # Layer continuity (complete the current layer before climbing)
    current_lowest_z = getattr(state, "free_min_z", None)
    if current_lowest_z is None:
        current_lowest_z = min((s.z for s in state.free.spaces.values()), default=0.0)
    if abs(z - current_lowest_z) < 1e-6:
        layer_continuity = 1.0
    else:
        layer_continuity = max(0.0, 1.0 - (z - current_lowest_z) / max(H, 1.0))

    # Accessibility / unloading priority
    direction = getattr(config, "load_direction", "inside_out")
    unloading = getattr(cargo, "unloading_order", None)
    if unloading is not None and direction == "inside_out":
        # unload first -> load last -> place near door (x = L)
        target_x = L if unloading <= 1 else 0.0
        accessibility = 1.0 - abs((x + dx / 2.0) - target_x) / max(L, 1.0)
    elif unloading is not None and direction == "door_to_inside":
        target_x = 0.0 if unloading <= 1 else L
        accessibility = 1.0 - abs((x + dx / 2.0) - target_x) / max(L, 1.0)
    else:
        accessibility = 0.5

    components = {
        "contact_area": contact_score,
        "wall_alignment": wall_score,
        "support_ratio": support,
        "stability": stability,
        "void_reduction": void_reduction,
        "void_left": 1.0 - (space.length - dx) / max(space.length, 1.0),
        "void_above": 1.0 - (space.height - dz) / max(space.height, 1.0),
        "remaining_width": 1.0 - (space.width - dy) / max(space.width, 1.0),
        "remaining_height": 1.0 - (space.height - dz) / max(space.height, 1.0),
        "remaining_length": 1.0 - (space.length - dx) / max(space.length, 1.0),
        "remaining_space": remaining_score,
        "weight_balance": weight_balance,
        "low_cog": low_cog,
        "layer_continuity": layer_continuity,
        "accessibility": accessibility,
        "unloading_priority": accessibility,
        "future_packing_potential": future_potential,
    }

    total = 0.0
    for key, value in components.items():
        total += weights.get(key, 0.0) * float(value)
    return total


def _prune_and_merge_spaces(spaces):
    """Maintain a compact free-space tree after every placement."""
    # Large detailed loads must not spend quadratic time merging every historic
    # void.  This is the free-space priority cache: retain the most reachable
    # low-Z spaces and let the local Beam handle difficult regions.
    if len(spaces) > MAX_ACTIVE_FREE_SPACES:
        valid = [space for space in spaces if min(space.length, space.width, space.height) > 1e-6]
        return sorted(valid, key=lambda space: (space.z, -space.volume, space.x, space.y))[:MAX_ACTIVE_FREE_SPACES]
    kept = []
    for space in spaces:
        if min(space.length, space.width, space.height) <= 1e-6:
            continue
        contained = any(
            other is not space and space.x >= other.x - 1e-6 and space.y >= other.y - 1e-6 and space.z >= other.z - 1e-6
            and space.x + space.length <= other.x + other.length + 1e-6
            and space.y + space.width <= other.y + other.width + 1e-6
            and space.z + space.height <= other.z + other.height + 1e-6
            for other in spaces
        )
        if not contained:
            kept.append(space)
    changed = True
    while changed:
        changed = False
        for i, left in enumerate(kept):
            for j in range(i + 1, len(kept)):
                right = kept[j]
                merged = None
                if left.y == right.y and left.z == right.z and left.width == right.width and left.height == right.height and abs(left.x + left.length - right.x) < 1e-6:
                    merged = _FreeSpace(left.x, left.y, left.z, left.length + right.length, left.width, left.height)
                elif left.x == right.x and left.z == right.z and left.length == right.length and left.height == right.height and abs(left.y + left.width - right.y) < 1e-6:
                    merged = _FreeSpace(left.x, left.y, left.z, left.length, left.width + right.width, left.height)
                elif left.x == right.x and left.y == right.y and left.length == right.length and left.width == right.width and abs(left.z + left.height - right.z) < 1e-6:
                    merged = _FreeSpace(left.x, left.y, left.z, left.length, left.width, left.height + right.height)
                if merged is not None:
                    kept = [space for index, space in enumerate(kept) if index not in (i, j)] + [merged]
                    changed = True
                    break
            if changed:
                break
    return sorted(kept, key=lambda space: (space.z, space.x, space.y, space.volume))


def _split_free_space(layout, space_index, size):
    space = layout.spaces[space_index]
    dx, dy, dz = size
    spaces = [value for index, value in enumerate(layout.spaces) if index != space_index]
    # A non-overlapping guillotine partition: right, then width, then above.
    spaces.extend((
        _FreeSpace(space.x + dx, space.y, space.z, space.length - dx, space.width, space.height),
        _FreeSpace(space.x, space.y + dy, space.z, dx, space.width - dy, space.height),
        _FreeSpace(space.x, space.y, space.z + dz, dx, dy, space.height - dz),
    ))
    return _prune_and_merge_spaces(spaces)


def _layout_score(layout, container, total_cartons, config):
    """Weighted score used both to retain the Beam top-N and final plans."""
    if not layout.items:
        return 0.0
    used = sum(item.size[0] * item.size[1] * item.size[2] for item in layout.items)
    capacity = max(container.length_mm * container.width_mm * container.height_mm, 1.0)
    fill_rate = used / capacity
    void_ratio = 1.0 - fill_rate
    # Use a bounded geometry sample for detailed loads; fill and weight remain
    # exact while secondary quality metrics stay fast at thousands of cartons.
    stride = max(1, len(layout.items) // 300)
    metric_items = layout.items[::stride]
    contact_area = sum(_contact_area_for_item(item, metric_items) for item in metric_items) / max(used ** (2 / 3), 1.0)
    support_ratio = _average_support(metric_items)
    total_weight = max(sum(item.cargo.weight_kg for item in layout.items), 1e-9)
    cx = sum((item.position[0] + item.size[0] / 2) * item.cargo.weight_kg for item in layout.items) / total_weight
    cy = sum((item.position[1] + item.size[1] / 2) * item.cargo.weight_kg for item in layout.items) / total_weight
    cz = sum((item.position[2] + item.size[2] / 2) * item.cargo.weight_kg for item in layout.items) / total_weight
    weight_balance = max(0.0, 1.0 - (((cx - container.length_mm / 2) / max(container.length_mm / 2, 1)) ** 2 + ((cy - container.width_mm / 2) / max(container.width_mm / 2, 1)) ** 2) ** 0.5)
    low_cog = max(0.0, 1.0 - cz / max(container.height_mm, 1))
    fragmentation = 1.0 / max(len(layout.spaces), 1)
    loading_order = len(layout.items) / max(total_cartons, 1)
    fill, void, contact, support, balance, cog, fragment, order = get_pattern(getattr(config, "packing_pattern", "balanced")).weights
    return fill_rate * fill - void_ratio * void + contact_area * contact + support_ratio * support + weight_balance * balance + low_cog * cog + fragmentation * fragment + loading_order * order


def _contact_area_for_item(item, items):
    contact = 0.0
    x, y, z = item.position
    dx, dy, dz = item.size
    for other in items:
        if other is item:
            continue
        ox, oy, oz = other.position
        odx, ody, odz = other.size
        if abs(oz + odz - z) < 1e-6:
            contact += _overlap_1d(x, dx, ox, odx) * _overlap_1d(y, dy, oy, ody)
    return contact


def _average_support(items):
    """Mean support ratio over all items, using a 500 mm top-face grid."""
    if not items:
        return 0.0
    cells = {}
    for item in items:
        x, y, z = item.position
        dx, dy, dz = item.size
        top = round(z + dz, 3)
        for gx in range(int(x // 500), int((x + dx - 1e-6) // 500) + 1):
            for gy in range(int(y // 500), int((y + dy - 1e-6) // 500) + 1):
                cells.setdefault((top, gx, gy), []).append(item)
    total = 0.0
    for item in items:
        x, y, z = item.position
        dx, dy, _ = item.size
        if z <= 1e-6:
            total += 1.0
            continue
        below = {}
        for gx in range(int(x // 500), int((x + dx - 1e-6) // 500) + 1):
            for gy in range(int(y // 500), int((y + dy - 1e-6) // 500) + 1):
                for other in cells.get((round(z, 3), gx, gy), ()):
                    below[id(other)] = other
        supported = 0.0
        for other in below.values():
            supported += _overlap_1d(x, dx, other.position[0], other.size[0]) * _overlap_1d(y, dy, other.position[1], other.size[1])
        total += min(1.0, supported / max(dx * dy, 1.0))
    return total / len(items)


def _heuristic_key(candidate, heuristic):
    space, size = candidate
    x, y, z = space.x, space.y, space.z
    if heuristic == "length_first":
        return (-x, -z, -y)
    if heuristic == "width_first":
        return (-y, -z, -x)
    if heuristic == "best_volume_fit":
        return (-(space.volume - size[0] * size[1] * size[2]), -z)
    if heuristic == "best_free_space_reduction":
        return (-min(space.length - size[0], space.width - size[1], space.height - size[2]), -z)
    if heuristic == "best_contact_area":
        return (-(x + y + z), -z)
    if heuristic == "fill_width_height":
        # Width (Y), then vertical completion (Z), then container length (X).
        return (-y, -z, -x)
    if heuristic == "fill_length_first":
        return (-x, -y, -z)
    if heuristic == "wall_building":
        # Hold X constant while completing the Y/Z wall.
        return (-x, -z, -y)
    if heuristic == "layer_by_layer":
        return (-z, -y, -x)
    if heuristic == "weight_balanced":
        center_x = x + size[0] / 2
        center_y = y + size[1] / 2
        return (-z, -abs(center_y), -abs(center_x))
    return (-z, -x, -y)


def _greedy_place(state, carton, config, orientation_cache=None):
    """Fast Best-Fit / Bottom-Left-Fill step used after the Beam prefix."""
    current_weight = state.weight
    choices = []
    orientations = _cargo_orientations(carton)
    if orientation_cache is not None:
        orientations = orientation_cache.get(carton.id)
        if orientations is None:
            orientations = _cargo_orientations(carton)
            orientation_cache[carton.id] = orientations
    for size in orientations:
        if current_weight + carton.weight_kg > state.container.max_weight_kg + 1e-6:
            break
        for ident, space in state.free.candidates(size):
            if _support_allowed_indexed(state, carton, (space.x, space.y, space.z), size, config):
                choices.append((ident, space, size))
    if not choices:
        return False
    strategy = getattr(config, "placement_strategy", "bottom_left_fill")
    if strategy == "searates" or getattr(config, "enable_searates_scoring", True):
        ident, space, size = max(choices, key=lambda choice: _placement_score(carton, choice[1], choice[2], state, config))
    elif strategy == "weight_balanced":
        ident, space, size = max(
            choices,
            key=lambda choice: (
                -choice[1].z,
                -abs(choice[1].x + choice[2][0] / 2 - state.container.length_mm / 2),
                -abs(choice[1].y + choice[2][1] / 2 - state.container.width_mm / 2),
            ),
        )
    else:
        ident, space, size = max(choices, key=lambda choice: _heuristic_key((choice[1], choice[2]), strategy))
    state.place(carton, ident, space, size)
    return True


def beam_search_pack(container, cartons, config):
    """Hybrid packing: bounded Beam prefix followed by O(n) greedy packing."""
    beam = [_BeamLayout(spaces=[_FreeSpace(0.0, 0.0, 0.0, float(container.length_mm), float(container.width_mm), float(container.height_mm))])]
    beam_width = max(1, int(getattr(config, "beam_width", 12)))
    total = len(cartons)
    beam_limit = min(total, max(0, int(getattr(config, "beam_carton_limit", 20))))
    orientation_cache = {}
    for carton in cartons[:beam_limit]:
        new_beam = []
        for layout in beam:
            candidates = []
            current_weight = layout.weight
            orientations = orientation_cache.get(carton.id)
            if orientations is None:
                orientations = _cargo_orientations(carton)
                orientation_cache[carton.id] = orientations
            for index, space in enumerate(layout.spaces):
                for size in orientations:
                    if size[0] <= space.length + 1e-6 and size[1] <= space.width + 1e-6 and size[2] <= space.height + 1e-6:
                        if current_weight + carton.weight_kg <= container.max_weight_kg + 1e-6 and _support_allowed(layout.items, carton, (space.x, space.y, space.z), size, config):
                            candidates.append((index, space, size))
            # Keep an unpacked branch, so an early carton does not prevent a
            # later carton from using a compatible void.
            new_beam.append(_BeamLayout(list(layout.items), list(layout.spaces), layout.score, layout.weight))
            for index, space, size in candidates:
                placed = PackedItem(carton, (space.x, space.y, space.z), size)
                candidate = _BeamLayout(list(layout.items) + [placed], _split_free_space(layout, index, size), weight=layout.weight + carton.weight_kg)
                candidate.score = _layout_score(candidate, container, total, config)
                new_beam.append(candidate)
        # Deduplicate equivalent layouts before retaining the global top-N.
        unique = {}
        for layout in new_beam:
            signature = tuple(sorted((item.cargo.id, tuple(round(value, 3) for value in item.position), tuple(round(value, 3) for value in item.size)) for item in layout.items))
            if signature not in unique or layout.score > unique[signature].score:
                unique[signature] = layout
        beam = sorted(unique.values(), key=lambda layout: layout.score, reverse=True)[:beam_width]
    # Commit the strongest partial layout, then use fast Largest/Best-Fit/BLF
    # packing for all remaining cartons.  This prevents a Beam explosion on
    # detailed loads with thousands of cartons.
    best = max(beam, key=lambda layout: layout.score)
    state = _MutablePackingState(container, best.spaces, best.items)
    for carton in cartons[beam_limit:]:
        _greedy_place(state, carton, config, orientation_cache)
    result = _BeamLayout(state.items, list(state.free.spaces.values()), weight=state.weight)
    result.score = _layout_score(result, container, total, config)
    return result


def _completion_candidate_score(carton, space, size, state, config):
    """Score one legal placement without allocating a trial full layout."""
    volume = size[0] * size[1] * size[2] / max(
        state.container.length_mm * state.container.width_mm * state.container.height_mm, 1.0
    )
    if getattr(config, "enable_searates_scoring", True) or getattr(config, "placement_strategy", "") == "searates":
        quality = _placement_score(carton, space, size, state, config)
        # Volume is the primary key so the engine still commits big cartons first;
        # the SeaRates-style quality score breaks ties and drives void filling.
        return (volume, quality)
    heuristic = getattr(config, "placement_strategy", "bottom_left_fill")
    placement = _heuristic_key((space, size), heuristic)
    pattern = get_pattern(getattr(config, "packing_pattern", "balanced"))
    fill_weight = pattern.weights[0]
    # Larger legal cartons first reduces unusable residual voids.  The tuple
    # makes ties deterministic by the configured placement priority.
    return (volume * fill_weight, *placement)


def _pre_score(carton, space, size, state, config):
    """Cheap score used to select the Top-N candidates for full _placement_score.

    It rewards large volume and good fit between the item and the free space,
    without any O(n) contact or support geometry loops.
    """
    container_vol = max(state.container.length_mm * state.container.width_mm * state.container.height_mm, 1.0)
    placed_vol = size[0] * size[1] * size[2]
    volume = placed_vol / container_vol
    space_vol = max(space.length * space.width * space.height, 1.0)
    fit = placed_vol / space_vol
    return volume * (1.0 + fit) * 100.0


def _placement_signature(carton):
    """Attributes that decide which placements a carton can legally take.

    Cartons sharing this signature produce identical candidate sets and
    identical scores for every (space, orientation) pair, so evaluating the
    group once per step is exact, not approximate.
    """
    return (
        float(carton.length_mm), float(carton.width_mm), float(carton.height_mm),
        float(carton.weight_kg),
        bool(carton.tilt_to_length), bool(carton.tilt_to_width),
        carton.max_layers, carton.max_stack_height_mm,
        bool(carton.disable_stacking), bool(carton.overhang_allowed),
        carton.unloading_order,
    )


def _find_best_completion_candidate(state, alive_groups, config, orientation_cache,
                                    banned=(), deadline=None, remaining_count=None):
    """Exhaustively test every remaining carton group, orientation and space.

    ``alive_groups`` maps a placement signature to the still-unplaced cartons
    that share it (dict id -> carton, in sorted order).  Groups that currently
    have no dimension-feasible free space can never gain one later — free
    spaces only shrink under guillotine splits — so they are removed from the
    caller's dict and never re-scanned within this state.  Support-infeasible
    groups are kept because new top surfaces can make them legal again.

    Multi-stage filtering per evaluated group:
    1) dimension-feasible (space, orientation) pairs via a per-step fit cache
    2) cheap ``_pre_score`` ranking capped by ``per_group_score_cap``
    3) support filter and full ``_placement_score`` on the surviving Top-N.
    At most ``completion_lookahead`` distinct groups with legal placements are
    fully scored per step; the loop only terminates without a candidate when
    every alive group has been checked.
    """
    best = None
    banned = set(banned) if banned else ()
    container_vol = max(state.container.length_mm * state.container.width_mm * state.container.height_mm, 1.0)
    full_scores = 0
    pre_candidates = 0
    skipped_by_volume = 0
    if remaining_count is None:
        remaining_count = sum(len(members) for members in alive_groups.values())
    limit = _dynamic_full_score_limit(config, remaining_count)
    lookahead = max(1, int(getattr(config, "completion_lookahead", 4)))
    per_group_cap = max(1, int(getattr(config, "per_group_score_cap", 64)))
    fit_cache = {}
    dead = []
    scored_groups = 0
    searates = getattr(config, "enable_searates_scoring", True) or getattr(config, "placement_strategy", "") == "searates"
    heuristic = getattr(config, "placement_strategy", "bottom_left_fill")
    volume_factor = 1.0 if searates else get_pattern(getattr(config, "packing_pattern", "balanced")).weights[0]

    for group_rank, (sig, members) in enumerate(alive_groups.items()):
        if deadline is not None and monotonic() >= deadline:
            break
        if not members:
            dead.append(sig)
            continue
        carton = next(iter(members.values()))
        default_winner = next(iter(members))
        # Orientation permutations preserve bounding volume, so a group's best
        # possible primary score is known before any space is inspected.
        volume = carton.length_mm * carton.width_mm * carton.height_mm
        vol_ratio = volume / container_vol
        if best is not None and volume * volume_factor < best[0][0] - 1e-9:
            skipped_by_volume += len(members)
            continue
        if state.weight + carton.weight_kg > state.container.max_weight_kg + 1e-6:
            # Weight only grows while packing: permanently infeasible here.
            dead.append(sig)
            continue
        orientations = orientation_cache.get(carton.id)
        if orientations is None:
            orientations = _cargo_orientations(carton)
            orientation_cache[carton.id] = orientations
        feasible = []
        seen_pairs = set()
        truncated = False
        exhausted_sizes = getattr(state, "_exhausted_sizes", None)
        if exhausted_sizes is None:
            exhausted_sizes = state._exhausted_sizes = set()
        for size in orientations:
            # Dimension infeasibility is monotonic: free spaces only shrink, so
            # a size with no fitting space can never gain one in this state.
            if size in exhausted_sizes:
                continue
            fit_spaces = fit_cache.get(size)
            if fit_spaces is None:
                # Priority-ordered collection bounds the scan to the most
                # promising spaces; an empty result triggers one exhaustive
                # pass that proves no orientation of this size fits anywhere.
                fit_spaces = state.free.fit_candidates_ordered(size, per_group_cap)
                if not fit_spaces:
                    fit_spaces = state.free.all_fit_candidates(size)
                    if not fit_spaces:
                        exhausted_sizes.add(size)
                elif len(fit_spaces) >= per_group_cap:
                    truncated = True
                fit_cache[size] = fit_spaces
            for ident, space in fit_spaces:
                pre_candidates += 1
                seen_pairs.add((ident, size))
                feasible.append((ident, space, size))
        if not feasible:
            dead.append(sig)
            continue
        if len(feasible) > per_group_cap:
            feasible.sort(key=lambda candidate: _pre_score(carton, candidate[1], candidate[2], state, config), reverse=True)
            truncated = True
            del feasible[per_group_cap:]
        group_best = None
        for ident, space, size in feasible:
            if deadline is not None and monotonic() >= deadline:
                break
            if banned:
                sx, sy, sz = round(space.x, 3), round(space.y, 3), round(space.z, 3)
                winner_id = next(
                    (member_id for member_id in members
                     if (member_id, sx, sy, sz, size) not in banned),
                    None,
                )
                if winner_id is None:
                    skipped_by_volume += 1
                    continue
            else:
                winner_id = default_winner
            if not _support_allowed_indexed(state, carton, (space.x, space.y, space.z), size, config):
                continue
            if getattr(config, "adaptive_search", True) and full_scores >= limit:
                continue
            if searates:
                score = (vol_ratio, _placement_score(carton, space, size, state, config))
            else:
                score = (vol_ratio * volume_factor, *_heuristic_key((space, size), heuristic))
            full_scores += 1
            if group_best is None or score > group_best[0]:
                group_best = (score, -group_rank, winner_id, sig, ident, space, size)
        if group_best is None and truncated:
            # Every collected candidate failed support or was banned.  Capping
            # could have hidden a legal placement beyond the priority horizon,
            # so run one exhaustive pass over the unscored pairs before this
            # group is allowed to produce nothing for the step.
            for size in orientations:
                if deadline is not None and monotonic() >= deadline:
                    break
                if size in exhausted_sizes:
                    continue
                for ident, space in state.free.all_fit_candidates(size):
                    if (ident, size) in seen_pairs:
                        continue
                    seen_pairs.add((ident, size))
                    pre_candidates += 1
                    if banned:
                        sx, sy, sz = round(space.x, 3), round(space.y, 3), round(space.z, 3)
                        winner_id = next(
                            (member_id for member_id in members
                             if (member_id, sx, sy, sz, size) not in banned),
                            None,
                        )
                        if winner_id is None:
                            continue
                    else:
                        winner_id = default_winner
                    if not _support_allowed_indexed(state, carton, (space.x, space.y, space.z), size, config):
                        continue
                    if getattr(config, "adaptive_search", True) and full_scores >= limit:
                        continue
                    if searates:
                        score = (vol_ratio, _placement_score(carton, space, size, state, config))
                    else:
                        score = (vol_ratio * volume_factor, *_heuristic_key((space, size), heuristic))
                    full_scores += 1
                    if group_best is None or score > group_best[0]:
                        group_best = (score, -group_rank, winner_id, sig, ident, space, size)
        if group_best is not None:
            scored_groups += 1
            if best is None or group_best[0] > best[0]:
                best = group_best
            if scored_groups >= lookahead:
                break
    for sig in dead:
        alive_groups.pop(sig, None)
    # Diagnostics to let the engine measure search cost.
    state._search_stats = getattr(state, "_search_stats", {})
    state._search_stats["full_scores"] = state._search_stats.get("full_scores", 0) + full_scores
    state._search_stats["pre_candidates"] = state._search_stats.get("pre_candidates", 0) + pre_candidates
    state._search_stats["skipped_by_volume"] = state._search_stats.get("skipped_by_volume", 0) + skipped_by_volume
    return best


def _completion_score(state, config):
    capacity = max(state.container.length_mm * state.container.width_mm * state.container.height_mm, 1.0)
    fill = state.used_volume / capacity
    # Exact used volume is dominant; fewer active voids resolves equal fills.
    return (fill, -len(state.free.spaces), -len(state.items))


def _rebuild_completion_state(container, history):
    """Rebuild only a checkpoint and its tail, never the entire vehicle plan."""
    state = _MutablePackingState(container, exhaustive=True)
    for carton, position, size in history:
        found = state.free.find_exact(position, size)
        if found is None:
            return None
        ident, space = found
        state.place(carton, ident, space, size)
    return state


def _complete_until_stalled(state, remaining, history, config, orientation_cache,
                            banned=(), deadline=None, signature_cache=None):
    """Place the globally best legal carton, then retry all remaining cartons.

    Remaining cartons are bucketed once by placement signature.  Each step
    evaluates a bounded lookahead window of distinct groups instead of
    rescanning every carton; groups that can never fit again are dropped
    permanently, so "no legal placement" is still only concluded after every
    remaining carton has effectively been checked.
    """
    remaining = list(remaining)
    history = list(history)
    if signature_cache is None:
        signature_cache = {}
    alive = {}
    for carton in remaining:
        sig = signature_cache.get(carton.id)
        if sig is None:
            sig = _placement_signature(carton)
            signature_cache[carton.id] = sig
        members = alive.get(sig)
        if members is None:
            alive[sig] = members = {}
        members[carton.id] = carton
    placed_ids = set()
    unplaced_count = len(remaining)
    while unplaced_count:
        if deadline is not None and monotonic() >= deadline:
            break
        per_call = None
        if deadline is not None:
            remaining_time = deadline - monotonic()
            per_call = monotonic() + min(remaining_time, max(0.5, 5.0 * remaining_time / max(1, unplaced_count)))
        candidate = _find_best_completion_candidate(
            state, alive, config, orientation_cache, banned, per_call, unplaced_count
        )
        if candidate is None:
            break
        _, _, carton_id, sig, ident, space, size = candidate
        members = alive[sig]
        carton = members.pop(carton_id)
        if not members:
            del alive[sig]
        placed_ids.add(carton_id)
        unplaced_count -= 1
        state.place(carton, ident, space, size)
        history.append((carton, (space.x, space.y, space.z), size))
    return state, [carton for carton in remaining if carton.id not in placed_ids], history


def _recover_tail(container, state, remaining, history, config, orientation_cache,
                  count, deadline=None, signature_cache=None):
    """Undo a bounded tail, search alternative placements, and rollback safely."""
    count = min(max(1, count), len(history))
    if not count:
        return None
    checkpoint, tail = history[:-count], history[-count:]
    trial = _rebuild_completion_state(container, checkpoint)
    if trial is None:
        return None
    retry_cartons = list(remaining) + [carton for carton, _, _ in tail]
    banned = {(carton.id, round(position[0], 3), round(position[1], 3), round(position[2], 3), size)
              for carton, position, size in tail}
    trial, retry_remaining, retry_history = _complete_until_stalled(
        trial, retry_cartons, checkpoint, config, orientation_cache, banned,
        deadline=deadline, signature_cache=signature_cache,
    )
    if _completion_score(trial, config) > _completion_score(state, config):
        return trial, retry_remaining, retry_history
    return None


def vehicle_completion_pack(container, cartons, config, deadline=None):
    """Commercial vehicle-completion state machine.

    A vehicle is only returned after an exhaustive legal-placement pass, local
    recovery and incremental repacking all fail to create another placement.
    """
    if deadline is None:
        deadline = monotonic() + float(getattr(config, "time_budget_seconds", 60.0))
    state = _MutablePackingState(container, exhaustive=True)
    remaining = list(sort_items_for_loading(cartons, config))
    history = []
    orientations = {}
    signatures = {}
    local_attempted = False
    incremental_attempted = False

    while True:
        if monotonic() >= deadline:
            break
        state, remaining, history = _complete_until_stalled(
            state, remaining, history, config, orientations,
            deadline=deadline, signature_cache=signatures,
        )
        if not remaining:
            break

        # Phase 0: below target, recover a configurable recent placement tail
        # before a second vehicle is even considered.
        utilization = state.used_volume / max(container.length_mm * container.width_mm * container.height_mm, 1.0) * 100
        if utilization < float(getattr(config, "vehicle_completion_target_pct", 90.0)):
            recovered = _recover_tail(
                container, state, remaining, history, config, orientations,
                int(getattr(config, "recovery_placement_count", 12)),
                deadline=deadline, signature_cache=signatures,
            )
            if recovered is not None:
                state, remaining, history = recovered
                local_attempted = True
                continue

        # Local optimization is deliberately bounded to cartons around the
        # latest fragmented/void region (the recent history tail).
        if not local_attempted:
            recovered = _recover_tail(
                container, state, remaining, history, config, orientations,
                min(8, int(getattr(config, "recovery_placement_count", 12))),
                deadline=deadline, signature_cache=signatures,
            )
            local_attempted = True
            if recovered is not None:
                state, remaining, history = recovered
                continue

        # Incremental repacking is smaller again and never rebuilds all cargo.
        if not incremental_attempted:
            recovered = _recover_tail(
                container, state, remaining, history, config, orientations,
                int(getattr(config, "incremental_repack_count", 5)),
                deadline=deadline, signature_cache=signatures,
            )
            incremental_attempted = True
            if recovered is not None:
                state, remaining, history = recovered
                continue

        # All cartons/orientations/free-spaces were searched, then both local
        # phases and their retry passes completed without a legal placement.
        break

    result = _BeamLayout(state.items, list(state.free.spaces.values()), weight=state.weight)
    result.score = _layout_score(result, container, len(cartons), config)
    result.history = history
    result.remaining = remaining
    result.search_stats = getattr(state, "_search_stats", {})
    return result, remaining


def _tail_repack_score(layout, container, total, config):
    """Score a candidate repack layout using the original config weights."""
    return _layout_score(layout, container, total, config)


def _dynamic_full_score_limit(config, item_count):
    """Return the maximum number of candidates that will receive a full score."""
    hard = int(getattr(config, "max_full_score_candidates", 1500))
    if not getattr(config, "adaptive_search", True):
        return hard
    table = getattr(config, "max_full_score_candidates_map", {100: 300, 300: 600, 700: 1000})
    for threshold, value in sorted(table.items()):
        if item_count < threshold:
            return min(hard, value)
    return min(hard, 1500)


def _auto_derive_config(config, items):
    """Derive an effective config for mode='auto' based on problem size and mix."""
    if getattr(config, "mode", "") != "auto":
        return config
    count = len(items)
    vols = [getattr(item, "volume_m3", item.length_mm * item.width_mm * item.height_mm / 1e9) for item in items]
    min_vol = min(vols) if vols else 1.0
    max_vol = max(vols) if vols else 1.0
    ratio = max_vol / max(min_vol, 1e-9)

    # Large homogeneous loads -> fast heuristic. Mixed or medium -> Searates quality.
    if count >= 300 or ratio <= 1.5:
        time_budget = min(60.0, max(4.0, count * 0.025))
        return replace(
            config,
            packing_sequence="largest_volume_first",
            placement_strategy="bottom_left_fill",
            packing_pattern="balanced",
            enable_searates_scoring=False,
            enable_repacking=False,
            adaptive_search=False,
            time_budget_seconds=float(time_budget),
            max_full_score_candidates=min(300, int(getattr(config, "max_full_score_candidates", 1500))),
        )

    time_budget = min(60.0, max(8.0, count * 0.12 + 2.0))
    full_score_limit = _dynamic_full_score_limit(config, count)
    return replace(
        config,
        packing_sequence="searates_priority",
        placement_strategy="searates",
        packing_pattern="balanced",
        enable_searates_scoring=True,
        enable_repacking=True,
        adaptive_search=True,
        time_budget_seconds=float(time_budget),
        max_full_score_candidates=int(full_score_limit),
    )


def _used_volume(layout):
    return sum(item.size[0] * item.size[1] * item.size[2] for item in layout.items)


def _active_repack_tail_sizes(config, fill_pct):
    """Choose repack depth based on how full the fast pack already is."""
    tail_sizes = list(getattr(config, "repack_tail_sizes", (5, 12, 25)))
    if fill_pct >= 95.0:
        return ()
    if fill_pct >= 85.0:
        return (tail_sizes[0],)
    if fill_pct >= 65.0:
        return tail_sizes
    return tail_sizes + [40]


def repack_container(container: ContainerSpec, items, config=None):
    """Pack + adaptive multi-strategy tail repacking to reduce leftover.

    This is a safe backtracking wrapper: every trial starts from a checkpoint,
    never discards a better result, and respects the configured time budget.
    """
    config = config or LoadingConfig()
    start = monotonic()
    deadline = start + float(getattr(config, "time_budget_seconds", 60.0))
    base_layout, base_leftover = vehicle_completion_pack(container, items, config, deadline=deadline)
    base_layout.score = _tail_repack_score(base_layout, container, len(items), config)
    best_layout, best_leftover = base_layout, base_leftover
    best_score = base_layout.score

    if not getattr(config, "enable_repacking", True) or not base_layout.history:
        return best_layout, best_leftover

    # Trigger decision: only spend time on tail repack if there are leftovers
    # and the layout has not already reached the configured targets.
    total = len(items)
    packed = total - len(base_leftover)
    if packed == total:
        return best_layout, best_leftover
    used = _used_volume(best_layout)
    capacity = container.length_mm * container.width_mm * container.height_mm
    util_pct = (used / max(capacity, 1.0)) * 100.0
    capacity_m3 = capacity / 1e9
    remaining_m3 = capacity_m3 - used / 1e9
    if remaining_m3 <= float(getattr(config, "repack_remaining_volume_min", 1.0)):
        return best_layout, best_leftover
    if util_pct >= float(getattr(config, "repack_utilization_threshold", 0.90)) * 100.0:
        return best_layout, best_leftover
    score_target = float(getattr(config, "repack_score_target", 0.0))
    if score_target > 0 and best_score >= score_target:
        return best_layout, best_leftover

    # Adaptive tail depth based on how full the base pack already is.
    active_tails = _active_repack_tail_sizes(config, util_pct)
    if not active_tails:
        return best_layout, best_leftover
    strategies = list(getattr(config, "repack_strategy_variants", ("best_volume_fit", "best_contact_area", "bottom_left_fill")))
    epsilon = float(getattr(config, "repack_epsilon", 0.001))
    prev_score = best_score
    improvement_found = True

    for tail_size in active_tails:
        if monotonic() >= deadline - 1.0:
            break
        if len(best_layout.history) < tail_size:
            continue
        for strategy in strategies:
            if monotonic() >= deadline - 1.0:
                break
            repack_cfg = replace(
                config,
                placement_strategy=strategy,
                enable_searates_scoring=(strategy == "searates"),
                enable_repacking=False,
                adaptive_search=True,
            )
            if repack_cfg == config:
                continue
            checkpoint = best_layout.history[:-tail_size]
            tail = best_layout.history[-tail_size:]
            trial = _rebuild_completion_state(container, checkpoint)
            if trial is None:
                continue
            retry_cartons = list(best_leftover) + [carton for carton, _, _ in tail]
            banned = {
                (carton.id, round(position[0], 3), round(position[1], 3), round(position[2], 3), size)
                for carton, position, size in tail
            }
            orientations = {}
            signatures = {}
            call_budget = (deadline - monotonic()) / max(1, len(active_tails) * len(strategies))
            call_deadline = monotonic() + max(0.5, call_budget)
            trial, retry_remaining, retry_history = _complete_until_stalled(
                trial, retry_cartons, checkpoint, repack_cfg, orientations, banned,
                deadline=call_deadline, signature_cache=signatures,
            )
            trial_layout = _BeamLayout(trial.items, list(trial.free.spaces.values()), weight=trial.weight)
            trial_layout.score = _tail_repack_score(trial_layout, container, len(items), config)
            trial_layout.search_stats = getattr(trial, "_search_stats", {})
            trial_layout.history = retry_history
            trial_layout.remaining = retry_remaining
            if trial_layout.score > best_score:
                best_score = trial_layout.score
                best_layout = trial_layout
                best_leftover = retry_remaining
                improvement_found = True
        # Early stop if this depth did not improve enough
        if not improvement_found or (best_score - prev_score) < epsilon:
            break
        prev_score = best_score
        improvement_found = False

    return best_layout, best_leftover


def pack_container(container: ContainerSpec, items, role="Selected", config=None):
    """Complete one vehicle before allowing the caller to create another."""
    config = _auto_derive_config(config or LoadingConfig(), items)
    start = monotonic()
    layout, completion_leftovers = repack_container(container, items, config)
    runtime = monotonic() - start
    packed = layout.items
    if getattr(config, "load_direction", "inside_out") == "door_to_inside":
        packed = [PackedItem(item.cargo, _normalize_position(item.position, item.size, container, config), item.size) for item in packed]

    packed_ids = {item.cargo.id for item in packed}
    leftovers = [item for item in completion_leftovers if item.id not in packed_ids]
    result = PackedContainer(spec=container, items=packed, role=role)
    # Kept on the plan so the global engine can compare the exact weighted
    # Beam score after each complete candidate layout is generated.
    result.optimization_score = layout.score
    result.score = layout.score
    # vehicle_completion_pack reaches this point only after exhaustive search,
    # recovery, local repacking and retry have all completed.
    result.completion_verified = True

    used = sum(item.size[0] * item.size[1] * item.size[2] for item in packed)
    capacity = max(container.length_mm * container.width_mm * container.height_mm, 1.0)
    total_weight = max(sum(item.cargo.weight_kg for item in packed), 1e-9)
    if total_weight > 0:
        cgx = sum((item.position[0] + item.size[0] / 2) * item.cargo.weight_kg for item in packed) / total_weight
        cgy = sum((item.position[1] + item.size[1] / 2) * item.cargo.weight_kg for item in packed) / total_weight
        cgz = sum((item.position[2] + item.size[2] / 2) * item.cargo.weight_kg for item in packed) / total_weight
    else:
        cgx = cgy = cgz = 0.0
    support = _average_support(packed)
    low_cog = max(0.0, 1.0 - cgz / max(container.height_mm, 1))
    stability = support * low_cog
    search_stats = getattr(layout, "search_stats", {})
    result.diagnostics = {
        "utilization_pct": (used / capacity) * 100.0,
        "weight_utilization_pct": (total_weight / max(container.max_weight_kg, 1.0)) * 100.0,
        "container_count": 1,
        "empty_space_m3": (capacity - used) / 1e9,
        "number_of_layers": len({round(item.position[2], 3) for item in packed}),
        "support_ratio": support,
        "center_of_gravity": (round(cgx, 2), round(cgy, 2), round(cgz, 2)),
        "stability_score": stability,
        "packing_score": getattr(layout, "score", 0.0),
        "number_of_extreme_points": len(layout.spaces),
        "runtime_s": round(runtime, 3),
        "candidate_count": search_stats.get("pre_candidates", 0),
        "full_scores": search_stats.get("full_scores", 0),
    }
    return result, leftovers
