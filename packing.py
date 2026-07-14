from dataclasses import dataclass, field
from itertools import permutations

from py3dbp import Bin, Item
from py3dbp.main import START_POSITION, RotationType, intersect

from .models import CargoItem, ContainerSpec, LoadingConfig, PackedContainer, PackedItem


MAX_ACTIVE_FREE_SPACES = 128


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
        if support_area / base_area < float(getattr(config, "minimum_support_ratio", 0.0)):
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
    return supported / max(dx * dy, 1.0) >= float(getattr(config, "minimum_support_ratio", 0.0))


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


def _layout_score(layout, container, total_cartons):
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
    return fill_rate * 55 - void_ratio * 15 + contact_area * 10 + support_ratio * 8 + weight_balance * 5 + low_cog * 3 + fragmentation * 2 + loading_order * 2


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
    if not items:
        return 0.0
    ratios = []
    for item in items:
        if item.position[2] <= 1e-6:
            ratios.append(1.0)
            continue
        ratios.append(min(1.0, _contact_area_for_item(item, items) / max(item.size[0] * item.size[1], 1.0)))
    return sum(ratios) / len(ratios)


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
    return (-z, -x, -y)


def _greedy_place(layout, carton, container, config, total, orientation_cache=None):
    """Fast Best-Fit / Bottom-Left-Fill step used after the Beam prefix."""
    current_weight = layout.weight
    choices = []
    orientations = _cargo_orientations(carton)
    if orientation_cache is not None:
        orientations = orientation_cache.get(carton.id)
        if orientations is None:
            orientations = _cargo_orientations(carton)
            orientation_cache[carton.id] = orientations
    for index, space in enumerate(layout.spaces):
        for size in orientations:
            if (size[0] <= space.length + 1e-6 and size[1] <= space.width + 1e-6
                    and size[2] <= space.height + 1e-6
                    and current_weight + carton.weight_kg <= container.max_weight_kg + 1e-6
                    and _support_allowed(layout.items, carton, (space.x, space.y, space.z), size, config)):
                choices.append((index, space, size))
    if not choices:
        return layout
    strategy = getattr(config, "placement_strategy", "bottom_left_fill")
    index, space, size = max(choices, key=lambda choice: _heuristic_key((choice[1], choice[2]), strategy))
    placed = PackedItem(carton, (space.x, space.y, space.z), size)
    result = _BeamLayout(list(layout.items) + [placed], _split_free_space(layout, index, size), weight=layout.weight + carton.weight_kg)
    return result


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
                candidate.score = _layout_score(candidate, container, total)
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
    for carton in cartons[beam_limit:]:
        best = _greedy_place(best, carton, container, config, total, orientation_cache)
    best.score = _layout_score(best, container, total)
    return best


def pack_container(container: ContainerSpec, items, role="Selected", config=None):
    """Optimize a single vehicle with Beam Search and commit only its best layout."""
    config = config or LoadingConfig()
    layout = beam_search_pack(container, sort_items_for_loading(items, config), config)
    packed = layout.items
    if getattr(config, "load_direction", "inside_out") == "door_to_inside":
        packed = [PackedItem(item.cargo, _normalize_position(item.position, item.size, container, config), item.size) for item in packed]

    packed_ids = {item.cargo.id for item in packed}
    leftovers = [item for item in items if item.id not in packed_ids]
    result = PackedContainer(spec=container, items=packed, role=role)
    # Kept on the plan so the global engine can compare the exact weighted
    # Beam score after each complete candidate layout is generated.
    result.optimization_score = layout.score
    result.score = layout.score
    return result, leftovers
