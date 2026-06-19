from itertools import permutations

from py3dbp import Bin, Item, Packer
from py3dbp.main import Axis, START_POSITION

from .models import CargoItem, ContainerSpec, LoadingConfig, PackedContainer, PackedItem


def can_fit_item(item: CargoItem, container: ContainerSpec) -> bool:
    cargo_dims = [item.length_mm, item.width_mm, item.height_mm]
    limit_dims = [container.length_mm, container.width_mm, container.height_mm]
    dimension_fit = any(all(rotated[i] <= limit_dims[i] for i in range(3)) for rotated in permutations(cargo_dims))
    return dimension_fit and item.weight_kg <= container.max_weight_kg


def sort_items_for_loading(items, config=None):
    config = config or LoadingConfig()
    if getattr(config, "heavy_priority", "heavy_bottom") == "heavy_bottom":
        return sorted(
            items,
            key=lambda item: (item.weight_kg, item.volume_mm3, item.length_mm * item.width_mm),
            reverse=True,
        )
    return sorted(items, key=lambda item: (item.volume_mm3, item.weight_kg), reverse=True)


def _pack_to_bin(bin_obj, item, axis_order):
    if not bin_obj.items:
        return bin_obj.put_item(item, START_POSITION)

    for axis in axis_order:
        for packed_item in list(bin_obj.items):
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
            if bin_obj.put_item(item, pivot):
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
    compacted = _apply_vertical_gravity(packed_items, config)
    compacted = _apply_inside_out_compaction(compacted, container, config)
    compacted = _apply_vertical_gravity(compacted, config)
    return compacted


def pack_container(container: ContainerSpec, items, role="Selected", config=None):
    config = config or LoadingConfig()
    active_bin = Bin(container.name, container.length_mm, container.width_mm, container.height_mm, container.max_weight_kg)
    active_bin.format_numbers(3)
    packer = Packer()
    packer.add_bin(active_bin)

    py_items = []
    cargo_by_id = {}
    for cargo in sort_items_for_loading(items, config):
        py_item = Item(cargo.id, cargo.length_mm, cargo.width_mm, cargo.height_mm, cargo.weight_kg)
        py_item.format_numbers(3)
        py_items.append(py_item)
        cargo_by_id[cargo.id] = cargo

    axis_order = _axis_order(config)
    for py_item in py_items:
        if not _pack_to_bin(active_bin, py_item, axis_order):
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
