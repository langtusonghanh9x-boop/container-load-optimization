from itertools import permutations

from py3dbp import Bin, Item
from py3dbp.main import START_POSITION, RotationType, intersect

from .models import CargoItem, ContainerSpec, LoadingConfig, PackedContainer, PackedItem


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


def pack_container(container: ContainerSpec, items, role="Selected", config=None):
    config = config or LoadingConfig()
    active_bin = Bin(container.name, container.length_mm, container.width_mm, container.height_mm, container.max_weight_kg)
    active_bin.format_numbers(3)
    py_items = []
    cargo_by_id = {}
    for cargo in sort_items_for_loading(items, config):
        py_item = Item(cargo.id, cargo.length_mm, cargo.width_mm, cargo.height_mm, cargo.weight_kg)
        py_item.format_numbers(3)
        py_items.append(py_item)
        cargo_by_id[cargo.id] = cargo

    for py_item in py_items:
        if not _pack_to_bin(active_bin, py_item, cargo_by_id[py_item.name], config):
            active_bin.unfitted_items.append(py_item)

    packed = []
    for py_item in active_bin.items:
        cargo = cargo_by_id[py_item.name]
        size = tuple(float(value) for value in py_item.get_dimension())
        position = _normalize_position(
            tuple(float(value) for value in py_item.position),
            size,
            container,
            config,
        )
        packed.append(PackedItem(
            cargo=cargo,
            position=position,
            size=size,
        ))

    packed = _apply_contact_compaction(packed, container, config)

    packed_ids = {item.cargo.id for item in packed}
    leftovers = [item for item in items if item.id not in packed_ids]
    return PackedContainer(spec=container, items=packed, role=role), leftovers
