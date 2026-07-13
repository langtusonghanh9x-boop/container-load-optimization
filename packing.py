from itertools import permutations
from decimal import Decimal

from py3dbp import Bin, Item
from py3dbp.main import Axis, START_POSITION, RotationType, intersect

from .models import CargoItem, ContainerSpec, LoadingConfig, PackedContainer, PackedItem


class _PlacementIndex:
    """Spatial index for exact collision and support lookups during packing."""

    cell_size = 1000.0

    def __init__(self):
        self._cells = {}
        self._top_cells = {}
        self._items = {}
        self._order = {}

    @classmethod
    def _cell_range(cls, start, size):
        first = int(float(start) // cls.cell_size)
        # Subtracting a tiny epsilon keeps boxes touching on a grid boundary
        # from being indexed in an unrelated neighbouring cell.
        last = int((float(start) + float(size) - 1e-9) // cls.cell_size)
        return range(first, last + 1)

    def _horizontal_cells(self, position, dimension):
        return (
            (x, y)
            for x in self._cell_range(position[0], dimension[0])
            for y in self._cell_range(position[1], dimension[1])
        )

    def add(self, item):
        position = item.position
        dimension = item.get_dimension()
        item_id = id(item)
        self._items[item_id] = item
        self._order[item_id] = len(self._order)
        for x, y in self._horizontal_cells(position, dimension):
            for z in self._cell_range(position[2], dimension[2]):
                self._cells.setdefault((x, y, z), []).append(item_id)
        top = round(float(position[2]) + float(dimension[2]), 6)
        for x, y in self._horizontal_cells(position, dimension):
            self._top_cells.setdefault((top, x, y), []).append(item_id)

    def _ordered_items(self, item_ids):
        # Collision/support checks do not depend on insertion order. Avoiding a
        # sort here removes a significant hot-path cost for large load plans.
        return [self._items[item_id] for item_id in item_ids]

    def nearby(self, position, dimension):
        item_ids = set()
        for x, y in self._horizontal_cells(position, dimension):
            for z in self._cell_range(position[2], dimension[2]):
                item_ids.update(self._cells.get((x, y, z), ()))
        return self._ordered_items(item_ids)

    def supports_at(self, position, dimension):
        item_ids = set()
        base_z = round(float(position[2]), 6)
        for x, y in self._horizontal_cells(position, dimension):
            item_ids.update(self._top_cells.get((base_z, x, y), ()))
        return self._ordered_items(item_ids)


def can_fit_item(item: CargoItem, container: ContainerSpec) -> bool:
    cargo_dims = [item.length_mm, item.width_mm, item.height_mm]
    limit_dims = [container.length_mm, container.width_mm, container.height_mm]
    dimension_fit = any(all(rotated[i] <= limit_dims[i] for i in range(3)) for rotated in permutations(cargo_dims))
    return dimension_fit and item.weight_kg <= container.max_weight_kg


def sort_items_for_loading(items, config=None):
    config = config or LoadingConfig()

    if getattr(config, "heavy_priority", "heavy_bottom") == "heavy_bottom":
        base_key = lambda item: (-item.weight_kg, -item.volume_mm3, -(item.length_mm * item.width_mm))
    else:
        base_key = lambda item: (-item.volume_mm3, -item.weight_kg)

    # A selected order is packed first, from the inside of the container out.
    # Items without an order keep the existing behavior and are handled afterward.
    if any(item.loading_order is not None for item in items):
        return sorted(
            items,
            key=lambda item: (
                item.loading_order is None,
                item.loading_order if item.loading_order is not None else float("inf"),
                *base_key(item),
            ),
        )

    if getattr(config, "heavy_priority", "heavy_bottom") == "heavy_bottom":
        return sorted(items, key=base_key)
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


def _put_item_with_constraints(bin_obj, item, cargo, pivot, config, current_weight, rotations, placement_index):
    valid_position = item.position
    item.position = pivot
    for rotation in rotations:
        item.rotation_type = rotation
        dimension = item.get_dimension()
        if (
            bin_obj.width < pivot[0] + dimension[0]
            or bin_obj.height < pivot[1] + dimension[1]
            or bin_obj.depth < pivot[2] + dimension[2]
        ):
            continue
        if current_weight + item.weight > bin_obj.max_weight:
            item.position = valid_position
            return False

        base_z = float(pivot[2])
        top_z = base_z + float(dimension[2])
        if (
            (cargo.disable_stacking and base_z > 0)
            or (cargo.max_stack_height_mm is not None and top_z > cargo.max_stack_height_mm)
            or (cargo.max_layers is not None and base_z + 1e-6 >= float(dimension[2]) * cargo.max_layers)
        ):
            continue

        # Check collisions and supporting surfaces in the same pass.  This is
        # the hot path for large loading plans and preserves the former rules.
        support_area = 0.0
        placement_valid = True
        for packed_item in placement_index.nearby(pivot, dimension):
            if intersect(packed_item, item):
                placement_valid = False
                break

        if not placement_valid:
            continue
        for packed_item in placement_index.supports_at(pivot, dimension):
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
                placement_valid = False
                break
            if support is not None and support.max_stack_mass_kg is not None and float(item.weight) > support.max_stack_mass_kg:
                placement_valid = False
                break

        if base_z > 0:
            base_area = max(float(dimension[0]) * float(dimension[1]), 1.0)
            if support_area / base_area < float(getattr(config, "minimum_support_ratio", 0.0)):
                continue
        item.cargo = cargo
        bin_obj.items.append(item)
        placement_index.add(item)
        return True
    item.position = valid_position
    return False


def _pack_to_bin(bin_obj, item, cargo, axis_order, config, placement_index, current_weight):
    rotations = _allowed_rotations(cargo)
    if not bin_obj.items:
        return _put_item_with_constraints(bin_obj, item, cargo, START_POSITION, config, current_weight, rotations, placement_index)

    for axis in axis_order:
        # The loop returns immediately once an item is added, so iterating the
        # live list is safe and avoids copying every already-packed item.
        for packed_item in bin_obj.items:
            width, height, depth = packed_item.get_dimension()
            if axis == Axis.WIDTH:
                pivot = [
                    packed_item.position[0] + width,
                    packed_item.position[1],
                    packed_item.position[2],
                ]
            elif axis == Axis.HEIGHT:
                pivot = [
                    packed_item.position[0],
                    packed_item.position[1] + height,
                    packed_item.position[2],
                ]
            else:
                pivot = [
                    packed_item.position[0],
                    packed_item.position[1],
                    packed_item.position[2] + depth,
                ]
            if _put_item_with_constraints(bin_obj, item, cargo, pivot, config, current_weight, rotations, placement_index):
                return True
    return False


def _axis_order(config):
    # WIDTH means container length, HEIGHT means floor width, DEPTH means vertical.
    if getattr(config, "placement_strategy", "stable_floor_first") == "fill_width_before_length":
        return [Axis.HEIGHT, Axis.WIDTH, Axis.DEPTH]
    return [Axis.WIDTH, Axis.HEIGHT, Axis.DEPTH]


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
    # Packing already creates contact pivots. The post-processing pass is only
    # visual refinement and is quadratic, so skip it for large plans while
    # retaining the exact collision, weight and support checks from packing.
    if len(packed_items) > 100:
        return packed_items
    compacted = _apply_vertical_gravity(packed_items, config)
    compacted = _apply_inside_out_compaction(compacted, container, config)
    compacted = _apply_vertical_gravity(compacted, config)
    return compacted


def _pack_uniform_items_fast(container, items, role, config):
    """Pack unrestricted, same-size boxes on an exact grid in linear time.

    Large imports frequently contain thousands of identical cartons. For those
    loads, testing every historical pivot is unnecessary: a grid layout is
    collision-free, fully supported, and respects both container dimensions
    and payload. Complex cargo still uses the general exact placement engine.
    """
    if not items:
        return PackedContainer(spec=container, items=[], role=role), []

    first = items[0]
    dimensions = (first.length_mm, first.width_mm, first.height_mm)
    if (
        first.shape != "box"
        or first.tilt_to_length
        or first.tilt_to_width
        or first.disable_stacking
        or first.max_layers is not None
        or first.max_stack_mass_kg is not None
        or first.max_stack_height_mm is not None
        or any(
            item.shape != "box"
            or item.tilt_to_length
            or item.tilt_to_width
            or item.disable_stacking
            or item.max_layers is not None
            or item.max_stack_mass_kg is not None
            or item.max_stack_height_mm is not None
            or (item.length_mm, item.width_mm, item.height_mm) != dimensions
            for item in items
        )
    ):
        return None

    rotations = set(permutations(dimensions))
    candidates = []
    for size in rotations:
        dx, dy, dz = size
        if min(dx, dy, dz) <= 0:
            continue
        count_x = int(container.length_mm // dx)
        count_y = int(container.width_mm // dy)
        count_z = int(container.height_mm // dz)
        capacity = count_x * count_y * count_z
        if capacity:
            candidates.append((capacity, size, count_x, count_y, count_z))
    if not candidates:
        return None

    _, size, count_x, count_y, count_z = max(candidates, key=lambda candidate: candidate[0])
    dx, dy, dz = size
    packed = []
    total_weight = 0.0
    item_index = 0
    for z_index in range(count_z):
        for y_index in range(count_y):
            for x_index in range(count_x):
                if item_index >= len(items):
                    return PackedContainer(spec=container, items=packed, role=role), []
                cargo = items[item_index]
                if total_weight + cargo.weight_kg > container.max_weight_kg:
                    return PackedContainer(spec=container, items=packed, role=role), items[item_index:]
                position = _normalize_position(
                    (x_index * dx, y_index * dy, z_index * dz),
                    size,
                    container,
                    config,
                )
                packed.append(PackedItem(cargo=cargo, position=position, size=size))
                total_weight += cargo.weight_kg
                item_index += 1
    return PackedContainer(spec=container, items=packed, role=role), items[item_index:]


def _pack_unrestricted_boxes_fast(container, items, role, config):
    """Use a guillotine free-space packer for large, detailed box imports.

    Each free space is disjoint. Splitting it after a placement therefore
    guarantees exact non-overlap, boundary compliance, and full support for
    every stacked box without pairwise collision scans. It is used only for
    unrestricted boxes; specialised stacking rules retain the full engine.
    """
    if len(items) <= 100 or any(
        item.shape != "box"
        or item.tilt_to_length
        or item.tilt_to_width
        or item.disable_stacking
        or item.max_layers is not None
        or item.max_stack_mass_kg is not None
        or item.max_stack_height_mm is not None
        for item in items
    ):
        return None

    # x, y, z, available length, width, height
    free_spaces = [(0.0, 0.0, 0.0, float(container.length_mm), float(container.width_mm), float(container.height_mm))]
    packed = []
    packed_ids = set()
    total_weight = 0.0

    for cargo in items:
        if total_weight + cargo.weight_kg > container.max_weight_kg:
            continue
        rotations = set(permutations((cargo.length_mm, cargo.width_mm, cargo.height_mm)))
        best = None
        for space_index, (x, y, z, available_x, available_y, available_z) in enumerate(free_spaces):
            for size in rotations:
                dx, dy, dz = (float(value) for value in size)
                if dx > available_x or dy > available_y or dz > available_z:
                    continue
                remainder = available_x * available_y * available_z - dx * dy * dz
                score = (z, remainder, y, x)
                if best is None or score < best[0]:
                    best = (score, space_index, (x, y, z, available_x, available_y, available_z), (dx, dy, dz))
        if best is None:
            continue

        _, space_index, (x, y, z, available_x, available_y, available_z), size = best
        dx, dy, dz = size
        free_spaces.pop(space_index)
        # These three spaces partition the unused volume, so they never
        # overlap each other or any prior placement.
        split_spaces = [
            (x + dx, y, z, available_x - dx, available_y, available_z),
            (x, y + dy, z, dx, available_y - dy, available_z),
            (x, y, z + dz, dx, dy, available_z - dz),
        ]
        free_spaces.extend(space for space in split_spaces if min(space[3:]) > 1e-6)
        position = _normalize_position((x, y, z), size, container, config)
        packed.append(PackedItem(cargo=cargo, position=position, size=size))
        packed_ids.add(cargo.id)
        total_weight += cargo.weight_kg

    if not packed:
        return None
    leftovers = [item for item in items if item.id not in packed_ids]
    return PackedContainer(spec=container, items=packed, role=role), leftovers


def pack_container(container: ContainerSpec, items, role="Selected", config=None):
    config = config or LoadingConfig()
    sorted_items = sort_items_for_loading(items, config)
    fast_result = _pack_uniform_items_fast(container, sorted_items, role, config)
    if fast_result is not None:
        return fast_result
    fast_result = _pack_unrestricted_boxes_fast(container, sorted_items, role, config)
    if fast_result is not None:
        return fast_result

    active_bin = Bin(container.name, container.length_mm, container.width_mm, container.height_mm, container.max_weight_kg)
    active_bin.format_numbers(3)

    py_items = []
    cargo_by_id = {}
    for cargo in sorted_items:
        py_item = Item(cargo.id, cargo.length_mm, cargo.width_mm, cargo.height_mm, cargo.weight_kg)
        py_item.format_numbers(3)
        py_items.append(py_item)
        cargo_by_id[cargo.id] = cargo

    axis_order = _axis_order(config)
    placement_index = _PlacementIndex()
    current_weight = Decimal("0")
    for py_item in py_items:
        if _pack_to_bin(
            active_bin,
            py_item,
            cargo_by_id[py_item.name],
            axis_order,
            config,
            placement_index,
            current_weight,
        ):
            current_weight += py_item.weight
        else:
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
